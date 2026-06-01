# GCP Audit Tool — 상세 개발 컨텍스트 프롬프트

## 1. 프로젝트 개요

GCP 조직 내 전체 프로젝트의 빌링 연결 상태, 소유자/편집자, 리소스 현황을 파악하고
불필요한 프로젝트를 정리하기 위한 **웹 기반 감사 도구**이다.

- **소스 경로**: `/Users/kaiden.kim/gcp-audit/` (로컬 Mac), `/home/ec2-user/gcp-audit/` (EC2)
- **GitHub**: https://github.com/kaidenkim/gcp-audit
- **서버 포트**: 9090 (uvicorn + FastAPI)
- **EC2 인스턴스**: `i-0a7fc7068c3acbf73` (ap-northeast-2, tag: instance-tag-controller)
- **배포용 S3 버킷**: `kep-sre-config` (경로: `gcp-audit/`)
- **EC2 접속**: `aws ssm start-session --target i-0a7fc7068c3acbf73`
- **대상 GCP 조직**: `kakaoenterprise.com` (프로젝트 약 780개, 빌링 연결 148개)

---

## 2. 파일 구조

```
gcp-audit/
├── main.py           # FastAPI 앱 진입점, API 엔드포인트, SSE 스트리밍, 스캔 스레드
├── gcp.py            # GCP API 호출 전담 (프로젝트 목록, 빌링, IAM, 리소스 스캔)
├── billing.py        # BigQuery 비용 조회, 빌링 계정 현황 (Cloud Billing REST API)
├── export.py         # Excel 내보내기 (openpyxl)
├── server.py         # uvicorn 직접 실행용 진입점 (chdir + sys.path 설정)
├── run.sh            # 로컬 기동 스크립트
├── requirements.txt  # Python 의존성
└── static/
    └── index.html    # 단일 페이지 앱 (SPA, 바닐라 JS — Vue/React 미사용)

캐시 파일 (홈 디렉토리, 서버 재시작 시 자동 로드):
  ~/.gcp_audit_cache.json           — 전체 프로젝트 스캔 결과
  ~/.gcp_audit_resource_cache.json  — 리소스 스캔 결과
  ~/.gcp_audit_billing_costs.json   — 비용 스캔 결과
  ~/.gcp_audit_billing_settings.json — BigQuery 설정 (bq_project, bq_dataset)
```

---

## 3. 사용 라이브러리

### Python 백엔드 (`requirements.txt`)

| 라이브러리 | 버전 | 용도 |
|---|---|---|
| `fastapi` | 0.115.0 | REST API 프레임워크, SSE 스트리밍 |
| `uvicorn[standard]` | 0.30.6 | ASGI 서버 |
| `openpyxl` | 3.1.5 | Excel 내보내기 |
| `google-cloud-bigquery` | 3.25.0 | 빌링 비용 조회 (BigQuery Export) |
| `google-cloud-resource-manager` | 최신 | 프로젝트 목록/IAM (gRPC SDK) |
| `google-cloud-billing` | 최신 | 빌링 계정 조회 (warmup용만 사용) |
| `google-auth` | (간접 의존) | 인증 추상화 레이어 |
| `google-auth-httplib2` | (간접 의존) | HTTP 전송 어댑터 |
| `requests` | (간접 의존) | HTTP 클라이언트 (리소스 스캔용) |

### 프론트엔드 (`static/index.html`)
- 외부 라이브러리 **없음** — 순수 바닐라 JavaScript
- `fetch()` API로 백엔드 통신
- `EventSource`로 SSE 스트리밍 수신
- CSS는 인라인 `<style>` 태그

---

## 4. 인증 (Authentication)

### 인증 우선순위 (`gcp.py` → `_get_credentials()`)

```
1순위: ADC (Application Default Credentials)
       google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
       → ~/.config/gcloud/application_default_credentials.json
       → GOOGLE_APPLICATION_CREDENTIALS 환경변수
       → GCE 메타데이터 서버 (EC2에서는 해당 없음)

2순위: gcloud 토큰 폴백
       gcloud auth print-access-token
       → OAuthCreds(token=..., scopes=[...]) 로 래핑
       → 발급 시각 기준 50분 캐싱 (_GCLOUD_TOKEN_TTL)
```

