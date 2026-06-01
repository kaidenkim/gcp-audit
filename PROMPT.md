# GCP Audit Tool — 개발 컨텍스트 프롬프트

## 프로젝트 개요

GCP 조직 내 전체 프로젝트의 빌링 연결 상태, 소유자/편집자, 리소스 현황을 파악하고
불필요한 프로젝트를 정리하기 위한 웹 기반 감사 도구이다.

- **소스**: `/Users/kaiden.kim/gcp-audit/` (로컬), `/home/ec2-user/gcp-audit/` (EC2)
- **GitHub**: https://github.com/kaidenkim/gcp-audit
- **서버 포트**: 9090 (uvicorn + FastAPI)
- **EC2 인스턴스**: `i-0a7fc7068c3acbf73` (ap-northeast-2)
- **배포 S3 버킷**: `kep-sre-data` (경로: `gcp-audit/`)
- **EC2 접속**: `aws ssm start-session --target i-0a7fc7068c3acbf73`

---

## 파일 구조

```
gcp-audit/
├── main.py           # FastAPI 앱, API 엔드포인트, SSE 스트리밍
├── gcp.py            # GCP API 호출 (프로젝트 목록, 빌링, IAM, 리소스 스캔)
├── billing.py        # BigQuery 비용 조회, 빌링 계정 현황
├── export.py         # Excel 내보내기 (openpyxl)
├── server.py         # uvicorn 직접 실행용 진입점
├── run.sh            # 로컬 기동 스크립트
├── requirements.txt  # Python 의존성
└── static/
    └── index.html    # 단일 페이지 앱 (SPA, Vue/React 없이 바닐라 JS)
```

캐시 파일 (홈 디렉토리에 저장):
- `~/.gcp_audit_cache.json` — 프로젝트 전체 스캔 결과
- `~/.gcp_audit_resource_cache.json` — 리소스 스캔 결과
- `~/.gcp_audit_billing_costs.json` — 비용 스캔 결과

---

## gcp.py 핵심 설계

### API 할당량 제어
GCP Cloud Billing API는 약 700 req/min, Resource Manager IAM은 약 600 req/min의 할당량이 있다.
780개 프로젝트를 `max_workers=200`으로 한꺼번에 요청하면 quota를 초과하고,
gRPC 클라이언트가 `RESOURCE_EXHAUSTED`에 대해 자동 재시도를 최대 600초 수행하여 스캔이 10분 이상 걸린다.

**해결책**:
- `retry=None, timeout=15` — gRPC 자동 재시도 비활성화, 즉시 실패
- `_TokenBucketLimiter(500)` — 분당 500 요청으로 정확히 제한 (Semaphore는 동시 요청 수만 제한, 속도 제어 불가)

```python
_BILLING_LIM = _TokenBucketLimiter(500)  # Cloud Billing: 쿼터 ~700 → 500/min
_OWNER_LIM   = _TokenBucketLimiter(500)  # IAM getIamPolicy: 쿼터 ~600 → 500/min
```

**중요 버그 (해결됨)**: `_TokenBucketLimiter`의 `while True` 루프
- 200개 스레드 동시 호출 시: 각 스레드가 슬롯 배정 후 sleep하는 동안 `_next_allowed`가 앞으로 밀림
- 슬롯 배정된 스레드가 깨어나도 `_next_allowed`가 훨씬 앞에 있어 **또 sleep** → 무한 재sleep → 기아(starvation)
- **수정**: `while True` 제거. 슬롯 배정 후 1회만 sleep.

```python
def acquire(self) -> None:
    with self._lock:
        now = time.monotonic()
        if now >= self._next_allowed:
            self._next_allowed = now + self._interval
            return
        wait = self._next_allowed - now
        self._next_allowed += self._interval
    time.sleep(wait)  # 락 밖에서 1회만 대기, 루프 없음
```

### 빌링 계정 OPEN/CLOSED 선제 조회
Stage 2에서 780개 프로젝트 병렬 빌링 조회가 할당량을 소진하기 전에
`list_billing_accounts()`를 먼저 호출하여 전체 빌링 계정의 OPEN/CLOSED 상태를 캐싱한다.

