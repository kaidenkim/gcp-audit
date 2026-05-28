from __future__ import annotations
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# GCP Python SDK
from google.cloud import resourcemanager_v3, billing_v1
from google.api_core import exceptions as api_errors
import google.auth
import google.auth.transport.requests


# ── 토큰 버킷 레이트 리미터 ────────────────────────────────────────────
# [문제] Semaphore(N)은 동시 요청 수만 제한하고 '초당 요청 수'는 제어 못함.
#        API 응답이 0.3s이면 Semaphore(12) = 40 req/s = 2400 req/min
#        → 쿼터(700/min) 3배 초과 → 일부 호출 무작위 실패 → 스캔마다 결과 달라짐
#
# [해결] 토큰 버킷으로 '분당 요청 수'를 정확히 제어.
#        acquire()가 필요한 만큼 sleep 후 반환 → 항상 쿼터 이하 유지.
#
# 스캔 소요 시간 추정 (780 프로젝트):
#   빌링 조회: 780 / 500 * 60 ≈ 94s
#   IAM 조회:  780 / 500 * 60 ≈ 94s  (동시 실행)
#   총 스캔:   ≈ 100~120s (약 2분)  — 쿼터 초과 없이 안정적
class _TokenBucketLimiter:
    """스레드 안전 토큰 버킷 레이트 리미터.

    acquire() 는 다음 허용 시각까지 sleep 한 뒤 반환한다.
    여러 스레드가 동시에 호출하면 순서대로 슬롯을 배정받아 대기한다.
    """
    def __init__(self, rate_per_minute: int) -> None:
        self._interval: float = 60.0 / rate_per_minute
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed:
                    self._next_allowed = now + self._interval
                    return
                wait = self._next_allowed - now
                self._next_allowed += self._interval
            time.sleep(wait)


# Cloud Billing API: 쿼터 ~700 req/min → 500 req/min 제한 (여유 200/min)
_BILLING_LIM = _TokenBucketLimiter(500)
# Resource Manager getIamPolicy: 쿼터 ~600 req/min → 500 req/min 제한
_OWNER_LIM   = _TokenBucketLimiter(500)

# ── gcloud (auth 표시 및 리소스 스캔용으로만 유지) ─────────────────────
_GCLOUD_CANDIDATES = [
    "gcloud",
    "/usr/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/local/google-cloud-sdk/bin/gcloud",
    "/opt/google-cloud-sdk/bin/gcloud",
    "/opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    str(Path.home() / "google-cloud-sdk/bin/gcloud"),
    "/home/ec2-user/google-cloud-sdk/bin/gcloud",
    "/root/google-cloud-sdk/bin/gcloud",
]

_gcloud_bin: str | None = None
_creds_cache: object | None = None
_creds_cache_ts: float = 0.0      # gcloud 토큰 발급 시각 (ADC는 자체 갱신하므로 사용 안 함)
_GCLOUD_TOKEN_TTL = 50 * 60       # 50분 (gcloud 토큰 유효기간 60분에서 여유분 10분)
_creds_lock = threading.Lock()


def _find_gcloud() -> str:
    for path in _GCLOUD_CANDIDATES:
        try:
            subprocess.run([path, "version"], capture_output=True, timeout=5)
            return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("gcloud CLI를 찾을 수 없습니다. gcloud auth login을 먼저 실행하세요.")


