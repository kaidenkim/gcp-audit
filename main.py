from __future__ import annotations
import asyncio
import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from gcp import get_auth_info, full_scan, scan_billing_resources, delete_project
from export import generate_excel
from billing import load_settings, save_settings, fetch_costs, fetch_account_invoices, fetch_billing_accounts

CACHE_FILE          = Path.home() / ".gcp_audit_cache.json"
RESOURCE_CACHE_FILE = Path.home() / ".gcp_audit_resource_cache.json"
BILLING_COST_FILE   = Path.home() / ".gcp_audit_billing_costs.json"

app = FastAPI(title="GCP Project Auditor")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── auth 캐시 (gcloud 호출 최소화) ────────────────────────────────────
_auth_cache: dict | None = None
_auth_cache_lock = threading.Lock()

def _refresh_auth_cache():
    """백그라운드에서 60초마다 auth 정보 갱신."""
    global _auth_cache
    while True:
        try:
            info = get_auth_info()
            with _auth_cache_lock:
                _auth_cache = info
        except Exception:
            pass
        time.sleep(60)

def _warmup_credentials():
    """서버 시작 시 credentials + gRPC 클라이언트를 미리 초기화 → 첫 스캔 지연 제거."""
    try:
        from gcp import _get_credentials
        from google.cloud import resourcemanager_v3, billing_v1
        creds = _get_credentials()
        # 각 클라이언트 타입 1개씩 미리 생성 (gRPC 채널 워밍업)
        resourcemanager_v3.ProjectsClient(credentials=creds)
        billing_v1.CloudBillingClient(credentials=creds)
    except Exception:
        pass

threading.Thread(target=_refresh_auth_cache, daemon=True).start()
threading.Thread(target=_warmup_credentials, daemon=True).start()

# ── 전역 상태 ─────────────────────────────────────────────────────────
_state: dict = {
    "status": "idle",
    "pct": 0, "stage": "", "projects": [], "last_scan": None, "error": None,
}
_progress_q: queue.Queue = queue.Queue()
_scan_running = False

# ── 리소스 상태 ───────────────────────────────────────────────────────
_res_state: dict = {
    "status": "idle",
    "pct": 0, "stage": "", "projects": [], "last_scan": None, "error": None,
}
_res_q: queue.Queue = queue.Queue()
_res_running = False

# ── 빌링 비용 상태 ────────────────────────────────────────────────────
_billing_state: dict = {
    "status": "idle",
    "pct": 0, "stage": "", "costs": {}, "mode": "", "last_scan": None, "error": None,
}
_billing_q: queue.Queue = queue.Queue()
_billing_running = False


# ── 시작 시 캐시 로드 ─────────────────────────────────────────────────
@app.on_event("startup")
async def _load_cache():
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            _state["projects"] = data.get("projects", [])
            _state["last_scan"] = data.get("last_scan")
            if _state["projects"]:
                _state["status"] = "done"
        except Exception:
            pass
    if RESOURCE_CACHE_FILE.exists():
        try:
            data = json.loads(RESOURCE_CACHE_FILE.read_text())
            _res_state["projects"] = data.get("projects", [])
            _res_state["last_scan"] = data.get("last_scan")
            if _res_state["projects"]:
                _res_state["status"] = "done"
        except Exception:
            pass
    if BILLING_COST_FILE.exists():
        try:
            data = json.loads(BILLING_COST_FILE.read_text())
            _billing_state["costs"] = data.get("costs", {})
            _billing_state["mode"] = data.get("mode", "")
            _billing_state["last_scan"] = data.get("last_scan")
            if _billing_state["costs"]:
                _billing_state["status"] = "done"
        except Exception:
            pass


# ── 스캔 스레드 ───────────────────────────────────────────────────────
def _run_scan():
    global _scan_running
    _scan_running = True
    _state["status"] = "scanning"
    _state["error"] = None

    def on_progress(pct, stage, done=0, total=0):
        _state["pct"] = pct
        _state["stage"] = stage
        _progress_q.put({"type": "progress", "pct": pct, "stage": stage,
                          "done": done, "total": total})

    try:
        projects = full_scan(on_progress)
        _state["projects"] = projects
        _state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _state["status"] = "done"
        CACHE_FILE.write_text(
            json.dumps({"projects": projects, "last_scan": _state["last_scan"]},
                       ensure_ascii=False)
        )
        _progress_q.put({"type": "done"})
    except Exception as exc:
        _state["status"] = "error"
        _state["error"] = str(exc)
        _progress_q.put({"type": "error", "message": str(exc)})
    finally:
        _scan_running = False


# ── API ───────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    with _auth_cache_lock:
        auth = _auth_cache
    return {
        "account":   auth.get("account") if auth else None,
        "status":    _state["status"],
        "pct":       _state["pct"],
        "stage":     _state["stage"],
        "last_scan": _state["last_scan"],
        "error":     _state["error"],
        "projects":  _state["projects"],
    }


@app.post("/api/scan")
async def api_scan_start():
    global _scan_running
    if _scan_running:
        return {"status": "already_running"}
    while not _progress_q.empty():
        try:
            _progress_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/scan/stream")
async def api_scan_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _progress_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _scan_running:
                    break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/export")
async def api_export():
    if not _state["projects"]:
        return Response(content="데이터 없음. 먼저 스캔을 실행하세요.", status_code=400)
    xlsx_bytes = generate_excel(_state["projects"])
    filename = f"gcp_projects_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/projects/delete")