### 환경별 인증 방식

| 환경 | 인증 방식 | 비고 |
|---|---|---|
| 로컬 Mac | gcloud 토큰 폴백 | ADC 파일 없음, `gcloud auth login` 필요 |
| EC2 | gcloud 토큰 폴백 | ADC 없음, gcloud 세션 유지 필요 |

### 인증 관련 중요 사항

- **gRPC billing_v1 호환 불가**: EC2 gcloud 토큰 환경에서 `ACCESS_TOKEN_TYPE_UNSUPPORTED` 오류 발생
  → 빌링 API는 모두 REST (`google.auth.transport.requests.AuthorizedSession`) 로 전환
- **OAuthCreds refresh 불가**: gcloud 토큰으로 생성한 `OAuthCreds`는 `refresh_token` 없음
  → `expiry=None` 으로 생성하여 만료 판단을 TTL 기반으로 처리
- **리소스 스캔 인증**: `AuthorizedSession` 대신 gcloud 토큰을 `Bearer` 헤더에 직접 주입
  → `requests.Session` + `s.headers.update({"Authorization": f"Bearer {token}"})`
  → 45분마다 자동 갱신 (`_get_session()` 내 토큰 재발급 로직)

### gcloud 바이너리 탐색 경로

```python
_GCLOUD_CANDIDATES = [
    "gcloud",
    "/usr/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/local/google-cloud-sdk/bin/gcloud",
    "/opt/google-cloud-sdk/bin/gcloud",
    "/opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    "~/google-cloud-sdk/bin/gcloud",
    "/home/ec2-user/google-cloud-sdk/bin/gcloud",
    "/root/google-cloud-sdk/bin/gcloud",
]
```

---

## 5. API 설계 (FastAPI 엔드포인트)

### main.py 엔드포인트 목록

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/status` | 전체 스캔 상태 + 프로젝트 목록 반환 |
| POST | `/api/scan` | 전체 스캔 시작 (백그라운드 스레드) |
| GET | `/api/scan/stream` | 전체 스캔 진행률 SSE 스트리밍 |
| GET | `/api/export` | 전체 스캔 결과 Excel 다운로드 |
| POST | `/api/projects/delete` | 프로젝트 삭제 |
| POST | `/api/resources/scan` | 리소스 스캔 시작 (빌링 연결 프로젝트만) |
| GET | `/api/resources/stream` | 리소스 스캔 진행률 SSE 스트리밍 |
| GET | `/api/resources` | 리소스 스캔 상태 + 결과 반환 |
| GET | `/api/billing/settings` | BigQuery 설정 조회 |
| POST | `/api/billing/settings` | BigQuery 설정 저장 |
| POST | `/api/billing/scan` | 빌링 비용 스캔 시작 |
| GET | `/api/billing/stream` | 빌링 비용 스캔 SSE 스트리밍 |
| GET | `/api/billing` | 빌링 비용 스캔 결과 반환 |
| GET | `/api/debug-scan` | 단일 프로젝트 리소스 진단 (개발용) |

### SSE 스트리밍 패턴

```python
# 각 스캔마다 큐(queue.Queue) 사용
# 스캔 스레드 → 큐에 progress/done/error 이벤트 push
# SSE 핸들러 → 0.4s 폴링으로 큐에서 pop → 클라이언트에 전달

yield f"data: {json.dumps({'type': 'progress', 'pct': 50, 'stage': '조회 중...'})}\n\n"
yield f"data: {json.dumps({'type': 'done'})}\n\n"
yield f"data: {json.dumps({'type': 'error', 'message': '...'})}\n\n"
```

### 서버 시작 (lifespan)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _load_cache()          # 캐시 파일에서 이전 스캔 결과 복원
    threading.Thread(target=_refresh_auth_cache, daemon=True).start()   # 60s마다 gcloud 계정 갱신
    threading.Thread(target=_warmup_credentials, daemon=True).start()   # gRPC 채널 사전 초기화
    yield
```

