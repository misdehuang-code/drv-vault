"""
驱匣管理后台：浏览器一键启动 Link Curation 商业流水线
（需求发现 → 挖直链 → 验链 → SEO → 店面导航目录）。

启动：
    pip install -r requirements.txt
    python admin_server.py

浏览器打开：http://127.0.0.1:8787
首页点「开始」即可（不本地下载大文件）。
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Optional

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import DEFAULT_DB_PATH, Database, Status
from display_labels import enrich_forum, enrich_scraped
from usdt_payment_verify import verify_payment_request
from sync_catalog import sync_catalog, sync_status

# 启动时加载 .env / data/github_token.txt，供 GitHub 挖链使用
try:
    from github_auth import get_github_token, load_dotenv_file

    load_dotenv_file()
    get_github_token()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "admin_web" / "templates"
STATIC_DIR = ROOT / "admin_web" / "static"

app = FastAPI(title="驱匣抓取后台", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:3001",
        "http://localhost:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.post("/api/verify-payment")
async def api_verify_payment(request: Request) -> JSONResponse:
    """Smart USDT-TRC20 reconcile: claim a recent ~4.90 inbound transfer."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    result = verify_payment_request(body)
    status = 200 if result.get("success") else 402
    if result.get("error", "").startswith("trongrid"):
        status = 502
    if result.get("error") == "missing_slug":
        status = 400
    return JSONResponse(result, status_code=status)

# ---------------------------------------------------------------------------
# 后台任务与日志
# ---------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_log_buffer: Deque[str] = deque(maxlen=800)
_log_lock = threading.Lock()
_running = threading.Event()
_stop_requested = threading.Event()  # 「停止」置位
_active_job_id: Optional[str] = None
_active_kind: Optional[str] = None  # "pipeline" | "sync"
_pipeline_proc: Optional[subprocess.Popen] = None
_pipeline_proc_lock = threading.Lock()


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        _append_log(msg)


def _append_log(line: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    text = line.rstrip()
    if not text:
        return
    with _log_lock:
        for part in text.splitlines() or [""]:
            _log_buffer.append(f"[{stamp}] {part}")


def _get_logs(limit: int = 200) -> list[str]:
    with _log_lock:
        items = list(_log_buffer)
    return items[-limit:]


def _set_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {})
        job.update(fields)


def _update_running_job(**fields: Any) -> None:
    """更新当前 running 任务字段（用于一键流水线阶段名）。"""
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("status") == "running":
                job.update(fields)
                break


def _current_job() -> Optional[dict[str, Any]]:
    with _jobs_lock:
        if _active_job_id and _active_job_id in _jobs:
            return dict(_jobs[_active_job_id])
        running = [j for j in _jobs.values() if j.get("status") == "running"]
        if running:
            return dict(sorted(running, key=lambda x: x.get("started_at", ""))[-1])
        if not _jobs:
            return None
        latest = sorted(
            _jobs.values(), key=lambda x: x.get("started_at", ""), reverse=True
        )[0]
        return dict(latest)


def _control_state() -> dict[str, Any]:
    """开始/停止按钮的唯一真相源。"""
    busy = _running.is_set()
    kind = _active_kind
    pause = _stop_requested.is_set()
    pipeline_running = busy and kind == "pipeline"
    sync_running = busy and kind == "sync"
    # 停止中仍允许点停止无效；开始在 busy 时禁用
    # 强制：只要流水线 busy，停止按钮可点（含正在停止）
    return {
        "busy": busy,
        "kind": kind,
        "pipeline_running": pipeline_running,
        "sync_running": sync_running,
        "pause_requested": pause and pipeline_running,
        "can_start": not busy,
        "can_stop": pipeline_running,  # 停止中也可再点强制杀
    }


class PipelineStopped(Exception):
    """用户点击停止后主动结束流水线。"""


