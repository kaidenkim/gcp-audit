from __future__ import annotations
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# GCP Python SDK
from google.cloud import resourcemanager_v3
from google.api_core import exceptions as api_errors
import google.auth
import google.auth.transport.requests
from requests.adapters import HTTPAdapter


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

    acquire() 는 자신의 슬롯을 한 번 배정받아 필요한 만큼 sleep 후 반환한다.
    여러 스레드가 동시에 호출해도 각자 독립된 슬롯을 배정받으므로 기아(starvation) 없음.

    [버그 수정] while True 루프를 제거.
    이전 구현에서 200개 스레드가 동시에 acquire()를 호출하면:
      - 각 스레드가 슬롯을 배정받고 sleep 하는 동안
      - 다른 스레드들이 _next_allowed를 계속 앞으로 밀어냄
      - 슬롯을 배정받은 스레드가 깨어나도 _next_allowed가 이미 훨씬 앞에 있어 또 sleep
      - → 무한 재sleep → 사실상 1~2개 스레드만 실행되는 기아 발생
    슬롯을 배정받으면 한 번만 sleep 하고 반환해야 함.
    """
    def __init__(self, rate_per_minute: int) -> None:
        self._interval: float = 60.0 / rate_per_minute
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now >= self._next_allowed:
                # 슬롯 즉시 사용 가능 → 대기 없이 반환
                self._next_allowed = now + self._interval
                return
            # 슬롯 배정: _next_allowed가 이 스레드의 실행 시각
            wait = self._next_allowed - now
            self._next_allowed += self._interval
        # 락 밖에서 sleep — 배정받은 슬롯까지만 1회 대기, 루프 없음
        time.sleep(wait)


# Cloud Billing API: 쿼터 ~700 req/min → 650 req/min 제한 (여유 50/min)
# 500→650: 500프로젝트 기준 60s → 46s (약 14s 단축)
_BILLING_LIM = _TokenBucketLimiter(650)
# Resource Manager getIamPolicy: 쿼터 ~600 req/min → 570 req/min 제한 (여유 30/min)
# 500→570: 500프로젝트 기준 60s → 53s (약 7s 단축)
_OWNER_LIM   = _TokenBucketLimiter(570)

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


# ── 빌링 상태 (REST API) ──────────────────────────────────────────────
# gRPC SDK(billing_v1)는 gcloud 토큰 폴백 환경(EC2 등)에서 ACCESS_TOKEN_TYPE_UNSUPPORTED
# 오류가 발생한다. AuthorizedSession은 토큰을 Bearer 헤더로 전달하므로 항상 호환됨.
def _check_billing(pid: str, session) -> dict:
    """프로젝트 빌링 정보 REST 조회. 일시적 오류 시 최대 3회 재시도(5s 간격).

    반환 dict의 `_failed` 키:
      False → 정상 응답(빌링 없음 포함) 또는 영구 오류(권한 없음) → Stage 4 재시도 불필요
      True  → 3회 모두 일시적 예외 → Stage 4 재시도 대상
    """
    url = f"https://cloudbilling.googleapis.com/v1/projects/{pid}/billingInfo"
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        _BILLING_LIM.acquire()
        try:
            r = session.get(url, timeout=15)
            if r.status_code in (403, 404):
                return {"billing_enabled": "False", "billing_account_id": "", "_failed": False}
            r.raise_for_status()
            data = r.json()
            bid = data.get("billingAccountName", "").replace("billingAccounts/", "")
            return {
                "billing_enabled":    "True" if data.get("billingEnabled") else "False",
                "billing_account_id": bid,
                "_failed":            False,
            }
        except Exception:
            pass
    return {"billing_enabled": "False", "billing_account_id": "", "_failed": True}


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


_OWNERS_FAILED = object()   # 일시적 오류로 3회 모두 실패한 경우의 sentinel

def _get_owners(pid: str, client: resourcemanager_v3.ProjectsClient):
    """roles/owner + roles/editor 중 비시스템 멤버 반환. 일시적 오류 시 최대 3회 재시도.

    반환:
      list[str]      → 정상 응답 (빈 리스트 포함) 또는 영구 오류(권한 없음)
      _OWNERS_FAILED → 3회 모두 일시적 예외 → Stage 4 재시도 대상

    roles/owner만 보면 App Engine 기본 SA 등 editor 레벨 실사용자를 놓침.
    단, GCP 자동 생성 시스템 SA(service-숫자@, 숫자@)는 제외.
    """
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        _OWNER_LIM.acquire()
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
            pass
    return _OWNERS_FAILED


# ── 빌링 계정 정보 (REST API) ─────────────────────────────────────────
def _get_billing_account(bid: str, session) -> dict:
    """단일 빌링 계정 REST 조회."""
    if not bid:
        return {"name": "", "open": ""}
    try:
        r = session.get(f"https://cloudbilling.googleapis.com/v1/billingAccounts/{bid}", timeout=15)
        if r.status_code in (400, 403, 404):
            return {"name": "", "open": ""}
        r.raise_for_status()
        acc = r.json()
        return {
            "name": acc.get("displayName", ""),
            "open": str(acc.get("open", "")),
        }
    except Exception:
        return {"name": "", "open": ""}


def _list_all_billing_accounts(session) -> dict[str, dict]:
    """Cloud Billing REST API로 접근 가능한 모든 빌링 계정 목록 조회.

    gRPC SDK 대신 REST 사용: gcloud 토큰 폴백 환경(EC2)에서도 정상 동작.
    """
    result = {}
    try:
        url: str | None = "https://cloudbilling.googleapis.com/v1/billingAccounts"
        while url:
            r = session.get(url, timeout=15)
            if r.status_code in (400, 403, 404):
                return result
            r.raise_for_status()
            data = r.json()
            for acc in data.get("billingAccounts", []):
                bid = acc.get("name", "").replace("billingAccounts/", "")
                if bid:
                    result[bid] = {
                        "name": acc.get("displayName", ""),
                        "open": str(acc.get("open", "")),
                    }
            pt = data.get("nextPageToken")
            url = f"https://cloudbilling.googleapis.com/v1/billingAccounts?pageToken={pt}" if pt else None
    except Exception:
        pass
    return result


# ── 리소스 조회 (REST API — gcloud CLI 대신 직접 호출로 교체) ──────────
# gcloud CLI는 매 호출마다 Python 인터프리터를 새로 띄워 느리고 CPU 부하가 크다.
# google.auth.transport.requests.AuthorizedSession을 재사용해 HTTP 연결을 공유한다.

def _res_count_list(session, url: str, key: str) -> int:
    """단순 목록 API (items/services/clusters 등) 항목 수 반환."""
    try:
        r = session.get(url, timeout=15)
        if r.status_code in (400, 403, 404):
            return 0
        r.raise_for_status()
        val = r.json().get(key)
        return len(val) if isinstance(val, list) else 0
    except Exception:
        return 0


def _res_count_aggregated(session, url: str, inner_key: str) -> int:
    """Compute aggregated list (items.{zone}.{inner_key}) 항목 수 반환."""
    try:
        r = session.get(url, timeout=15)
        if r.status_code in (400, 403, 404):
            return 0
        r.raise_for_status()
        zones = r.json().get("items", {})
        return sum(
            len(v.get(inner_key, []))
            for v in zones.values()
            if isinstance(v, dict)
        )
    except Exception:
        return 0


def get_project_resources(pid: str, session) -> dict:
    """REST API로 프로젝트 리소스 조회. session = AuthorizedSession (스캔 전체 공유)."""
    compute = f"https://compute.googleapis.com/compute/v1/projects/{pid}"

    checks: dict[str, tuple] = {
        "vm":          (_res_count_aggregated, f"{compute}/aggregated/instances",          "instances"),
        "run":         (_res_count_list,       f"https://run.googleapis.com/v2/projects/{pid}/locations/-/services", "services"),
        "functions":   (_res_count_list,       f"https://cloudfunctions.googleapis.com/v2/projects/{pid}/locations/-/functions", "functions"),
        "gke":         (_res_count_list,       f"https://container.googleapis.com/v1/projects/{pid}/locations/-/clusters", "clusters"),
        "storage":     (_res_count_list,       f"https://storage.googleapis.com/storage/v1/b?project={pid}", "items"),
        "sql":         (_res_count_list,       f"https://sqladmin.googleapis.com/v1/projects/{pid}/instances", "items"),
        "pubsub":      (_res_count_list,       f"https://pubsub.googleapis.com/v1/projects/{pid}/topics", "topics"),
        "vpc":         (_res_count_list,       f"{compute}/global/networks",               "items"),
        "lb":          (_res_count_aggregated, f"{compute}/aggregated/forwardingRules",    "forwardingRules"),
        "armor":       (_res_count_list,       f"{compute}/global/securityPolicies",       "items"),
        "marketplace": (_res_count_list,       f"https://www.googleapis.com/deploymentmanager/v2/projects/{pid}/global/deployments", "deployments"),
    }

    results: dict[str, int] = {}
    # max_workers=11: 11개 엔드포인트를 전부 동시에 요청 (6→11, 2배치→1배치)
    with ThreadPoolExecutor(max_workers=11) as ex:
        futs = {ex.submit(fn, session, url, key): rk
                for rk, (fn, url, key) in checks.items()}
        for f in as_completed(futs):
            results[futs[f]] = f.result()
    return results


def scan_billing_resources(billing_projects: list[dict], on_progress) -> list[dict]:
    total = len(billing_projects)
    on_progress(2, f"리소스 조회 준비 중... (빌링 프로젝트 {total}개)", 0, total)

    # AuthorizedSession 한 개를 전체 스캔에서 공유 → HTTP 연결 풀 재사용
    session = google.auth.transport.requests.AuthorizedSession(_get_credentials())
    # 연결 풀 확장: max_workers=40 × inner 11 = 최대 440 동시 연결 대비
    _adapter = HTTPAdapter(pool_connections=100, pool_maxsize=500)
    session.mount("https://", _adapter)

    def scan_one(p: dict) -> dict:
        resources = get_project_resources(p["project_id"], session)
        return {**p, "resources": resources, "total_resources": sum(resources.values())}

    results: list[dict] = []
    # max_workers 20→40: 500프로젝트 기준 ~70s → ~35s
    with ThreadPoolExecutor(max_workers=40) as ex:
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

    # Billing API: REST 세션 사용 (gRPC billing_v1은 EC2 gcloud 토큰과 호환 불가)
    # AuthorizedSession은 Bearer 헤더로 토큰을 전달 → 모든 환경에서 동작
    _N = 10
    session = google.auth.transport.requests.AuthorizedSession(creds)
    _s_adapter = HTTPAdapter(pool_connections=50, pool_maxsize=200)
    session.mount("https://", _s_adapter)

    # gRPC 클라이언트 풀 — IAM(소유자) 조회 전용, _N+1개 병렬 생성
    with ThreadPoolExecutor(max_workers=_N + 1) as _init_ex:
        _rm_futs   = [_init_ex.submit(resourcemanager_v3.ProjectsClient, credentials=creds) for _ in range(_N)]
        _list_fut  = _init_ex.submit(resourcemanager_v3.ProjectsClient, credentials=creds)
        rm_clients   = [f.result() for f in _rm_futs]
        list_client  = _list_fut.result()

    # 빌링 계정 목록을 백그라운드에서 병렬 조회 시작
    # → 직렬 1-3s 낭비 제거: 메인 스캔(60s+)과 완전히 겹쳐서 실행
    _bg_ex = ThreadPoolExecutor(max_workers=1)
    _account_cache_future = _bg_ex.submit(_list_all_billing_accounts, s3_client)

    billing_map: dict[str, dict] = {}
    owner_map:   dict[str, list[str]] = {}
    projects:    list[dict] = []
    _lock = threading.Lock()
    _done = {"b": 0, "o": 0}

    def _billing_task(pid: str, idx: int) -> None:
        r = _check_billing(pid, session)
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

    # page_size=1000: 500개 프로젝트를 1회 호출로 수신 (기본 100개씩 5회 → ~8s 낭비 제거)
    _search_req = resourcemanager_v3.SearchProjectsRequest(page_size=1000)
    with ThreadPoolExecutor(max_workers=200) as ex:
        futures = []
        for i, proj in enumerate(list_client.search_projects(request=_search_req)):
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

    # 백그라운드로 실행한 billing accounts 목록 결과 수집 (메인 스캔과 완전히 겹쳐 실행됨)
    account_cache: dict[str, dict] = _account_cache_future.result()
    _bg_ex.shutdown(wait=False)

    # Stage 3 – 사전 조회에서 못 가져온 빌링 계정 OPEN/CLOSED 보완
    on_progress(90, "빌링 계정 OPEN/CLOSED 보완 중...", 0, 0)
    bid_set = {v["billing_account_id"] for v in billing_map.values() if v["billing_account_id"]}
    missing_bids = bid_set - set(account_cache.keys())
    if missing_bids:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_get_billing_account, bid, session): bid for bid in missing_bids}
            for f in as_completed(futs):
                info = f.result()
                if info["name"] or info["open"]:
                    account_cache[futs[f]] = info

    # Stage 4 – 실패 프로젝트 보완 패스
    # Stage 2의 3회 재시도에도 불구하고 일시적 오류로 실패한 프로젝트만 재조회.
    # (빌링 미연결 / 권한 없음 등 정상 응답은 재시도 불필요 → _failed 플래그로 구분)
    failed_billing = [pid for pid, v in billing_map.items() if v.get("_failed")]
    failed_owners  = [pid for pid, v in owner_map.items() if v is _OWNERS_FAILED]

    # owner_map에 sentinel이 남아 있으면 빈 리스트로 초기화 (결과 조합 단계에서 안전하게 처리)
    for pid in failed_owners:
        owner_map[pid] = []

    retry_pids = list(dict.fromkeys(failed_billing + failed_owners))
    if retry_pids:
        on_progress(93, f"실패 프로젝트 재조회 중... ({len(retry_pids)}개)", 0, 0)
        with ThreadPoolExecutor(max_workers=4) as ex:
            b_futs = {ex.submit(_check_billing, pid, session): pid for pid in failed_billing}
            o_futs = {ex.submit(_get_owners,    pid, rm_clients[0]): pid for pid in failed_owners}
            for f in as_completed({**b_futs, **o_futs}):
                pid = (b_futs if f in b_futs else o_futs)[f]
                res = f.result()
                if f in b_futs:
                    billing_map[pid] = res   # _failed 포함하지만 결과 조합 시 무시됨
                elif f in o_futs and res is not _OWNERS_FAILED:
                    owner_map[pid] = res

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