---

## 6. 전체 스캔 (`gcp.py` → `full_scan`)

### 스캔 흐름 (4단계)

```
Stage 1: 인증 초기화 + gRPC 클라이언트 풀 생성 (병렬 _N+1개)
         + 빌링 계정 목록 백그라운드 조회 시작

Stage 2: search_projects(page_size=1000) 스트리밍
         → 각 프로젝트 즉시 billing_task + owner_task 제출
         → max_workers=100 ThreadPoolExecutor
         → 빌링: REST AuthorizedSession (650 req/min 제한)
         → 소유자: gRPC ProjectsClient getIamPolicy (570 req/min 제한)

Stage 3: 백그라운드 billing accounts 결과 수집
         누락 빌링 계정 OPEN/CLOSED 보완 (최대 5 workers)

Stage 4: billing_failed + owner_failed 프로젝트 재시도
         (일시적 오류만 재시도, 정상 빌링 미연결 프로젝트 제외)
```

### 스캔 결과 프로젝트 구조

```json
{
  "project_id": "my-project-123",
  "name": "My Project",
  "create_time": "2023-01-15",
  "billing_enabled": "True",
  "billing_account_id": "ABCD12-345678-EFGH90",
  "billing_account_name": "내 결제 계정",
  "billing_open": "True",
  "owners": [
    "user:admin@kakaoenterprise.com",
    "serviceAccount:myapp@appspot.gserviceaccount.com"
  ],
  "deletable": "빌링연결"
}
```

### 삭제 가능 여부 판단 (`_deletable`)

| `deletable` 값 | 조건 |
|---|---|
| `"빌링연결"` | `billing_enabled=="True"` OR `billing_account_id` 존재 (CLOSED 포함) |
| `"소유자 확인 필요"` | 빌링 미연결 + `user:` / `group:` 소유자 없음 + 기본 프로젝트명 아님 |
| `"즉시 삭제 가능"` | 빌링 미연결 + 사람 계정 소유자 존재 OR 기본 프로젝트명 |

**핵심**: `billing_account_id` 있으면 `billing_enabled=False`여도 CLOSED 빌링 계정 연결 상태
→ "즉시 삭제 가능"으로 분류하면 안 됨

### 소유자/편집자 조회 (`_get_owners`)

- `roles/owner` + `roles/editor` 동시 조회
  (App Engine 기본 SA는 `roles/editor` 보유 → `roles/owner`만 보면 26% 누락)
- GCP 자동 생성 시스템 SA 제외 패턴:
  - `service-숫자@...` (예: `service-123456@gcp-sa-pubsub.iam.gserviceaccount.com`)
  - `숫자@...` (예: `123456@cloudservices.gserviceaccount.com`)
- **주의**: `get_iam_policy`는 프로젝트에 **직접 부여된** 권한만 반환. 조직/폴더 상속 IAM 미포함

---

## 7. API 할당량 제어 (`_TokenBucketLimiter`)

### 왜 Semaphore가 아닌 TokenBucketLimiter인가

- `Semaphore(N)`: 동시 요청 수만 제한, 초당 요청 수 제어 불가
  - API 응답이 0.3s이면 Semaphore(12) = 40 req/s = 2400 req/min → 쿼터(700/min) 3배 초과
- `_TokenBucketLimiter(rate)`: 분당 요청 수를 정확히 제한

### 설정값

```python
_BILLING_LIM = _TokenBucketLimiter(650)   # Cloud Billing API: 쿼터 ~700 → 650/min
_OWNER_LIM   = _TokenBucketLimiter(570)   # Resource Manager IAM: 쿼터 ~600 → 570/min
```

### 구현 핵심 (starvation 방지)

```python
def acquire(self) -> None:
    with self._lock:
        now = time.monotonic()
        if now >= self._next_allowed:
            self._next_allowed = now + self._interval
            return                          # 즉시 반환
        wait = self._next_allowed - now
        self._next_allowed += self._interval
    time.sleep(wait)   # 락 밖에서 1회만 sleep — while True 루프 없음
```