def _kill_pipeline_process() -> bool:
    """Hard-kill pipeline subprocess (Windows taskkill /T)."""
    global _pipeline_proc
    with _pipeline_proc_lock:
        proc = _pipeline_proc
        _pipeline_proc = None
    if not proc:
        return False
    if proc.poll() is not None:
        return True
    pid = proc.pid
    _append_log(f"[停止] 强制结束流水线子进程 pid={pid}")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:
        _append_log(f"[停止] taskkill 异常，改用 kill: {exc}")
        try:
            proc.kill()
        except Exception:
            pass
    return True


def _mark_job_stopped(message: str = "已强制停止") -> None:
    global _active_job_id, _active_kind
    jid = _active_job_id
    if jid:
        _set_job(
            jid,
            status="paused",
            finished_at=datetime.now(timezone.utc).isoformat(),
            paused=True,
            message=message,
            name="已停止",
            phase="",
        )
    _running.clear()
    _active_job_id = None
    _active_kind = None
    clear_pipeline_pause()


def request_pipeline_pause() -> bool:
    """停止按钮：立刻硬杀流水线子进程，并解锁开始按钮。"""
    if not (_running.is_set() and _active_kind == "pipeline"):
        # 兜底：若 busy 但 kind 丢失，仍尝试杀残留子进程
        if _running.is_set():
            _stop_requested.set()
            _kill_pipeline_process()
            _mark_job_stopped("已强制停止（状态异常恢复）")
            _append_log("[停止] 异常 busy 状态已强制清空")
            return True
        return False
    _stop_requested.set()
    _update_running_job(name="正在强制停止…", paused=True, phase="STOPPING")
    _append_log("[停止] 强制终止流水线子进程（不等网络请求结束）")
    _kill_pipeline_process()
    _mark_job_stopped("已强制停止")
    _append_log("[停止] 流水线已停止，可以重新点「开始」")
    return True


def clear_pipeline_pause() -> None:
    _stop_requested.clear()


def _sleep_interruptible(seconds: float) -> None:
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if _stop_requested.is_set():
            raise PipelineStopped("用户已请求停止")
        time.sleep(min(0.25, end - time.time()))


def run_full_pipeline(
    *,
    max_items: int = 8,
    list_url: str = "https://www.oldversion.com/windows/",
    max_per_query: int = 5,
    sources: str = "github",
    clean_scraped_limit: int = 80,
    clean_forum_limit: int = 80,
    offline: bool = False,
    fast: bool = True,
    do_harvest: bool = True,
    use_ai: bool = True,
    harvest_workers: int = 4,
    harvest_rate_mbps: float = 8.0,
    continuous: bool = True,
    round_sleep_sec: float = 12.0,
) -> dict[str, Any]:
    """
    商业级常驻流水线（唯一入口）。
    旧版分步/多线路流程已废弃；参数保留仅为兼容表单字段。
    """
    del max_items, list_url, clean_scraped_limit, fast, do_harvest  # 兼容忽略
    clear_pipeline_pause()

    from commercial_pipeline import (
        PipelineStopped as CommercialStopped,
        run_commercial_daemon,
    )

    def on_round_start(round_no: int) -> None:
        _update_running_job(
            name=f"商业流水线 · 第 {round_no} 轮",
            round=round_no,
            continuous=continuous,
            phase="",
        )

    def on_phase(code: str, title: str, step: int, total: int) -> None:
        if _stop_requested.is_set():
            raise CommercialStopped(f"用户停止于 {code}")
        _update_running_job(
            name=f"{step}/{total} {title}",
            phase=code,
            step=step,
            total_steps=total,
            paused=False,
            kind="pipeline",
        )
        _append_log(f"[{step}/{total}] {code} · {title}")

    try:
        result = run_commercial_daemon(
            continuous=continuous,
            round_sleep_sec=round_sleep_sec,
            max_per_query=max_per_query,
            sources=sources or "github,reddit",
            clean_forum_limit=clean_forum_limit,
            use_ai=use_ai and not offline,
            harvest_workers=harvest_workers,
            harvest_rate_mbps=harvest_rate_mbps,
            should_stop=lambda: _stop_requested.is_set(),
            append_log=_append_log,
            on_phase=on_phase,
            on_round_start=on_round_start,
        )
        if result.get("paused"):
            result["message"] = result.get("message") or "已按用户停止退出"
        return result
    except CommercialStopped as exc:
        _append_log(f"[停止] {exc}")
        return {
            "ok": True,
            "paused": True,
            "message": "已按用户停止退出",
        }