def _gcloud(*args, timeout: int = 20) -> tuple[str, str, int]:
    global _gcloud_bin
    if _gcloud_bin is None:
        _gcloud_bin = _find_gcloud()
    r = subprocess.run([_gcloud_bin] + list(args), capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def get_auth_info() -> dict | None:
    stdout, _, rc = _gcloud("auth", "list", "--format=json")
    if rc != 0:
        return None
    try:
        accounts = json.loads(stdout)
        return next((a for a in accounts if a.get("status") == "ACTIVE"), None)
    except Exception:
        return None


# ── SDK 인증: ADC 우선, 실패 시 gcloud 토큰 폴백 ─────────────────────
def _get_credentials():
    global _creds_cache, _creds_cache_ts
    with _creds_lock:
        # ADC credentials: 자체 expired 속성으로 판단
        # gcloud 토큰: expired 속성이 없으므로 발급 시각 기반 TTL로 판단
        if _creds_cache is not None:
            try:
                is_adc = getattr(_creds_cache, 'refresh_token', None) is not None \
                         or type(_creds_cache).__name__ != 'Credentials'
                if is_adc:
                    # ADC: expired 속성이 False이면 유효
                    if not getattr(_creds_cache, 'expired', False):
                        return _creds_cache
                else:
                    # gcloud 토큰: 50분 TTL
                    if time.time() - _creds_cache_ts < _GCLOUD_TOKEN_TTL:
                        return _creds_cache
            except Exception:
                pass
            _creds_cache = None

        # 1순위: Application Default Credentials (service account key, ADC 등)
        try:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            req = google.auth.transport.requests.Request()
            creds.refresh(req)
            _creds_cache = creds
            return creds
        except Exception:
            pass

        # 2순위: gcloud access token 폴백 (EC2 등 ADC 미설정 환경)
        from google.oauth2.credentials import Credentials as OAuthCreds
        stdout, _, rc = _gcloud("auth", "print-access-token", timeout=15)
        token = stdout.strip()
        if rc != 0 or not token:
            raise RuntimeError(
                "GCP 인증 실패. `gcloud auth login` 또는 "
                "`gcloud auth application-default login` 실행 필요"
            )
        creds = OAuthCreds(
            token=token,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _creds_cache = creds
        _creds_cache_ts = time.time()    # 발급 시각 기록 → 50분 후 자동 재발급
        return creds


# ── 프로젝트 목록 (SDK) ───────────────────────────────────────────────
def fetch_projects() -> list[dict]:
    creds = _get_credentials()
    client = resourcemanager_v3.ProjectsClient(credentials=creds)
    projects = []
    # search_projects: parent 불필요, 접근 가능한 전체 프로젝트 반환
    # page_size=1000 → 780개를 1회 호출로 수신 (기본 100개씩 8회 → 12초 → 1-2초)
    req = resourcemanager_v3.SearchProjectsRequest(page_size=1000)
    for proj in client.search_projects(request=req):
        pid = proj.project_id
        if pid.startswith("sys-"):
            continue
        if proj.state != resourcemanager_v3.Project.State.ACTIVE:
            continue
        ct = proj.create_time.strftime("%Y-%m-%dT%H:%M:%SZ") if proj.create_time else ""
        projects.append({
            "project_id": pid,
            "name":        proj.display_name,
            "create_time": ct,
        })
    return projects


# ── 빌링 상태 (SDK) ───────────────────────────────────────────────────
def _check_billing(pid: str, client: billing_v1.CloudBillingClient) -> dict:
    _BILLING_LIM.acquire()   # 500 req/min 이하로 속도 제한
    try:
        info = client.get_project_billing_info(
            name=f"projects/{pid}", timeout=15, retry=None
        )
        bid = ""
        if info.billing_account_name:
            bid = info.billing_account_name.replace("billingAccounts/", "")
        return {
            "billing_enabled":    "True" if info.billing_enabled else "False",
            "billing_account_id": bid,
        }
    except (api_errors.PermissionDenied, api_errors.NotFound):
        return {"billing_enabled": "False", "billing_account_id": ""}
    except Exception:
        return {"billing_enabled": "False", "billing_account_id": ""}


# ── 소유자 조회 (SDK) ─────────────────────────────────────────────────
def _is_system_sa(member: str) -> bool:
    """GCP 자동 생성 시스템 서비스 계정 여부 판별.

    포함 예: service-123456@gcp-sa-xxx.iam.gserviceaccount.com,
             123456@cloudservices.gserviceaccount.com
    제외 예: myapp@appspot.gserviceaccount.com,
             mysa@project.iam.gserviceaccount.com (사용자 생성)
    """
    if not member.startswith("serviceAccount:"):
        return False
    sa = member[len("serviceAccount:"):]
    prefix = sa.split("@")[0]
    # 'service-숫자' 또는 순수 숫자 형태 → GCP 시스템 SA
    return bool(re.match(r"^service-\d+$", prefix) or re.match(r"^\d+$", prefix))


def _get_owners(pid: str, client: resourcemanager_v3.ProjectsClient) -> list[str]:
    """roles/owner + roles/editor 중 비시스템 멤버 반환.

    roles/owner만 보면 App Engine 기본 SA 등 editor 레벨 실사용자를 놓침.
    단, GCP 자동 생성 시스템 SA(service-숫자@, 숫자@)는 제외.
    """
    _OWNER_LIM.acquire()     # 500 req/min 이하로 속도 제한
    try:
        policy = client.get_iam_policy(
            resource=f"projects/{pid}", timeout=15, retry=None
        )
        members: list[str] = []
        for binding in policy.bindings:
            if binding.role in ("roles/owner", "roles/editor"):
                for m in binding.members:
                    if not _is_system_sa(m):
                        members.append(m)
        return list(dict.fromkeys(members))  # 순서 유지 + 중복 제거
    except (api_errors.PermissionDenied, api_errors.NotFound):
        return []
    except Exception:
        return []


# ── 빌링 계정 정보 (SDK) ──────────────────────────────────────────────
def _get_billing_account(bid: str, client: billing_v1.CloudBillingClient) -> dict:
    """단일 빌링 계정 조회 (billing.accounts.get 권한 필요)."""
    if not bid:
        return {"name": "", "open": ""}
    try:
        acc = client.get_billing_account(name=f"billingAccounts/{bid}")
        return {
            "name": acc.display_name,
            "open": str(acc.open),
        }
    except Exception:
        return {"name": "", "open": ""}


def _list_all_billing_accounts(client: billing_v1.CloudBillingClient) -> dict[str, dict]:
    """접근 가능한 모든 빌링 계정 목록 조회 (단일 API 호출, billing.accounts.list 권한).

    get_billing_account()는 계정별 billing.accounts.get 권한 필요 → 대부분 실패.
    list_billing_accounts()는 한 번 호출로 접근 가능한 전체 계정 반환 → OPEN/CLOSED 포함.
    Stage 2 병렬 요청(780개) 전에 선제 호출해야 quota 소진을 피할 수 있음.
    """
    result = {}
    try:
        for acc in client.list_billing_accounts():
            bid = acc.name.replace("billingAccounts/", "")
            result[bid] = {
                "name": acc.display_name,
                "open": str(acc.open),
            }
    except Exception:
        pass
    return result


# ── 리소스 조회 (gcloud 유지 — 수동 실행, 빈도 낮음) ─────────────────
_RESOURCE_CHECKS: dict[str, list[str]] = {
    "vm":          ["compute", "instances", "list", "--format=value(name)", "--quiet"],
    "run":         ["run", "services", "list", "--platform=managed", "--format=value(metadata.name)", "--quiet"],
    "functions":   ["functions", "list", "--format=value(name)", "--quiet"],
    "gke":         ["container", "clusters", "list", "--format=value(name)", "--quiet"],
    "storage":     ["storage", "buckets", "list", "--format=value(name)"],
    "sql":         ["sql", "instances", "list", "--format=value(name)", "--quiet"],
    "pubsub":      ["pubsub", "topics", "list", "--format=value(name)", "--quiet"],
    "vpc":         ["compute", "networks", "list", "--format=value(name)", "--quiet"],
    "lb":          ["compute", "forwarding-rules", "list", "--format=value(name)", "--quiet"],
    "armor":       ["compute", "security-policies", "list", "--format=value(name)", "--quiet"],
    "marketplace": ["deployment-manager", "deployments", "list", "--format=value(name)", "--quiet"],
}


def _count_lines(stdout: str) -> int:
    return len([l for l in stdout.strip().splitlines() if l.strip()])


def get_project_resources(pid: str) -> dict:
    def check(key: str, base_args: list[str]) -> tuple[str, int]:
        try:
            stdout, _, _ = _gcloud(*base_args, f"--project={pid}", timeout=25)
            return key, _count_lines(stdout)
        except Exception:
            return key, 0

    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(check, k, v): k for k, v in _RESOURCE_CHECKS.items()}
        for f in as_completed(futs):
            key, cnt = f.result()
            results[key] = cnt
    return results


def scan_billing_resources(billing_projects: list[dict], on_progress) -> list[dict]:
    total = len(billing_projects)
    on_progress(2, f"리소스 조회 준비 중... (빌링 프로젝트 {total}개)", 0, total)

    def scan_one(p: dict) -> dict:
        resources = get_project_resources(p["project_id"])
        return {**p, "resources": resources, "total_resources": sum(resources.values())}

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(scan_one, p): p for p in billing_projects}
        done = 0
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            on_progress(int(done / total * 98) + 1,
                        f"리소스 조회 중... ({done}/{total})", done, total)

    on_progress(100, "리소스 스캔 완료!", total, total)
    return sorted(results, key=lambda x: -x["total_resources"])