**해결한 버그**: `while True` 루프 사용 시 200개 스레드 동시 호출 → 각 스레드가 `_next_allowed`를
앞으로 밀어, 슬롯을 배정받은 스레드가 깨어나도 다시 sleep → 무한 루프 → 기아(starvation)

---

## 8. 리소스 스캔 (`gcp.py` → `scan_billing_resources`)

### 대상
빌링 연결 프로젝트 (`deletable == "빌링연결"`)만 스캔 — 현재 148개

### 조회하는 14개 리소스 타입

| 키 | API 엔드포인트 | 응답 키 | 방식 |
|---|---|---|---|
| `vm` | `compute/v1/projects/{pid}/aggregated/instances` | `instances` | aggregated |
| `run` | `run.googleapis.com/v2/projects/{pid}/locations/-/services` | `services` | list |
| `functions` | `cloudfunctions.googleapis.com/v2/projects/{pid}/locations/-/functions` | `functions` | list |
| `gke` | `container.googleapis.com/v1/projects/{pid}/locations/-/clusters` | `clusters` | list |
| `storage` | `storage.googleapis.com/storage/v1/b?project={pid}` | `items` | list |
| `sql` | `sqladmin.googleapis.com/v1/projects/{pid}/instances` | `items` | list |
| `pubsub` | `pubsub.googleapis.com/v1/projects/{pid}/topics` | `topics` | list |
| `vpc` | `compute/v1/projects/{pid}/global/networks` | `items` | list |
| `lb` | `compute/v1/projects/{pid}/aggregated/forwardingRules` | `forwardingRules` | aggregated |
| `armor` | `compute/v1/projects/{pid}/global/securityPolicies` | `items` | list |
| `marketplace` | `deploymentmanager/v2/projects/{pid}/global/deployments` | `deployments` | list |
| `sa` | `iam.googleapis.com/v1/projects/{pid}/serviceAccounts` | `accounts` | list |
| `log_sink` | `logging.googleapis.com/v2/projects/{pid}/sinks` | `sinks` | list |
| `log_bucket` | `logging.googleapis.com/v2/projects/{pid}/locations/-/buckets` | `buckets` | list |

**참고**: 모든 GCP 프로젝트는 기본으로 `_Default` + `_Required` 싱크·버킷 보유 → log_sink=2, log_bucket=2 항상

### 동시 요청 수 제한 (핵심)

```python
# outer: max_workers=5 × inner: 14개 = 최대 70 동시 HTTP 연결
with ThreadPoolExecutor(max_workers=5) as ex:
    ...
```

**이유**: `max_workers=40` (이전 설정) 시 40×14=560 동시 연결 → Google API 쿼터 초과 → 모든 요청 타임아웃 → 전부 0 반환
`max_workers=5` 시 148개 프로젝트 약 45초 내 안정 완료

### 인증 방식 (리소스 스캔 전용)

```python
# AuthorizedSession 공유 경합 방지 → gcloud 토큰 직접 주입
stdout, _, rc = _gcloud("auth", "print-access-token", timeout=15)
token = stdout.strip()

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {token}"})
session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=100))

# 45분마다 자동 갱신 (_get_session() 내부)
```

---

## 9. 빌링 비용 스캔 (`billing.py`)

### 두 가지 모드

#### 모드 1: BigQuery Export 모드 (`fetch_costs`)
- `~/.gcp_audit_billing_settings.json`에 `bq_project`, `bq_dataset` 설정 시 동작
- `google-cloud-bigquery` SDK 사용
- 최근 90일 프로젝트별·서비스별 비용 집계
- 쿼리: `gcp_billing_export_*` 테이블에서 `project.id`, `service.description`, `SUM(cost)`

#### 모드 2: 빌링 계정 현황 모드 (`fetch_billing_accounts`)
- BigQuery 미설정 시 기본 동작
- Cloud Billing REST API 사용: `https://cloudbilling.googleapis.com/v1/billingAccounts`
- `google.auth.transport.requests.AuthorizedSession` 사용
- 빌링 계정별 display_name, open 여부, 연결 프로젝트 수/목록 반환
- **접근 불가 빌링 계정 처리**: `list_billing_accounts`에 미노출 계정도 프로젝트 기반 stub 생성
  (다른 조직 소속 계정 등 → "접근불가" 표시)