class _StreamToLog(io.TextIOBase):
    """把 print / tqdm 输出 diversion 到日志缓冲。"""

    encoding = "utf-8"

    def write(self, s: str) -> int:  # type: ignore[override]
        if not isinstance(s, str):
            s = str(s)
        if s.strip():
            _append_log(s.rstrip("\n"))
        return len(s)

    def flush(self) -> None:
        return None


def _run_job(
    name: str,
    fn: Callable[[], Any],
    *,
    kind: str = "pipeline",
) -> str:
    global _active_job_id, _active_kind
    if _running.is_set():
        raise RuntimeError("已有任务在运行，请先点「停止」结束当前流水线")

    job_id = uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc).isoformat()
    if kind == "pipeline":
        clear_pipeline_pause()
    _set_job(
        job_id,
        id=job_id,
        name=name,
        kind=kind,
        status="running",
        started_at=started,
        finished_at=None,
        error=None,
        paused=False,
        phase="",
    )
    _append_log(f"===== 开始任务: {name} ({job_id}) =====")
    _active_job_id = job_id
    _active_kind = kind
    _running.set()

    def worker() -> None:
        global _active_job_id, _active_kind
        root_logger = logging.getLogger()
        handler = _BufferHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        root_logger.addHandler(handler)
        stream = _StreamToLog()
        try:
            with redirect_stdout(stream), redirect_stderr(stream):
                result = fn()
            paused = bool(isinstance(result, dict) and result.get("paused"))
            status = "paused" if paused else "success"
            msg = ""
            if isinstance(result, dict):
                msg = str(result.get("message") or "")
            _append_log(f"任务结果: {result!r}")
            _set_job(
                job_id,
                status=status,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=str(result),
                paused=paused,
                message=msg,
            )
            if paused:
                _append_log(f"===== 已停止: {name} =====")
            else:
                _append_log(f"===== 任务完成: {name} =====")
        except Exception as exc:
            _append_log(f"任务失败: {exc}")
            _append_log(traceback.format_exc())
            _set_job(
                job_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
                paused=False,
            )
        finally:
            root_logger.removeHandler(handler)
            clear_pipeline_pause()
            _running.clear()
            _active_job_id = None
            _active_kind = None

    threading.Thread(target=worker, name=f"job-{job_id}", daemon=True).start()
    return job_id


# ---------------------------------------------------------------------------
# 数据查询
# ---------------------------------------------------------------------------
def _stats() -> dict[str, Any]:
    with Database() as db:
        scraped = {s.value: db.count_by_status(s) for s in Status}
        forums = {s.value: db.count_forum_posts(s) for s in Status}
        scraped_total = sum(scraped.values())
        forum_total = sum(forums.values())
    return {
        "scraped": scraped,
        "forum": forums,
        "scraped_total": scraped_total,
        "forum_total": forum_total,
        "db_path": str(DEFAULT_DB_PATH),
    }