# ── 프로젝트 삭제 (SDK) ───────────────────────────────────────────────
def delete_project(pid: str) -> tuple[bool, str]:
    try:
        creds = _get_credentials()
        client = resourcemanager_v3.ProjectsClient(credentials=creds)
        client.delete_project(name=f"projects/{pid}")
        return True, "삭제 완료"
    except api_errors.PermissionDenied:
        return False, "권한 없음 (resourcemanager.projects.delete 필요)"
    except Exception as e:
        return False, str(e)


# ── 전체 스캔 (SDK — subprocess 없음, 고병렬 가능) ─────────────────────
def _deletable(billing_enabled: str, billing_account_id: str, owners: list[str], name: str) -> str:
    is_default = not name or name == "My First Project" or name.startswith("My Project")
    # 빌링 활성(OPEN) 또는 CLOSED라도 빌링 계정이 연결된 경우 → 삭제 전 확인 필요
    if billing_enabled == "True" or billing_account_id:
        return "빌링연결"
    # 사람 계정(user:, group:)이 없으면 소유자 확인 필요
    # serviceAccount만 있는 경우(예: App Engine SA)는 담당자 불명으로 처리
    human_owners = [o for o in owners if o.startswith("user:") or o.startswith("group:")]
    if not human_owners and not is_default:
        return "소유자 확인 필요"
    return "즉시 삭제 가능"