```python
# Stage 2 시작 전 실행
account_cache: dict[str, dict] = _list_all_billing_accounts(s3_client)
```

`s3_client`는 Stage 2 billing 클라이언트와 별도로 생성한 전용 클라이언트이다.
Stage 2에서 quota가 소진된 채널을 재사용하면 Stage 3 조회도 실패하기 때문이다.

### 소유자/편집자 조회 (`_get_owners`)
- `roles/owner` **와** `roles/editor` 모두 조회 (App Engine 기본 SA는 `roles/editor`)
- GCP 시스템 자동 생성 SA 제외: `service-숫자@...` 또는 `숫자@...` 패턴

```python
def _is_system_sa(member: str) -> bool:
    if not member.startswith("serviceAccount:"):
        return False
    sa = member[len("serviceAccount:"):]
    prefix = sa.split("@")[0]
    return bool(re.match(r"^service-\d+$", prefix) or re.match(r"^\d+$", prefix))
```

**중요**: `get_iam_policy`는 프로젝트에 **직접 부여된** 권한만 반환한다.
조직/폴더에서 상속된 IAM은 반환되지 않으므로 실제 IAM 콘솔과 다를 수 있다.

### 삭제 가능 여부 판단 (`_deletable`)

| 반환값 | 조건 |
|---|---|
| `빌링연결` | `billing_enabled == "True"` OR `billing_account_id` 존재 (CLOSED 포함) |
| `소유자 확인 필요` | 빌링 미연결 + `user:` / `group:` 소유자 없음 + 기본 프로젝트명 아님 |
| `즉시 삭제 가능` | 빌링 미연결 + 사람 계정 소유자 존재 |

```python
def _deletable(billing_enabled: str, billing_account_id: str, owners: list[str], name: str) -> str:
    is_default = not name or name == "My First Project" or name.startswith("My Project")
    if billing_enabled == "True" or billing_account_id:
        return "빌링연결"
    human_owners = [o for o in owners if o.startswith("user:") or o.startswith("group:")]
    if not human_owners and not is_default:
        return "소유자 확인 필요"
    return "즉시 삭제 가능"
```

**핵심**: `billing_account_id`가 있으면 `billing_enabled=False`여도 CLOSED 상태의 빌링 계정이 연결된 것이므로
"즉시 삭제 가능"으로 분류하면 안 된다.

### Stage 4 실패 프로젝트 재시도
`_check_billing` / `_get_owners`는 일시적 예외로 3회 모두 실패한 경우에만 `_failed=True` / `_OWNERS_FAILED` sentinel 반환.
Stage 4는 이 경우만 재시도 → 빌링 미연결 프로젝트(~700개) 전체 재시도 방지.

```python
# _check_billing 반환 예시
{"billing_enabled": "False", "billing_account_id": "", "_failed": True}   # 일시적 오류
{"billing_enabled": "False", "billing_account_id": "", "_failed": False}  # 정상 응답

# _get_owners 반환 예시
[]              # 정상 응답 (소유자 없음 포함)
_OWNERS_FAILED  # 일시적 오류 sentinel

# Stage 4 필터링
failed_billing = [pid for pid, v in billing_map.items() if v.get("_failed")]
failed_owners  = [pid for pid, v in owner_map.items() if v is _OWNERS_FAILED]
```

### 스캔 결과 필드

각 프로젝트는 다음 필드를 포함한다:

```json
{
  "project_id": "my-project-123",
  "name": "My Project",
  "create_time": "2023-01-15",
  "billing_enabled": "True",        // "True" | "False"
  "billing_account_id": "ABCD12-...",
  "billing_account_name": "My Billing Account",
  "billing_open": "True",           // "True" | "False" | "" (조회 실패 시)
  "owners": [
    "user:admin@example.com",
    "serviceAccount:myapp@appspot.gserviceaccount.com"
  ],
  "deletable": "빌링연결"           // "빌링연결" | "즉시 삭제 가능" | "소유자 확인 필요"
}
```