def _list_scraped(
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with Database() as db:
        if status:
            rows = db._conn.execute(
                """
                SELECT id, title, device_model, status, value_score,
                       os_version, seo_title, source_url, created_at
                FROM scraped_items
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """
                SELECT id, title, device_model, status, value_score,
                       os_version, seo_title, source_url, created_at
                FROM scraped_items
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [enrich_scraped(dict(r)) for r in rows]


def _list_forum(
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with Database() as db:
        if status:
            rows = db._conn.execute(
                """
                SELECT id, title, device_model, device_category, status,
                       value_score, keyword, source, source_url, pain_point,
                       error_codes, created_at
                FROM forum_posts
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """
                SELECT id, title, device_model, device_category, status,
                       value_score, keyword, source, source_url, pain_point,
                       error_codes, created_at
                FROM forum_posts
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [enrich_forum(dict(r)) for r in rows]


def _whitelist_preview(limit: int = 20) -> list[dict[str, Any]]:
    path = ROOT / "data" / "急需设备型号白名单.json"
    if not path.exists():
        return []
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("items") or [])[:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    from commercial_pipeline import commercial_stats, load_commercial_catalog

    commerce = commercial_stats()
    ctrl = _control_state()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "job": _current_job(),
            "busy": ctrl["busy"],
            "can_start": ctrl["can_start"],
            "can_stop": ctrl["can_stop"],
            "commerce": commerce,
            "phases": commerce.get("phases") or [],
            "catalog": load_commercial_catalog(40),
            "logs": _get_logs(100),
        },
    )


@app.get("/data/scraped", response_class=HTMLResponse)
def data_scraped(
    request: Request,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    limit = 40
    offset = (page - 1) * limit
    rows = _list_scraped(status=status or None, limit=limit, offset=offset)
    return templates.TemplateResponse(
        "data_scraped.html",
        {
            "request": request,
            "rows": rows,
            "status": status or "",
            "page": page,
            "busy": _running.is_set(),
            "job": _current_job(),
        },
    )


@app.get("/data/forum", response_class=HTMLResponse)
def data_forum(
    request: Request,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    limit = 40
    offset = (page - 1) * limit
    rows = _list_forum(status=status or None, limit=limit, offset=offset)
    return templates.TemplateResponse(
        "data_forum.html",
        {
            "request": request,
            "rows": rows,
            "status": status or "",
            "page": page,
            "busy": _running.is_set(),
            "job": _current_job(),
        },
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "logs": _get_logs(300),
            "busy": _running.is_set(),
            "job": _current_job(),
        },
    )


# ---------------------------------------------------------------------------
# 任务 API / 表单提交
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status() -> JSONResponse:
    from commercial_pipeline import commercial_stats, load_commercial_catalog

    job = _current_job()
    commerce = commercial_stats()
    ctrl = _control_state()
    try:
        # Lightweight sync hint only — full fingerprint scan freezes the UI.
        from sync_catalog import sync_status_light

        sync = sync_status_light()
    except Exception:
        try:
            sync = sync_status()
        except Exception as exc:
            sync = {"needs_sync": False, "error": str(exc)}
    return JSONResponse(
        {
            **ctrl,
            "job": job,
            "stats": _stats(),
            "commerce": commerce,
            "catalog_preview": load_commercial_catalog(8, allow_cold_parse=False),
            "storefront": "http://127.0.0.1:3000",
            "logs": _get_logs(100),
            "sync": sync,
            "phases": commerce.get("phases") or [],
        }
    )


@app.get("/api/sync-status")
def api_sync_status() -> JSONResponse:
    """Lightweight: whether local catalog needs Cloudflare sync."""
    try:
        return JSONResponse(sync_status())
    except Exception as exc:
        return JSONResponse(
            {"needs_sync": False, "new_items_count": 0, "error": str(exc)},
            status_code=500,
        )


@app.post("/api/sync-cloudflare")
def api_sync_cloudflare() -> JSONResponse:
    """Dedupe catalog, dual-write web/data, then wrangler pages deploy."""
    if _running.is_set():
        # allow sync while pipeline runs? safer to block deploy mid-pipeline
        return JSONResponse(
            {
                "ok": False,
                "error": "pipeline_busy",
                "hint": "Wait for the commercial pipeline to finish (or pause it), then sync.",
            },
            status_code=409,
        )

    def task() -> dict[str, Any]:
        _append_log("[SYNC] Strict dedupe + dual-write + Cloudflare deploy…")
        result = sync_catalog(deploy=True)
        if result.get("deploy_ok"):
            _append_log(
                f"[SYNC] Cloudflare OK · sku={result.get('sku_count')} · "
                f"removed={result.get('dedupe', {}).get('removed')}"
            )
        else:
            _append_log(
                f"[SYNC] Catalog written; deploy failed: {result.get('deploy_error')}"
            )
            if result.get("deploy_log"):
                for line in str(result["deploy_log"]).splitlines()[-40:]:
                    _append_log(line)
        return result

    try:
        job_id = _run_job("Sync to Cloudflare", task, kind="sync")
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    return JSONResponse({"ok": True, "started": True, "job_id": job_id})


def _run_pipeline_subprocess(
    *,
    max_per_query: int = 5,
    sources: str = "github,reddit",
    clean_forum_limit: int = 80,
    use_ai: bool = True,
    continuous: bool = True,
    harvest_workers: int = 4,
    offline: bool = False,
    round_sleep_sec: float = 12.0,
) -> str:
    """启动可强制杀死的流水线子进程（停止按钮 = taskkill）。"""
    global _active_job_id, _active_kind, _pipeline_proc

    if _running.is_set():
        raise RuntimeError("已有任务在运行，请先点「停止」结束当前流水线")

    job_id = uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc).isoformat()
    clear_pipeline_pause()
    _set_job(
        job_id,
        id=job_id,
        name="商业级常驻流水线",
        kind="pipeline",
        status="running",
        started_at=started,
        finished_at=None,
        error=None,
        paused=False,
        phase="",
    )
    worker_script = ROOT / "run_pipeline_worker.py"
    cmd = [
        sys.executable,
        "-u",
        str(worker_script),
        "--sources",
        sources or "github,reddit",
        "--max-per-query",
        str(max(1, min(int(max_per_query), 20))),
        "--clean-forum-limit",
        str(max(1, min(int(clean_forum_limit), 500))),
        "--harvest-workers",
        str(max(1, min(int(harvest_workers), 8))),
        "--round-sleep",
        str(max(5.0, float(round_sleep_sec))),
        "--continuous",
        "1" if continuous else "0",
        "--use-ai",
        "1" if use_ai and not offline else "0",
        "--offline",
        "1" if offline else "0",
    ]
    _append_log(f"===== 开始任务: 商业级常驻流水线 ({job_id}) [subprocess] =====")
    creationflags = 0
    if sys.platform == "win32":
        # 独立进程组，便于 taskkill /T
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 避免子进程继承错误的控制台代码页导致日志乱码
    env["PYTHONLEGACYWINDOWSSTDIO"] = "0"

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=creationflags,
    )
    with _pipeline_proc_lock:
        _pipeline_proc = proc
    _active_job_id = job_id
    _active_kind = "pipeline"
    _running.set()
    _append_log(f"[流水线] 子进程已启动 pid={proc.pid}")

    def reader() -> None:
        global _active_job_id, _active_kind, _pipeline_proc
        exit_code = None
        killed = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = (raw or "").rstrip("\n")
                if not line:
                    continue
                if line.startswith("__PHASE__ "):
                    parts = line.split(" ", 3)
                    # __PHASE__ CODE step/total title
                    code = parts[1] if len(parts) > 1 else ""
                    step_info = parts[2] if len(parts) > 2 else ""
                    title = parts[3] if len(parts) > 3 else ""
                    step_n = 0
                    total_n = 0
                    if "/" in step_info:
                        try:
                            a, b = step_info.split("/", 1)
                            step_n, total_n = int(a), int(b)
                        except ValueError:
                            pass
                    if _running.is_set() and _active_job_id == job_id:
                        _update_running_job(
                            name=f"{step_info} {title}".strip() or code,
                            phase=code,
                            step=step_n,
                            total_steps=total_n,
                        )
                    continue
                if line.startswith("__ROUND__ "):
                    try:
                        rn = int(line.split(" ", 1)[1])
                    except ValueError:
                        rn = 0
                    if _running.is_set() and _active_job_id == job_id:
                        _update_running_job(
                            name=f"商业流水线 · 第 {rn} 轮",
                            round=rn,
                            continuous=continuous,
                        )
                    continue
                if line.startswith("__RESULT__ "):
                    continue
                _append_log(line)
            exit_code = proc.wait()
        except Exception as exc:
            _append_log(f"[流水线] 读取子进程输出失败: {exc}")
            try:
                exit_code = proc.wait(timeout=1)
            except Exception:
                exit_code = -1
        finally:
            with _pipeline_proc_lock:
                if _pipeline_proc is proc:
                    _pipeline_proc = None
            # 若停止按钮已清空状态，不要覆盖
            if _active_job_id != job_id and not _running.is_set():
                return
            if _stop_requested.is_set() or killed:
                if _active_job_id == job_id or _running.is_set():
                    _mark_job_stopped("已强制停止")
                return
            # 正常结束
            paused = bool(exit_code == 0 and _stop_requested.is_set())
            status = "paused" if paused else ("success" if exit_code == 0 else "failed")
            _set_job(
                job_id,
                status=status,
                finished_at=datetime.now(timezone.utc).isoformat(),
                paused=paused,
                result=f"exit={exit_code}",
                error=None if exit_code == 0 else f"worker_exit_{exit_code}",
            )
            _append_log(
                f"===== {'已停止' if paused else '任务完成'}: 商业级常驻流水线 ====="
            )
            clear_pipeline_pause()
            _running.clear()
            _active_job_id = None
            _active_kind = None

    threading.Thread(target=reader, name=f"pipeline-reader-{job_id}", daemon=True).start()
    return job_id


def _start_pipeline_from_params(
    *,
    max_per_query: int = 5,
    sources: str = "github,reddit",
    clean_forum_limit: int = 80,
    use_ai: bool = True,
    continuous: bool = True,
    harvest_workers: int = 4,
    harvest_rate_mbps: float = 0.0,
    offline: bool = False,
    round_sleep_sec: float = 12.0,
) -> str:
    del harvest_rate_mbps
    return _run_pipeline_subprocess(
        max_per_query=max_per_query,
        sources=sources,
        clean_forum_limit=clean_forum_limit,
        use_ai=use_ai,
        continuous=continuous,
        harvest_workers=harvest_workers,
        offline=offline,
        round_sleep_sec=round_sleep_sec,
    )


@app.post("/api/pipeline/start")
async def api_pipeline_start(request: Request) -> JSONResponse:
    """开始按钮唯一入口：启动 DEMAND→…→MERCHANDIZE 常驻流水线。"""
    if _running.is_set():
        return JSONResponse(
            {
                "ok": False,
                "error": "busy",
                "hint": "流水线或同步任务仍在运行，请先点「停止」。",
                **_control_state(),
            },
            status_code=409,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        job_id = _start_pipeline_from_params(
            max_per_query=int(body.get("max_per_query") or 5),
            sources=str(body.get("sources") or "github,reddit"),
            clean_forum_limit=int(body.get("clean_forum_limit") or 80),
            use_ai=bool(body.get("use_ai", True)),
            continuous=bool(body.get("continuous", True)),
            harvest_workers=int(body.get("harvest_workers") or 4),
            harvest_rate_mbps=float(body.get("harvest_rate_mbps") or 0),
            offline=bool(body.get("offline") or False),
            round_sleep_sec=float(body.get("round_sleep_sec") or 12),
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), **_control_state()},
            status_code=409,
        )
    return JSONResponse(
        {
            "ok": True,
            "started": True,
            "job_id": job_id,
            "message": "商业流水线已启动",
            **_control_state(),
        }
    )