def full_scan(on_progress) -> list[dict]:
    """
    프로젝트 목록 스트리밍과 빌링/소유자 조회를 완전히 겹쳐서 실행.
    search_projects() 페이지가 도착하는 즉시 해당 프로젝트의 task를 제출 →
    목록 조회가 끝날 때쯤 billing/owner 조회도 거의 완료.

    빌링 계정 OPEN/CLOSED: Stage 2 병렬 요청이 700 req/min 할당량을 소진하기 전에
    list_billing_accounts()를 먼저 호출하여 미리 캐싱.
    """
    on_progress(2, "인증 초기화 중...", 0, 0)
    creds = _get_credentials()

    # gRPC 클라이언트 풀 — 병렬 생성으로 채널 핸드셰이크 시간 단축
    # s3_client: Stage 3용 전용 클라이언트 (Stage 2 quota 소진 후 재사용 방지)
    _N = 6
    with ThreadPoolExecutor(max_workers=_N * 2 + 2) as _init_ex:
        _bill_futs = [_init_ex.submit(billing_v1.CloudBillingClient, credentials=creds) for _ in range(_N)]
        _rm_futs   = [_init_ex.submit(resourcemanager_v3.ProjectsClient, credentials=creds) for _ in range(_N)]
        _list_fut  = _init_ex.submit(resourcemanager_v3.ProjectsClient, credentials=creds)
        _s3_fut    = _init_ex.submit(billing_v1.CloudBillingClient, credentials=creds)
        bill_clients = [f.result() for f in _bill_futs]
        rm_clients   = [f.result() for f in _rm_futs]
        list_client  = _list_fut.result()
        s3_client    = _s3_fut.result()

    # Stage 2 전 선제 조회: 할당량 소진 전에 빌링 계정 OPEN/CLOSED 정보 캐싱
    on_progress(3, "빌링 계정 목록 사전 조회 중...", 0, 0)
    account_cache: dict[str, dict] = _list_all_billing_accounts(s3_client)

    billing_map: dict[str, dict] = {}
    owner_map:   dict[str, list[str]] = {}
    projects:    list[dict] = []
    _lock = threading.Lock()
    _done = {"b": 0, "o": 0}

    def _billing_task(pid: str, idx: int) -> None:
        r = _check_billing(pid, bill_clients[idx % _N])
        with _lock:
            billing_map[pid] = r
            _done["b"] += 1
            t = max(len(projects), 1)
            on_progress(5 + int((_done["b"] + _done["o"]) / (t * 2) * 88),
                        f"조회 중... 빌링 {_done['b']}/{t}  소유자 {_done['o']}/{t}",
                        _done["b"] + _done["o"], t * 2)

    def _owner_task(pid: str, idx: int) -> None:
        r = _get_owners(pid, rm_clients[idx % _N])
        with _lock:
            owner_map[pid] = r
            _done["o"] += 1
            t = max(len(projects), 1)
            on_progress(5 + int((_done["b"] + _done["o"]) / (t * 2) * 88),
                        f"조회 중... 빌링 {_done['b']}/{t}  소유자 {_done['o']}/{t}",
                        _done["b"] + _done["o"], t * 2)

    on_progress(3, "프로젝트 스트리밍 + 빌링/소유자 병렬 조회 시작...", 0, 0)

    # search_projects 페이지 도착 즉시 billing/owner task 제출 (page_size=100 기본값 유지 → 첫 페이지 빨리 도착)
    with ThreadPoolExecutor(max_workers=200) as ex:
        futures = []
        for i, proj in enumerate(list_client.search_projects()):
            pid = proj.project_id
            if pid.startswith("sys-"):
                continue
            if proj.state != resourcemanager_v3.Project.State.ACTIVE:
                continue
            ct = proj.create_time.strftime("%Y-%m-%dT%H:%M:%SZ") if proj.create_time else ""
            p = {"project_id": pid, "name": proj.display_name, "create_time": ct}
            with _lock:
                projects.append(p)
            futures.append(ex.submit(_billing_task, pid, i))
            futures.append(ex.submit(_owner_task,   pid, i))

        total = len(projects)
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    # Stage 3 – 사전 조회에서 못 가져온 빌링 계정 보완
    # (Stage 2 quota 소진 후이므로 적은 수만 개별 조회 시도)
    on_progress(93, "빌링 계정 정보 보완 중...", 0, 0)
    bid_set = {v["billing_account_id"] for v in billing_map.values() if v["billing_account_id"]}
    missing_bids = bid_set - set(account_cache.keys())
    if missing_bids:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_get_billing_account, bid, s3_client): bid for bid in missing_bids}
            for f in as_completed(futs):
                info = f.result()
                if info["name"] or info["open"]:
                    account_cache[futs[f]] = info

    # 결과 조합
    on_progress(95, "데이터 조합 중...", 0, 0)
    result = []
    for p in projects:
        pid    = p["project_id"]
        name   = p.get("name", "").strip()
        b      = billing_map.get(pid, {"billing_enabled": "False", "billing_account_id": ""})
        owners = owner_map.get(pid, [])
        bid    = b["billing_account_id"]
        binfo  = account_cache.get(bid, {"name": "", "open": ""})
        result.append({
            "project_id":           pid,
            "name":                 name,
            "create_time":          (p.get("create_time") or "")[:10],
            "billing_enabled":      b["billing_enabled"],
            "billing_account_id":   bid,
            "billing_account_name": binfo["name"],
            "billing_open":         binfo["open"],
            "owners":               owners,
            "deletable":            _deletable(b["billing_enabled"], bid, owners, name),
        })

    result.sort(key=lambda x: x["create_time"])
    on_progress(100, "스캔 완료!", total, total)
    return result