#### 모드 3: Invoice API 모드 (`fetch_account_invoices`)
- Cloud Billing Invoice API: `https://cloudbilling.googleapis.com/v1/billingAccounts/{bid}/invoices`
- 카드 자동결제 계정에서는 404 반환 (인보이스 미지원)
- 최근 6개월 월별 청구 합계 반환

---

## 10. 프론트엔드 (`static/index.html`)

### 구조
- 바닐라 JS SPA, 프레임워크 없음
- 탭 4개: **프로젝트 관리** | **리소스 현황** | **결제 관리** | **빌링 비용**
- 전역 데이터: `PROJECTS_ALL`, `RES_ALL`, `ACCT_ALL`, `COSTS_ALL`

### 빌링 상태 표시 로직

```javascript
if (p.billing_enabled === 'True') {
  // OPEN/CLOSED 표시
  const openTag = p.billing_open === 'True' ? '● OPEN' : p.billing_open === 'False' ? '● CLOSED' : '';
  bSt = `연결됨 ${openTag}`;
} else if (p.billing_account_id) {
  bSt = '연결됨 ● CLOSED';   // billing_enabled=False + account_id 있음 = CLOSED 연결
} else {
  bSt = '미연결';
}
```

### 빌링 계정 상태 (결제 관리 탭)

```javascript
// billing.py의 open 필드: True | False | null (접근 불가)
const bStat = a.open === true
  ? '<span class="dot-open">● OPEN</span>'
  : a.open === false
    ? '<span class="dot-closed">● CLOSED</span>'
    : '<span style="color:#999">접근불가</span>';
```

### 필터 옵션
- **프로젝트 관리**: 빌링 상태(연결/OPEN/CLOSED/미연결), 삭제 가능 여부, 소유자 유무, 텍스트 검색
- **결제 관리**: OPEN/CLOSED/접근불가만 필터
- **리소스 현황**: 리소스 타입별 정렬, 프로젝트명 검색

### 리소스 현황 테이블 컬럼 순서
`🖥VM | ☁Run | ⚡Fn | ⎈GKE | 🗄Stor | 🗃SQL | 📨PS | 🌐VPC | ⚖LB | 🛡Armor | 👤SA | 📋Sink | 📦Bkt | 🏪Mkt`

---

## 11. 해결한 주요 버그 (최신순)

### [2026-06-01] 리소스 스캔 전부 0 반환
- **원인**: `max_workers=40` → 560 동시 HTTP 연결 → Google API 쿼터 초과 → 타임아웃 → 0
- **해결**: `max_workers=5` (70 동시 연결로 제한)
- **추가**: `AuthorizedSession` 대신 gcloud 토큰 직접 주입으로 스레드 경합 제거

### [2026-06-01] 빌링 계정 수 불일치 (148 vs 126)
- **원인**: `list_billing_accounts()` 조회 시 다른 조직 소속 계정 미포함
- **해결**: `billing.py`에 접근 불가 계정 stub 추가 (프로젝트에 연결된 계정 기준)

### [2026-06-01] `s3_client is not defined` 오류
- **원인**: 변수명 변경 후 잔여 참조 (`s3_client` → `session`)
- **해결**: `gcp.py` line 475 수정

### [이전] _TokenBucketLimiter starvation (EC2 스캔 1/780 멈춤)
- **원인**: `acquire()`의 `while True` 루프 — 슬롯 배정 후 무한 재sleep
- **해결**: `while True` 제거, 락 밖에서 1회 sleep

### [이전] 스캔 10분 소요
- **원인**: gRPC `RESOURCE_EXHAUSTED` 자동 재시도 최대 600초
- **해결**: `retry=None, timeout=15` + Semaphore → TokenBucketLimiter(650/570)