@app.post("/api/pipeline/stop")
def api_pipeline_stop() -> JSONResponse:
    """停止按钮：立刻 taskkill 流水线子进程。"""
    ctrl = _control_state()
    if ctrl.get("sync_running"):
        return JSONResponse(
            {
                "ok": False,
                "error": "sync_running",
                "hint": "当前是 Cloudflare 同步任务，请等它结束。",
                **ctrl,
            },
            status_code=409,
        )
    if not ctrl.get("busy"):
        clear_pipeline_pause()
        _kill_pipeline_process()
        return JSONResponse(
            {
                "ok": True,
                "stopped": False,
                "message": "当前没有运行中的流水线",
                **_control_state(),
            }
        )
    ok = request_pipeline_pause()
    return JSONResponse(
        {
            "ok": True,
            "stopped": ok,
            "message": "已强制停止流水线" if ok else "停止未生效",
            **_control_state(),
        }
    )


@app.post("/jobs/full-pipeline")
def job_full_pipeline(
    max_items: int = Form(8),
    list_url: str = Form("https://www.oldversion.com/windows/"),
    max_per_query: int = Form(5),
    sources: str = Form("github,reddit"),
    clean_scraped_limit: int = Form(80),
    clean_forum_limit: int = Form(80),
    offline: Optional[str] = Form(""),
    fast: Optional[str] = Form("1"),
    do_harvest: Optional[str] = Form("1"),
    use_ai: Optional[str] = Form("1"),
    continuous: Optional[str] = Form("1"),
    harvest_workers: int = Form(4),
    harvest_rate_mbps: float = Form(0.0),
) -> RedirectResponse:
    """兼容旧表单；推荐走 /api/pipeline/start。"""
    del max_items, list_url, clean_scraped_limit, fast, do_harvest
    try:
        _start_pipeline_from_params(
            max_per_query=max_per_query,
            sources=sources,
            clean_forum_limit=clean_forum_limit,
            use_ai=bool(use_ai),
            continuous=bool(continuous),
            harvest_workers=harvest_workers,
            harvest_rate_mbps=harvest_rate_mbps,
            offline=bool(offline),
            round_sleep_sec=12.0,
        )
    except RuntimeError as exc:
        _append_log(str(exc))
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/pause-pipeline")
def job_pause_pipeline() -> RedirectResponse:
    """兼容旧表单；推荐走 /api/pipeline/stop。"""
    if not request_pipeline_pause():
        _append_log("[停止] 当前没有运行中的商业流水线")
    return RedirectResponse("/", status_code=303)