---

## index.html 핵심 설계

### 빌링 상태 표시 로직

`billing_enabled=False`이면서 `billing_account_id`가 있는 경우는
CLOSED 빌링 계정이 연결된 상태이므로 "미연결"이 아닌 "연결됨 CLOSED"로 표시한다.

```javascript
let bSt;
if (p.billing_enabled === 'True') {
  const openTag = p.billing_open === 'True'
    ? '<span class="dot-open">OPEN</span>'
    : p.billing_open === 'False'
      ? '<span class="dot-closed">CLOSED</span>'
      : '';
  bSt = `<span class="badge badge-billing">연결됨</span>${openTag}`;
} else if (p.billing_account_id) {
  // billing_enabled=False + billing_account_id 존재 = CLOSED 계정 연결됨
  bSt = '<span class="badge badge-billing">연결됨</span><span class="dot-closed">CLOSED</span>';
} else {
  bSt = '<span class="badge" style="background:#f1f3f4;color:#5f6368">미연결</span>';
}
```

### 소유자/편집자 컬럼
- 컬럼 헤더: `소유자/편집자`
- 모달 섹션: `소유자/편집자 (Owner/Editor)`
- 빈 경우: `소유자/편집자 없음`

### 필터 로직 (빌링)

```javascript
if (bi === 'connected' && p.billing_enabled !== 'True' && !p.billing_account_id) return false;
if (bi === 'OPEN' && !(p.billing_enabled === 'True' && p.billing_open === 'True')) return false;
if (bi === 'CLOSED' && !((p.billing_enabled === 'True' && p.billing_open === 'False') || (p.billing_account_id && p.billing_enabled !== 'True'))) return false;
if (bi === 'none' && (p.billing_enabled === 'True' || p.billing_account_id)) return false;
```

---

## EC2 배포 절차

로컬에서 수정 후 EC2에 반영하는 표준 절차:

```bash
# 1. S3에 업로드
aws s3 cp gcp.py s3://kep-sre-data/gcp-audit/gcp.py
aws s3 cp static/index.html s3://kep-sre-data/gcp-audit/index.html

# 2. presigned URL 생성
URL_GCP=$(aws s3 presign s3://kep-sre-data/gcp-audit/gcp.py --expires-in 1800)
URL_HTML=$(aws s3 presign s3://kep-sre-data/gcp-audit/index.html --expires-in 1800)

# 3. SSM으로 EC2에 배포 + 캐시 삭제 + 서비스 재시작
aws ssm send-command \
  --instance-ids "i-0a7fc7068c3acbf73" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "commands=[
    \"curl -s -o /home/ec2-user/gcp-audit/gcp.py '${URL_GCP}'\",
    \"curl -s -o /home/ec2-user/gcp-audit/static/index.html '${URL_HTML}'\",
    \"find /home/ec2-user/gcp-audit -name '*.json' -delete\",
    \"systemctl restart gcp-audit\",
    \"sleep 3 && systemctl is-active gcp-audit\"
  ]"

# 4. 결과 확인 (CommandId는 위 명령 결과에서 확인)
aws ssm get-command-invocation \
  --command-id "<CommandId>" \
  --instance-id "i-0a7fc7068c3acbf73" \
  --region ap-northeast-2 \
  --query "{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}"
```

**주의**: `aws s3 cp`로 EC2에서 직접 다운로드하는 방법은 실패한다.
EC2에 `No module named 'cryptography'` 오류가 발생하므로 반드시 `curl + presigned URL` 방식을 사용한다.

---

## 해결한 주요 버그 (최신순)

### 7. Stage 4가 ~700개 프로젝트를 불필요하게 재시도하는 문제
- **원인**: `failed_billing = [pid for pid, v in billing_map.items() if billing_enabled==False and no billing_account_id]` → 정상적으로 빌링 미연결인 프로젝트까지 모두 재시도
- **해결**: `_check_billing` 반환 dict에 `_failed` 플래그, `_get_owners` 반환에 `_OWNERS_FAILED` sentinel 추가. Stage 4는 실제 일시적 오류만 재시도.

