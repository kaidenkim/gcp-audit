# GCP Audit Tool

GCP 조직 내 전체 프로젝트의 빌링 연결 상태, 소유자/편집자, 리소스 현황을 한눈에 파악하고 불필요한 프로젝트를 정리할 수 있는 웹 기반 감사 도구입니다.

---

## 주요 기능

| 탭 | 설명 |
|---|---|
| **프로젝트 관리** | 전체 프로젝트 목록 · 빌링 연결(OPEN/CLOSED) · 소유자/편집자 · 삭제 가능 여부 판단 |
| **리소스 관리** | 빌링 연결 프로젝트의 VM · Cloud Run · GKE · Cloud SQL · Cloud Functions 현황 |
| **결제 관리** | BigQuery 연동 비용 조회 또는 빌링 계정별 프로젝트 현황 |

### 삭제 가능여부 판단 기준

| 상태 | 조건 |
|---|---|
| **빌링연결** | 빌링 계정이 연결된 프로젝트 (OPEN 또는 CLOSED 모두 포함) |
| **즉시 삭제 가능** | 빌링 미연결 + 사람 계정(user:/group:) 소유자 존재 |
| **소유자 확인 필요** | 빌링 미연결 + 소유자/편집자가 서비스 계정만 있거나 기본 프로젝트명이 아닌 경우 |

---

## 사전 요구사항

- Python 3.9+
- `gcloud` CLI 설치 및 `gcloud auth application-default login` 완료
- GCP 조직 수준의 적절한 IAM 권한
  - `resourcemanager.projects.list`
  - `resourcemanager.projects.getIamPolicy`
  - `billing.accounts.get` / `billing.accounts.list`

---

## 로컬 실행

```bash
# 1. 소스코드 복제
git clone https://github.com/kaidenkim/gcp-audit.git
cd gcp-audit

# 2. 의존성 설치
pip3 install -r requirements.txt

# 3. GCP 인증
gcloud auth application-default login

# 4. 서버 기동 (포트 9090)
bash run.sh
```

브라우저에서 `http://localhost:9090` 접속

---

## EC2 서버 배포

### 1. EC2 환경 준비

```bash
# Amazon Linux 2023 기준
sudo dnf install -y python3 python3-pip git

# gcloud CLI 설치
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
tar -xf google-cloud-cli-linux-x86_64.tar.gz -C /usr/local/
/usr/local/google-cloud-sdk/install.sh --quiet
echo 'export PATH="/usr/local/google-cloud-sdk/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 2. 소스코드 서버에 복사

**방법 A: git clone**

```bash
git clone https://github.com/kaidenkim/gcp-audit.git /home/ec2-user/gcp-audit
cd /home/ec2-user/gcp-audit
```

**방법 B: S3 경유 배포 (로컬 → S3 → EC2)**

```bash
# [로컬] 파일 S3에 업로드
aws s3 cp gcp.py s3://<YOUR_BUCKET>/gcp-audit/gcp.py
aws s3 cp static/index.html s3://<YOUR_BUCKET>/gcp-audit/index.html

# [로컬] presigned URL 생성 (1800초 유효)
aws s3 presign s3://<YOUR_BUCKET>/gcp-audit/gcp.py --expires-in 1800
aws s3 presign s3://<YOUR_BUCKET>/gcp-audit/index.html --expires-in 1800