def _legacy_job_removed(name: str) -> RedirectResponse:
    _append_log(f"[已废弃] {name}：请只使用首页「开始」商业流水线")
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/crawl-source-a")
def job_crawl_source_a(
    max_items: int = Form(5),
    list_url: str = Form("https://www.oldversion.com/windows/"),
) -> RedirectResponse:
    del max_items, list_url
    return _legacy_job_removed("分步抓取驱动站")


@app.post("/jobs/crawl-forums")
def job_crawl_forums(
    max_per_query: int = Form(5),
    sources: str = Form("stackexchange"),
    fast: Optional[str] = Form(None),
) -> RedirectResponse:
    del max_per_query, sources, fast
    return _legacy_job_removed("分步抓论坛")


@app.post("/jobs/clean-scraped")
def job_clean_scraped(limit: int = Form(50)) -> RedirectResponse:
    del limit
    return _legacy_job_removed("分步清洗驱动页")


@app.post("/jobs/clean-forum")
def job_clean_forum(
    limit: int = Form(50),
    offline: Optional[str] = Form("1"),
) -> RedirectResponse:
    del limit, offline
    return _legacy_job_removed("分步清洗论坛")


@app.post("/jobs/rescore-scraped")
def job_rescore_scraped() -> RedirectResponse:
    return _legacy_job_removed("分步重算价值分")


def main() -> None:
    # 保证能 import 同目录模块
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    import socket

    def port_free(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return True
            except OSError:
                return False

    host = "127.0.0.1"
    port = 8787
    for candidate in range(8787, 8800):
        if port_free(host, candidate):
            port = candidate
            break
    else:
        raise SystemExit("8787–8799 端口均被占用，请先关闭旧的 admin_server 进程")

    if port != 8787:
        print(f"提示: 8787 已被占用，改用 http://{host}:{port}")
    print(f"驱匣抓取后台: http://{host}:{port}")
    print("注意: 不要重复启动多个后台；关掉本窗口即停止服务。")
    uvicorn.run(
        "admin_server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