### 8. EC2에서 스캔이 1/780에서 멈추는 문제 (_TokenBucketLimiter starvation)
- **원인**: `acquire()`의 `while True` 루프 — 200개 스레드 동시 호출 시 각 스레드가 `_next_allowed`를 앞으로 밀어, 슬롯을 배정받은 스레드가 깨어나도 또 sleep → 무한 재sleep → 사실상 1개 스레드만 실행
- **해결**: `while True` 제거. 슬롯 배정 후 락 밖에서 1회만 sleep.

### 1. 스캔 10분 걸리는 문제
- **원인**: gRPC 클라이언트가 `RESOURCE_EXHAUSTED` (quota 초과)에 대해 자동 재시도 최대 600초
- **해결**: 모든 API 호출에 `retry=None, timeout=15` 추가 + Semaphore → TokenBucketLimiter(500)으로 교체

### 2. OPEN/CLOSED 표시 안 되는 문제
- **원인**: `list_billing_accounts()`를 Stage 2 이후에 호출 → 이미 quota 소진 → 조회 실패
- **해결**: Stage 2 시작 전 선제 호출하여 캐싱, Stage 2/3 결과와 조합

### 3. CLOSED 빌링 계정도 "미연결"로 표시되는 문제
- **원인**: `billing_enabled=False`인 경우 무조건 "미연결"로 처리
- **해결**: `billing_account_id` 필드도 확인. 있으면 CLOSED 상태의 연결된 계정

### 4. "즉시 삭제 가능"인데 빌링 계정이 있는 문제
- **원인**: `_deletable()`이 `billing_enabled=="True"`만 체크, CLOSED 계정 누락
- **해결**: `billing_account_id` 있으면 `billing_enabled` 값에 관계없이 "빌링연결" 반환

### 5. 소유자가 표시 안 되는 프로젝트 (26% miss rate)
- **원인 1**: `_OWNER_SEM=40` 설정이 quota 초과를 유발 → Semaphore(10)으로 낮춤
- **원인 2**: `roles/owner`만 체크 → App Engine 기본 SA(`roles/editor`)를 놓침
- **해결**: `roles/owner` + `roles/editor` 동시 확인, 시스템 SA 패턴(`service-숫자@`, `숫자@`) 필터링

### 6. 소유자/편집자 239 → 346개로 증가
- App Engine 기본 SA(`PROJECT@appspot.gserviceaccount.com`)는 `roles/editor`를 가짐
- 이를 포함하도록 수정 후 107개 프로젝트에서 추가 소유자/편집자 발견

---

## GCP API 동작 특성 (중요 참고사항)

- **`get_iam_policy`**: 프로젝트 직접 부여 권한만 반환. 조직/폴더 상속 권한은 포함 안 됨
- **`billing_enabled=False` 의 두 가지 의미**:
  1. 빌링 계정 없음 (미연결)
  2. 빌링 계정은 연결되어 있지만 CLOSED 상태 → `billing_account_id`로 구분
- **gRPC retry**: Google Cloud 클라이언트 라이브러리는 `RESOURCE_EXHAUSTED`를 최대 600초 재시도
  → `retry=None`으로 비활성화 필수
- **`list_billing_accounts()`**: 단일 호출로 접근 가능한 모든 빌링 계정 반환 (OPEN/CLOSED 포함)
  `get_billing_account(bid)`: 계정별 `billing.accounts.get` 권한 필요 → 대부분 PermissionDenied

---

## systemd 서비스 설정 (EC2)

```ini
# /etc/systemd/system/gcp-audit.service
[Unit]
Description=GCP Project Auditor
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/gcp-audit
Environment="GOOGLE_APPLICATION_CREDENTIALS=/home/ec2-user/gcp-audit/service-account-key.json"
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 9090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

서비스 관리:
```bash
systemctl restart gcp-audit
systemctl status gcp-audit
journalctl -u gcp-audit -f
```