# [EC2 또는 SSM] presigned URL로 다운로드
curl -s -o /home/ec2-user/gcp-audit/gcp.py '<GCP_PRESIGNED_URL>'
curl -s -o /home/ec2-user/gcp-audit/static/index.html '<HTML_PRESIGNED_URL>'
```

**방법 C: AWS SSM을 통한 원격 배포**

```bash
# [로컬] URL 생성 후 SSM으로 EC2에 명령 전송
URL_GCP=$(aws s3 presign s3://<YOUR_BUCKET>/gcp-audit/gcp.py --expires-in 1800)
URL_HTML=$(aws s3 presign s3://<YOUR_BUCKET>/gcp-audit/index.html --expires-in 1800)

aws ssm send-command \
  --instance-ids "<EC2_INSTANCE_ID>" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "commands=[
    \"curl -s -o /home/ec2-user/gcp-audit/gcp.py '${URL_GCP}'\",
    \"curl -s -o /home/ec2-user/gcp-audit/static/index.html '${URL_HTML}'\",
    \"find /home/ec2-user/gcp-audit -name '*.json' -delete\",
    \"systemctl restart gcp-audit\",
    \"sleep 3 && systemctl is-active gcp-audit\"
  ]"
```

### 3. 의존성 설치

```bash
cd /home/ec2-user/gcp-audit
pip3 install -r requirements.txt
```

### 4. GCP 인증 설정

EC2에서 GCP API를 호출하려면 서비스 계정 키 또는 Workload Identity 설정이 필요합니다.

**방법 A: 서비스 계정 키 파일 사용**

```bash
# 서비스 계정 키를 EC2에 복사
scp service-account-key.json ec2-user@<EC2_IP>:~/gcp-audit/

# EC2에서 환경변수 설정
export GOOGLE_APPLICATION_CREDENTIALS=/home/ec2-user/gcp-audit/service-account-key.json
```

**방법 B: gcloud 인증 (개발/테스트용)**

```bash
gcloud auth application-default login --no-launch-browser
```

### 5. systemd 서비스 등록 (상시 기동)

```bash
sudo tee /etc/systemd/system/gcp-audit.service > /dev/null << 'EOF'
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
EOF

sudo systemctl daemon-reload
sudo systemctl enable gcp-audit
sudo systemctl start gcp-audit

# 상태 확인
sudo systemctl status gcp-audit
```

### 6. 서비스 관리 명령어

```bash
# 서비스 시작 / 중지 / 재시작
sudo systemctl start gcp-audit
sudo systemctl stop gcp-audit
sudo systemctl restart gcp-audit

# 로그 확인
sudo journalctl -u gcp-audit -f

# 캐시 초기화 (새 스캔 강제)
find /home/ec2-user/gcp-audit -name '*.json' -delete
# 또는 홈 디렉토리 캐시
rm -f ~/.gcp_audit_cache.json ~/.gcp_audit_resource_cache.json ~/.gcp_audit_billing_costs.json

sudo systemctl restart gcp-audit
```

브라우저에서 `http://<EC2_IP>:9090` 접속 (보안 그룹에서 포트 9090 허용 필요)

---

## 프로젝트 구조

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
    └── index.html    # 단일 페이지 앱 (SPA)
```

---

## API 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/` | 웹 UI (SPA) |
| `GET` | `/api/status` | 스캔 상태 및 결과 조회 |
| `POST` | `/api/scan` | 전체 프로젝트 스캔 시작 |
| `GET` | `/api/scan/stream` | 스캔 진행률 SSE 스트림 |
| `GET` | `/api/export` | Excel 파일 다운로드 |
| `POST` | `/api/projects/delete` | 프로젝트 삭제 |
| `POST` | `/api/resources/scan` | 리소스 스캔 시작 |
| `GET` | `/api/resources/stream` | 리소스 스캔 SSE 스트림 |
| `GET` | `/api/resources` | 리소스 스캔 결과 조회 |
| `GET` | `/api/billing/settings` | BigQuery 설정 조회 |
| `POST` | `/api/billing/settings` | BigQuery 설정 저장 |
| `POST` | `/api/billing/scan` | 비용 스캔 시작 |
| `GET` | `/api/billing/stream` | 비용 스캔 SSE 스트림 |
| `GET` | `/api/billing/costs` | 비용 스캔 결과 조회 |

---

## 트러블슈팅

### 스캔이 매우 느릴 때 (10분 이상)

GCP Cloud Billing API 할당량 초과 시 gRPC 클라이언트가 자동 재시도합니다. `gcp.py`에 이미 `retry=None, timeout=15` 및 Semaphore로 동시 요청 수를 제한하여 처리합니다.

### `billing_open` (OPEN/CLOSED) 이 표시되지 않을 때

전체 스캔 캐시를 삭제하고 재스캔합니다.

```bash
rm -f ~/.gcp_audit_cache.json
```

### 소유자가 표시되지 않을 때

`get_iam_policy`는 프로젝트에 직접 부여된 권한만 반환합니다. 상위 조직/폴더에서 상속된 IAM은 표시되지 않습니다.
`roles/owner` 및 `roles/editor` 역할의 비시스템 멤버를 모두 표시합니다.
