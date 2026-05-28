import json
import urllib.request
import urllib.error
from pathlib import Path

SETTINGS_FILE = Path.home() / ".gcp_audit_billing_settings.json"


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {"bq_project": "", "bq_dataset": ""}


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False))


def fetch_costs(bq_project: str, bq_dataset: str, on_progress=None) -> dict:
    """
    BigQuery 빌링 익스포트를 쿼리하여 프로젝트별 서비스별 비용 반환.
    Returns: {project_id: {"_total": float, "_currency": str,
              "_month": str, "_monthly": {month: total}, service: float, ...}}
    """
    try:
        from google.cloud import bigquery
    except ImportError:
        raise RuntimeError(
            "google-cloud-bigquery 패키지가 필요합니다.\n"
            "pip install google-cloud-bigquery"
        )

    if on_progress:
        on_progress(5, "BigQuery 클라이언트 초기화 중...")
    client = bigquery.Client()

    if on_progress:
        on_progress(10, "빌링 익스포트 테이블 탐색 중...")
    try:
        tables = list(client.list_tables(f"{bq_project}.{bq_dataset}"))
    except Exception as e:
        raise RuntimeError(
            f"데이터셋 접근 실패: {bq_project}.{bq_dataset}\n{e}"
        )

    billing_tables = [
        t.table_id for t in tables
        if "gcp_billing_export" in t.table_id.lower()
    ]
    if not billing_tables:
        raise RuntimeError(
            f"gcp_billing_export 테이블 없음: {bq_project}.{bq_dataset}\n"
            "Cloud Billing → 빌링 익스포트 → BigQuery로 내보내기를 설정하세요."
        )

    table_path = f"`{bq_project}.{bq_dataset}.{billing_tables[0]}`"
    if on_progress:
        on_progress(20, f"쿼리 중: {billing_tables[0]}")

    query = f"""
    SELECT
      COALESCE(project.id, '_unknown') AS project_id,
      service.description               AS service,
      ROUND(SUM(cost), 2)               AS total_cost,
      MIN(currency)                     AS currency,
      FORMAT_DATE('%Y-%m', DATE(usage_start_time)) AS month
    FROM {table_path}
    WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
      AND cost > 0
    GROUP BY project_id, service, month
    ORDER BY project_id, month DESC, total_cost DESC
    """

    if on_progress:
        on_progress(30, "쿼리 실행 중... (수십 초 소요될 수 있습니다)")
    try:
        rows = list(client.query(query).result())
    except Exception as e:
        raise RuntimeError(f"BigQuery 쿼리 실패: {e}")

    if on_progress:
        on_progress(85, f"{len(rows):,}행 데이터 처리 중...")

    # pid -> month -> {svc: cost}
    monthly: dict = {}
    for row in rows:
        pid = row.project_id
        svc = row.service or "기타"
        cost = float(row.total_cost or 0)
        month = row.month or ""
        monthly.setdefault(pid, {}).setdefault(month, {})
        monthly[pid][month][svc] = monthly[pid][month].get(svc, 0) + cost

    cost_map: dict = {}
    for pid, months in monthly.items():
        sorted_m = sorted(months.keys(), reverse=True)
        latest = sorted_m[0] if sorted_m else ""
        svcs = months.get(latest, {})
        total = sum(svcs.values())
        cost_map[pid] = {
            "_total":    round(total, 2),
            "_currency": "USD",
            "_month":    latest,
            "_monthly":  {m: round(sum(s.values()), 2) for m, s in months.items()},
            **{k: round(v, 2) for k, v in svcs.items()},
        }

    if on_progress:
        on_progress(100, "빌링 스캔 완료!")
    return cost_map


# ── 인보이스 모드 (BigQuery 없이) ────────────────────────────────────

def _get_access_token() -> str:
    from gcp import _gcloud
    stdout, _, _ = _gcloud("auth", "print-access-token")
    return stdout.strip()


def _parse_money(amt: dict) -> float:
    """Google Money 타입(units + nanos)을 float으로 변환."""
    units = int(amt.get("units") or 0)
    nanos = int(amt.get("nanos") or 0)
    return round(units + nanos / 1e9, 2)