async def api_delete_projects(request: Request):
    body = await request.json()
    project_ids = body.get("project_ids", [])
    if not project_ids:
        return {"status": "error", "message": "삭제할 프로젝트 ID가 없습니다."}

    results = []
    for pid in project_ids:
        success, msg = delete_project(pid)
        results.append({"project_id": pid, "success": success, "message": msg})
        if success:
            _state["projects"] = [p for p in _state["projects"] if p["project_id"] != pid]

    CACHE_FILE.write_text(
        json.dumps({"projects": _state["projects"], "last_scan": _state["last_scan"]},
                   ensure_ascii=False)
    )
    return {"status": "done", "results": results}


# ── 리소스 스캔 스레드 ───────────────────────────────────────────────
def _run_resource_scan():
    global _res_running
    _res_running = True
    _res_state["status"] = "scanning"
    _res_state["error"] = None

    def on_progress(pct, stage, done=0, total=0):
        _res_state["pct"] = pct
        _res_state["stage"] = stage
        _res_q.put({"type": "progress", "pct": pct, "stage": stage,
                    "done": done, "total": total})

    try:
        billing = [p for p in _state["projects"] if p.get("deletable") == "빌링연결"]
        if not billing:
            raise RuntimeError("빌링 연결된 프로젝트가 없습니다. 전체 스캔을 먼저 실행하세요.")
        result = scan_billing_resources(billing, on_progress)
        _res_state["projects"] = result
        _res_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _res_state["status"] = "done"
        RESOURCE_CACHE_FILE.write_text(
            json.dumps({"projects": result, "last_scan": _res_state["last_scan"]},
                       ensure_ascii=False)
        )
        _res_q.put({"type": "done"})
    except Exception as exc:
        _res_state["status"] = "error"
        _res_state["error"] = str(exc)
        _res_q.put({"type": "error", "message": str(exc)})
    finally:
        _res_running = False


@app.post("/api/resources/scan")
async def api_resource_scan_start():
    global _res_running
    if _res_running:
        return {"status": "already_running"}
    if not _state["projects"]:
        return {"status": "error", "message": "전체 프로젝트 스캔을 먼저 실행하세요."}
    while not _res_q.empty():
        try:
            _res_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_resource_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/resources/stream")
async def api_resource_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _res_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _res_running:
                    break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/resources")
async def api_resources():
    return {
        "status":    _res_state["status"],
        "pct":       _res_state["pct"],
        "stage":     _res_state["stage"],
        "last_scan": _res_state["last_scan"],
        "error":     _res_state["error"],
        "projects":  _res_state["projects"],
    }


# ── 빌링 비용 ─────────────────────────────────────────────────────────
@app.get("/api/billing/settings")
async def api_billing_settings_get():
    return load_settings()


@app.post("/api/billing/settings")
async def api_billing_settings_save(request: Request):
    body = await request.json()
    save_settings({"bq_project": body.get("bq_project", ""),
                   "bq_dataset": body.get("bq_dataset", "")})
    return {"status": "saved"}


def _run_billing_scan():
    global _billing_running
    _billing_running = True
    _billing_state["status"] = "scanning"
    _billing_state["error"] = None

    def on_progress(pct, stage):
        _billing_state["pct"] = pct
        _billing_state["stage"] = stage
        _billing_q.put({"type": "progress", "pct": pct, "stage": stage})

    try:
        s = load_settings()
        if s.get("bq_project") and s.get("bq_dataset"):
            # BigQuery 모드
            costs = fetch_costs(s["bq_project"], s["bq_dataset"], on_progress)
            mode = "bigquery"
        else:
            # 빌링 계정 현황 모드 (gcloud billing accounts list 기반)
            on_progress(2, "BigQuery 미설정 → 빌링 계정 현황 모드로 조회합니다...")
            if not _state["projects"]:
                raise RuntimeError("전체 프로젝트 스캔을 먼저 실행하세요.")
            costs = fetch_billing_accounts(_state["projects"], on_progress)
            mode = "account_overview"

        _billing_state["costs"] = costs
        _billing_state["mode"] = mode
        _billing_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _billing_state["status"] = "done"
        BILLING_COST_FILE.write_text(
            json.dumps({"costs": costs, "mode": mode,
                        "last_scan": _billing_state["last_scan"]},
                       ensure_ascii=False)
        )
        _billing_q.put({"type": "done", "mode": mode})
    except Exception as exc:
        _billing_state["status"] = "error"
        _billing_state["error"] = str(exc)
        _billing_q.put({"type": "error", "message": str(exc)})
    finally:
        _billing_running = False


@app.post("/api/billing/scan")
async def api_billing_scan_start():
    global _billing_running
    if _billing_running:
        return {"status": "already_running"}
    while not _billing_q.empty():
        try:
            _billing_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_billing_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/billing/stream")
async def api_billing_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _billing_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _billing_running:
                    break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/billing/costs")
async def api_billing_costs():
    return {
        "status":    _billing_state["status"],
        "pct":       _billing_state["pct"],
        "stage":     _billing_state["stage"],
        "last_scan": _billing_state["last_scan"],
        "error":     _billing_state["error"],
        "costs":     _billing_state["costs"],
        "mode":      _billing_state["mode"],
    }


# ── SPA ───────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return HTMLResponse((Path("static") / "index.html").read_text())