### [이전] OPEN/CLOSED 미표시
- **원인**: Stage 2 이후 `list_billing_accounts()` 호출 → 이미 quota 소진
- **해결**: Stage 2 시작 전 백그라운드 선제 조회 및 캐싱

### [이전] CLOSED 빌링 계정 "미연결"로 표시
- **원인**: `billing_enabled=False`를 무조건 미연결로 처리
- **해결**: `billing_account_id` 존재 여부로 CLOSED 상태 판단

### [이전] 소유자 26% 누락
- **원인**: `roles/owner`만 조회 (App Engine SA는 `roles/editor`)
- **해결**: `roles/owner` + `roles/editor` 동시 조회, 시스템 SA 패턴 필터

---

## 12. EC2 배포 절차

**주의**: EC2에서 `aws s3 cp`는 `No module named 'cryptography'` 오류로 실패.
반드시 **curl + presigned GET URL** 방식 사용.

```bash
# 1. S3에 업로드 (로컬에서)
aws s3 cp gcp.py s3://kep-sre-config/gcp-audit/gcp.py --region ap-northeast-2
aws s3 cp main.py s3://kep-sre-config/gcp-audit/main.py --region ap-northeast-2
aws s3 cp static/index.html s3://kep-sre-config/gcp-audit/index.html --region ap-northeast-2

# 2. presigned GET URL 생성 (600초 유효)
GCP_URL=$(aws s3 presign s3://kep-sre-config/gcp-audit/gcp.py --expires-in 600 --region ap-northeast-2)
MAIN_URL=$(aws s3 presign s3://kep-sre-config/gcp-audit/main.py --expires-in 600 --region ap-northeast-2)

# 3. SSM으로 EC2 배포 + 재시작
aws ssm send-command \
  --instance-ids "i-0a7fc7068c3acbf73" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "{\"commands\":[
    \"cd /home/ec2-user/gcp-audit\",
    \"curl -s -o gcp.py '${GCP_URL}'\",
    \"curl -s -o main.py '${MAIN_URL}'\",
    \"lsof -ti :9090 | xargs kill -9 2>/dev/null || true\",
    \"sleep 2\",
    \"nohup python3 server.py >> /tmp/gcp-audit.log 2>&1 &\",
    \"sleep 4\",
    \"curl -s http://localhost:9090/api/status | python3 -c \\\"import sys,json; d=json.load(sys.stdin); print(d.get('status'))\\\"\"
  ]}"
```

---

## 13. GCP API 동작 특성 (중요 참고사항)

- **`get_iam_policy`**: 프로젝트 직접 부여 권한만 반환, 조직/폴더 상속 IAM 미포함
- **`billing_enabled=False`의 두 가지 의미**:
  1. 빌링 계정 없음 (미연결) → `billing_account_id` 비어 있음
  2. CLOSED 상태의 빌링 계정 연결됨 → `billing_account_id` 있음
- **gRPC retry**: Google Cloud 클라이언트는 `RESOURCE_EXHAUSTED`를 최대 600초 재시도 → `retry=None` 필수
- **log_sink / log_bucket**: 모든 프로젝트에 `_Default`, `_Required` 기본값 존재 → 항상 2 이상
  GCP Console 확인: Logging → Log Router (sink), Logging → Log Storage (bucket)
- **빌링 계정 list_billing_accounts**: 접근 가능한 조직 내 계정만 반환. 타 조직 계정은 미포함

---

## 14. 로컬 개발 실행

```bash
cd /Users/kaiden.kim/gcp-audit
python3 server.py
# → http://localhost:9090

# 캐시 초기화 후 재시작
rm -f ~/.gcp_audit_*.json
python3 server.py
```

## 15. 현재 스캔 결과 (2026-06-01 기준)

- **전체 프로젝트**: 780개
- **빌링 연결 (`deletable=빌링연결`)**: 148개
- **리소스 스캔**: 148/148개, 총 833개 리소스
  - log_sink: 298, log_bucket: 296, sa: 105, storage: 59
  - vpc: 28, functions: 17, pubsub: 16, lb: 8, armor: 4
- **스캔 소요 시간**: 전체 ~2분, 리소스 ~45초