def fetch_account_invoices(billing_account_ids: list[str], on_progress=None) -> dict:
    """
    Cloud Billing 인보이스 API로 빌링 계정별 월별 청구 합계를 조회.
    BigQuery 익스포트 없이 사용 가능하나 프로젝트별 세분화는 불가.

    Returns: {
        billing_account_id: {
            "_total":    float,   # 최근 월 합계
            "_currency": str,
            "_month":    str,     # 최근 인보이스 월 (YYYY-MM)
            "_monthly":  {month: amount},
            "_mode":     "invoice",
        }
    }
    """
    if on_progress:
        on_progress(5, "Cloud Billing 인보이스 API 조회 중...")

    try:
        token = _get_access_token()
    except Exception as e:
        raise RuntimeError(f"gcloud 인증 토큰 취득 실패: {e}")

    result: dict = {}
    total = len(billing_account_ids)

    for idx, bid in enumerate(billing_account_ids):
        if on_progress:
            on_progress(10 + int(idx / total * 85),
                        f"({idx+1}/{total}) 인보이스 조회: {bid}")
        try:
            url = (f"https://cloudbilling.googleapis.com/v1/"
                   f"billingAccounts/{bid}/invoices")
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())

            invoices = data.get("invoices", [])
            monthly: dict[str, float] = {}
            currency = "USD"

            for inv in invoices[:6]:      # 최근 6개월
                date = inv.get("invoiceDate", {})
                year = date.get("year")
                month = date.get("month")
                if not year or not month:
                    continue
                month_key = f"{year}-{str(month).zfill(2)}"
                # subtotalAmount = 세금 전 합계
                amt = inv.get("subtotalAmount") or inv.get("totalAmount") or {}
                currency = amt.get("currencyCode", "USD")
                monthly[month_key] = _parse_money(amt)

            latest = max(monthly.keys()) if monthly else ""
            result[bid] = {
                "_total":    monthly.get(latest, 0),
                "_currency": currency,
                "_month":    latest,
                "_monthly":  monthly,
                "_mode":     "invoice",
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 인보이스 API는 카드 자동결제 계정에서는 지원되지 않음
                error_msg = "인보이스 API 미지원 (카드 자동결제 계정). BigQuery 빌링 익스포트를 설정하세요."
            elif e.code == 403:
                error_msg = "권한 없음 (roles/billing.viewer 이상 필요)"
            else:
                error_msg = f"HTTP {e.code}: {e.reason}"
            result[bid] = {
                "_total": 0, "_currency": "USD",
                "_month": "", "_monthly": {},
                "_mode": "invoice",
                "_error": error_msg,
            }
        except Exception as e:
            result[bid] = {
                "_total": 0, "_currency": "USD",
                "_month": "", "_monthly": {},
                "_mode": "invoice",
                "_error": str(e),
            }

    if on_progress:
        on_progress(100, "인보이스 조회 완료!")
    return result


# ── 빌링 계정 현황 스캔 (BigQuery 없이) ──────────────────────────────

def fetch_billing_accounts(projects: list[dict], on_progress=None) -> dict:
    """
    Cloud Billing SDK로 빌링 계정 현황 조회 (gcloud subprocess 없음).
    Returns: {
        billing_account_id: {
            "display_name": str,
            "open": bool,
            "currency": str,
            "master_billing_account": str,
            "project_count": int,
            "project_ids": [str],
            "_mode": "account_overview",
        }
    }
    """
    from google.cloud import billing_v1
    from gcp import _get_credentials

    if on_progress:
        on_progress(5, "빌링 계정 목록 조회 중 (SDK)...")

    try:
        creds = _get_credentials()
        client = billing_v1.CloudBillingClient(credentials=creds)
        accounts_raw = list(client.list_billing_accounts())
    except Exception as e:
        raise RuntimeError(f"빌링 계정 목록 조회 실패: {e}")

    if on_progress:
        on_progress(20, f"빌링 계정 {len(accounts_raw)}개 확인. 프로젝트 연결 정보 조합 중...")

    # 프로젝트 목록에서 billing_account_id → project 매핑
    pid_to_bid: dict[str, str] = {
        p["project_id"]: p["billing_account_id"]
        for p in projects
        if p.get("billing_account_id")
    }
    bid_to_pids: dict[str, list[str]] = {}
    for pid, bid in pid_to_bid.items():
        bid_to_pids.setdefault(bid, []).append(pid)

    if on_progress:
        on_progress(70, "데이터 조합 중...")

    result: dict = {}
    for acc in accounts_raw:
        bid = acc.name.replace("billingAccounts/", "") if acc.name else ""
        if not bid:
            continue
        master_id = acc.master_billing_account.replace("billingAccounts/", "") \
            if acc.master_billing_account else ""
        pids = bid_to_pids.get(bid, [])
        result[bid] = {
            "display_name":           acc.display_name,
            "open":                   acc.open,
            "currency":               getattr(acc, "currency_code", ""),
            "master_billing_account": master_id,
            "project_count":          len(pids),
            "project_ids":            pids,
            "_mode":                  "account_overview",
        }

    if on_progress:
        on_progress(100, f"빌링 계정 {len(result)}개 조회 완료!")
    return result
