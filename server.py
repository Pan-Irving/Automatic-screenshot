from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import cdp_collector
from xlsx_io import read_tasks_xlsx, write_tasks_xlsx


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKBOOK = BASE_DIR / "questions.xlsx"
RUNTIME_DIR = BASE_DIR / "runtime"
TASKS_PATH = RUNTIME_DIR / "tasks.json"
EVENTS_PATH = RUNTIME_DIR / "events.jsonl"
EXPORT_PATH = RUNTIME_DIR / "exported_tasks.xlsx"
RESULTS_DIR = BASE_DIR / "yingdao_results"
TASK_DELAY_SECONDS = int(os.getenv("YINGDAO_TASK_DELAY_SECONDS", "3"))
TASK_DELAY_RANDOM_SECONDS = int(os.getenv("YINGDAO_TASK_DELAY_RANDOM_SECONDS", "5"))
COLLECTOR_MODE = os.getenv("YINGDAO_COLLECTOR", "cdp").strip().lower()


def _parse_account_slots() -> list[dict[str, Any]]:
    raw = os.getenv("YINGDAO_ACCOUNTS", "").strip()
    if not raw:
        raw = ",".join(
            [
                "deepseek_a:9222:chrome_cdp_profile_deepseek_a:2",
                "deepseek_b:9223:chrome_cdp_profile_deepseek_b:2",
                "deepseek_c:9224:chrome_cdp_profile_deepseek_c:2",
            ]
        )
    slots: list[dict[str, Any]] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for index, item in enumerate(parsed):
                if not isinstance(item, dict):
                    continue
                account_id = str(item.get("account_id") or item.get("id") or f"deepseek_{index + 1}").strip()
                cdp_port = int(item.get("cdp_port") or item.get("port") or 9222 + index)
                profile_dir = str(item.get("profile_dir") or f"chrome_cdp_profile_{account_id}").strip()
                max_concurrency = max(1, min(2, int(item.get("max_concurrency") or 2)))
                slots.append({
                    "account_id": account_id,
                    "cdp_port": cdp_port,
                    "profile_dir": profile_dir,
                    "max_concurrency": max_concurrency,
                })
    except Exception:
        for index, part in enumerate(item.strip() for item in raw.split(",") if item.strip()):
            pieces = [piece.strip() for piece in part.split(":")]
            account_id = pieces[0] if pieces else f"deepseek_{index + 1}"
            try:
                cdp_port = int(pieces[1]) if len(pieces) > 1 and pieces[1] else 9222 + index
            except Exception:
                cdp_port = 9222 + index
            profile_dir = pieces[2] if len(pieces) > 2 and pieces[2] else f"chrome_cdp_profile_{account_id}"
            try:
                max_concurrency = int(pieces[3]) if len(pieces) > 3 and pieces[3] else 2
            except Exception:
                max_concurrency = 2
            slots.append({
                "account_id": account_id,
                "cdp_port": cdp_port,
                "profile_dir": profile_dir,
                "max_concurrency": max(1, min(2, max_concurrency)),
            })
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slot in slots:
        account_id = str(slot.get("account_id") or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        deduped.append(slot)
    return deduped or [{
        "account_id": "deepseek_a",
        "cdp_port": 9222,
        "profile_dir": "chrome_cdp_profile_deepseek_a",
        "max_concurrency": 2,
    }]


ACCOUNT_SLOTS = _parse_account_slots()
TOTAL_ACCOUNT_CAPACITY = max(1, sum(int(slot["max_concurrency"]) for slot in ACCOUNT_SLOTS))
MAX_WORKERS = max(1, min(TOTAL_ACCOUNT_CAPACITY, int(os.getenv("YINGDAO_MAX_WORKERS", str(TOTAL_ACCOUNT_CAPACITY)))))
DEFAULT_ENABLED_ACCOUNT_COUNT = max(1, min(len(ACCOUNT_SLOTS), int(os.getenv("YINGDAO_DEFAULT_ACCOUNT_COUNT", "2"))))

STATUSES_EXECUTED = {"success", "failed", "manual_required"}

app = FastAPI(title="yingdao_mvp Web Console")
app.mount("/results", StaticFiles(directory=str(RESULTS_DIR), check_dir=False), name="results")

_state_lock = threading.RLock()
_runner_thread: threading.Thread | None = None
_pause_requested = False
_runner_enabled_account_ids: list[str] = []


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_enabled_account_ids() -> list[str]:
    return [str(slot["account_id"]) for slot in ACCOUNT_SLOTS[:DEFAULT_ENABLED_ACCOUNT_COUNT]]


def select_account_slots(enabled_account_ids: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [str(item) for item in (enabled_account_ids or []) if str(item)]
    if not selected:
        selected = default_enabled_account_ids()
    selected_set = set(selected)
    return [slot for slot in ACCOUNT_SLOTS if str(slot["account_id"]) in selected_set]


def parse_enabled_account_ids(payload: dict[str, Any] | None) -> list[str]:
    payload = payload or {}
    raw_ids = payload.get("enabled_account_ids")
    if isinstance(raw_ids, list):
        ids = [str(item) for item in raw_ids if str(item)]
    else:
        ids = []
    if not ids:
        try:
            count = int(payload.get("account_count") or DEFAULT_ENABLED_ACCOUNT_COUNT)
        except Exception:
            count = DEFAULT_ENABLED_ACCOUNT_COUNT
        count = max(1, min(len(ACCOUNT_SLOTS), count))
        ids = [str(slot["account_id"]) for slot in ACCOUNT_SLOTS[:count]]
    known = {str(slot["account_id"]) for slot in ACCOUNT_SLOTS}
    return [account_id for account_id in ids if account_id in known]


def account_profile_path(slot: dict[str, Any]) -> Path:
    raw = Path(str(slot.get("profile_dir") or f"chrome_cdp_profile_{slot['account_id']}")).expanduser()
    return raw if raw.is_absolute() else BASE_DIR / raw


def open_account_chrome(slot: dict[str, Any]) -> dict[str, Any]:
    account_id = str(slot["account_id"])
    cdp_port = int(slot["cdp_port"])
    profile_path = account_profile_path(slot)
    if cdp_collector.cdp_is_available(cdp_port):
        ensured = cdp_collector.cdp_ensure_deepseek_page(cdp_port)
        return {
            "account_id": account_id,
            "cdp_port": cdp_port,
            "opened": False,
            "deepseek_opened": ensured,
            "message": "already_running_deepseek_ready" if ensured else "already_running_deepseek_open_failed",
        }
    profile_path.mkdir(parents=True, exist_ok=True)
    log_path = Path("/tmp") / f"yingdao_chrome_cdp_{account_id}_{cdp_port}.log"
    cmd = [
        "open",
        "-na",
        "Google Chrome",
        "--args",
        f"--user-data-dir={profile_path}",
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://chat.deepseek.com/",
    ]
    with log_path.open("ab") as log:
        subprocess.run(cmd, stdout=log, stderr=log, check=False)
    ensured = False
    for _ in range(10):
        if cdp_collector.cdp_is_available(cdp_port):
            ensured = cdp_collector.cdp_ensure_deepseek_page(cdp_port)
            break
        time.sleep(0.5)
    return {
        "account_id": account_id,
        "cdp_port": cdp_port,
        "opened": True,
        "deepseek_opened": ensured,
        "message": "opened" if ensured else "opened_but_deepseek_page_not_ready",
        "log_path": str(log_path),
    }


def account_status(slot: dict[str, Any]) -> dict[str, Any]:
    status = cdp_collector.cdp_account_status(int(slot["cdp_port"]))
    return {
        **slot,
        "profile_dir": str(slot["profile_dir"]),
        "profile_path": str(account_profile_path(slot)),
        **status,
    }


def load_tasks() -> list[dict[str, Any]]:
    if not TASKS_PATH.exists():
        return []
    with TASKS_PATH.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    return data if isinstance(data, list) else []


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(tasks, ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix="tasks.", suffix=".json", dir=str(RUNTIME_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(payload)
    os.replace(temp_name, TASKS_PATH)


def append_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    event = {"time": now_text(), "type": event_type, "payload": payload or {}}
    with EVENTS_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(event, ensure_ascii=False) + "\n")


def normalize_task(raw: dict[str, Any], index: int) -> dict[str, Any]:
    created_at = now_text()
    task_uid = make_task_uid(raw, index)
    status = str(raw.get("status") or "pending").strip().lower()
    if status not in {"pending", "running", "success", "manual_required", "failed"}:
        status = "pending"
    return {
        "task_uid": task_uid,
        "source_row": raw.get("source_row") or index + 2,
        "id": str(raw.get("id") or f"Q{index + 1:03d}").strip(),
        "question": str(raw.get("question") or "").strip(),
        "platform": str(raw.get("platform") or "deepseek").strip().lower(),
        "round": str(raw.get("round") or "1").strip() or "1",
        "status": status,
        "created_at": created_at,
        "started_at": "",
        "finished_at": "",
        "updated_at": str(raw.get("updated_at") or created_at),
        "duration_seconds": "",
        "screenshot_path": str(raw.get("screenshot_path") or ""),
        "answer_text_path": str(raw.get("answer_text_path") or ""),
        "answer_url": str(raw.get("answer_url") or ""),
        "url_text_path": str(raw.get("url_text_path") or ""),
        "search_results_path": str(raw.get("search_results_path") or ""),
        "search_result_count": raw.get("search_result_count") or "",
        "search_read_count": raw.get("search_read_count") or "",
        "html_path": str(raw.get("html_path") or ""),
        "stage": str(raw.get("stage") or ""),
        "answer_text_length": raw.get("answer_text_length") or "",
        "screenshot_mode": str(raw.get("screenshot_mode") or ""),
        "collector": str(raw.get("collector") or ""),
        "worker_id": str(raw.get("worker_id") or ""),
        "account_id": str(raw.get("account_id") or ""),
        "cdp_port": raw.get("cdp_port") or "",
        "profile_dir": str(raw.get("profile_dir") or ""),
        "cdp_target_id": str(raw.get("cdp_target_id") or ""),
        "remark": str(raw.get("remark") or ""),
        "error": "",
        "attempt_count": 0,
        "last_run_at": "",
    }


def make_task_uid(raw: dict[str, Any], index: int) -> str:
    seed = "|".join(
        [
            str(raw.get("source_row") or index + 2),
            str(raw.get("id") or ""),
            str(raw.get("question") or ""),
            str(raw.get("round") or "1"),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"task_{index + 1:04d}_{digest}"


def task_summary() -> dict[str, Any]:
    global _pause_requested
    with _state_lock:
        tasks = load_tasks()
        runner_alive = _runner_thread is not None and _runner_thread.is_alive()
        if not runner_alive and _pause_requested:
            _pause_requested = False
        running = [task for task in tasks if task.get("status") == "running"]
        account_usage = {
            str(slot["account_id"]): len([task for task in running if task.get("account_id") == slot["account_id"]])
            for slot in ACCOUNT_SLOTS
        }
        accounts = [
            {
                **slot,
                "active": account_usage.get(str(slot["account_id"]), 0),
                "enabled": str(slot["account_id"]) in set(_runner_enabled_account_ids or default_enabled_account_ids()),
            }
            for slot in ACCOUNT_SLOTS
        ]
        current = running[0] if running else None
        pending = [task for task in tasks if task.get("status") == "pending"]
        executed = [task for task in tasks if task.get("status") in STATUSES_EXECUTED]
        return {
            "runner": {
                "running": runner_alive,
                "pause_requested": _pause_requested,
                "task_delay_seconds": TASK_DELAY_SECONDS,
                "task_delay_random_seconds": TASK_DELAY_RANDOM_SECONDS,
                "collector_mode": COLLECTOR_MODE,
                "max_workers": MAX_WORKERS,
                "active_workers": len(running),
                "accounts": accounts,
                "total_account_capacity": TOTAL_ACCOUNT_CAPACITY,
                "default_enabled_account_ids": default_enabled_account_ids(),
                "enabled_account_ids": _runner_enabled_account_ids or default_enabled_account_ids(),
            },
            "current": current,
            "running": running,
            "pending": pending,
            "executed": executed,
            "counts": {
                "total": len(tasks),
                "pending": len(pending),
                "running": len(running),
                "executed": len(executed),
                "success": len([task for task in tasks if task.get("status") == "success"]),
                "failed": len([task for task in tasks if task.get("status") == "failed"]),
                "manual_required": len([task for task in tasks if task.get("status") == "manual_required"]),
            },
        }


def update_task(task_uid: str, updates: dict[str, Any]) -> dict[str, Any]:
    with _state_lock:
        tasks = load_tasks()
        for task in tasks:
            if task.get("task_uid") != task_uid:
                continue
            task.update(updates)
            task["updated_at"] = now_text()
            save_tasks(tasks)
            return task
    raise KeyError(task_uid)


def collect_deepseek(task: dict[str, Any]) -> dict[str, Any]:
    result = cdp_collector.run_deepseek(task)
    result["collector"] = "cdp"
    return result


def run_pending_tasks(enabled_account_ids: list[str] | None = None) -> None:
    global _pause_requested, _runner_enabled_account_ids
    run_accounts = select_account_slots(enabled_account_ids)
    run_capacity = max(1, sum(int(slot["max_concurrency"]) for slot in run_accounts))
    run_max_workers = max(1, min(MAX_WORKERS, run_capacity))
    _runner_enabled_account_ids = [str(slot["account_id"]) for slot in run_accounts]
    append_event("runner_started", {"max_workers": run_max_workers, "accounts": run_accounts})
    try:
        active: dict[Any, dict[str, Any]] = {}
        disabled_accounts: set[str] = set()

        def next_worker_id() -> str:
            used = {meta["worker_id"] for meta in active.values()}
            for index in range(1, run_max_workers + 1):
                candidate = f"worker-{index}"
                if candidate not in used:
                    return candidate
            return f"worker-{len(active) + 1}"

        def active_count(account_id: str) -> int:
            return len([meta for meta in active.values() if meta.get("account_id") == account_id])

        def next_account_slot() -> dict[str, Any] | None:
            for slot in run_accounts:
                account_id = str(slot["account_id"])
                if account_id in disabled_accounts:
                    continue
                if active_count(account_id) >= int(slot["max_concurrency"]):
                    continue
                if not cdp_collector.cdp_is_available(int(slot["cdp_port"])):
                    disabled_accounts.add(account_id)
                    append_event("account_disabled_cdp_unavailable", {"account_id": account_id, "cdp_port": slot["cdp_port"]})
                    continue
                return slot
            return None

        def claim_pending_task(worker_id: str, account: dict[str, Any]) -> dict[str, Any] | None:
            with _state_lock:
                if _pause_requested:
                    return None
                tasks = load_tasks()
                task = next(
                    (
                        item
                        for item in tasks
                        if item.get("status") == "pending"
                        and item.get("platform") == "deepseek"
                        and item.get("question")
                    ),
                    None,
                )
                if not task:
                    return None
                started_at = now_text()
                task["status"] = "running"
                task["started_at"] = started_at
                task["finished_at"] = ""
                task["duration_seconds"] = ""
                task["last_run_at"] = started_at
                task["attempt_count"] = int(task.get("attempt_count") or 0) + 1
                task["worker_id"] = worker_id
                task["account_id"] = account["account_id"]
                task["cdp_port"] = account["cdp_port"]
                task["profile_dir"] = account["profile_dir"]
                task["cdp_target_id"] = ""
                task["remark"] = f"正在采集_{worker_id}_{account['account_id']}:{account['cdp_port']}"
                task["error"] = ""
                task["updated_at"] = started_at
                save_tasks(tasks)
                return dict(task)

        def exception_result(exc: Exception, meta: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "failed",
                "screenshot_path": "",
                "answer_text_path": "",
                "answer_url": "",
                "url_text_path": "",
                "search_results_path": "",
                "search_result_count": "",
                "search_read_count": "",
                "html_path": "",
                "stage": "collector_exception",
                "answer_text_length": "",
                "screenshot_mode": "",
                "collector": COLLECTOR_MODE,
                "worker_id": meta.get("worker_id") or "",
                "account_id": meta.get("account_id") or "",
                "cdp_port": meta.get("cdp_port") or "",
                "profile_dir": meta.get("profile_dir") or "",
                "cdp_target_id": "",
                "remark": f"run_deepseek_failed: {exc}",
                "error": str(exc),
            }

        def finish_task(task_uid: str, result: dict[str, Any], started_seconds: float) -> dict[str, Any]:
            finished_at = now_text()
            duration = round(time.time() - started_seconds, 1)
            updates = {
                "status": result.get("status") or "failed",
                "finished_at": finished_at,
                "duration_seconds": duration,
                "screenshot_path": result.get("screenshot_path") or "",
                "answer_text_path": result.get("answer_text_path") or "",
                "answer_url": result.get("answer_url") or "",
                "url_text_path": result.get("url_text_path") or "",
                "search_results_path": result.get("search_results_path") or "",
                "search_result_count": result.get("search_result_count") or "",
                "search_read_count": result.get("search_read_count") or "",
                "html_path": result.get("html_path") or "",
                "stage": result.get("stage") or "",
                "answer_text_length": result.get("answer_text_length") or "",
                "screenshot_mode": result.get("screenshot_mode") or "",
                "collector": result.get("collector") or "",
                "worker_id": result.get("worker_id") or "",
                "account_id": result.get("account_id") or "",
                "cdp_port": result.get("cdp_port") or "",
                "profile_dir": result.get("profile_dir") or "",
                "cdp_target_id": result.get("cdp_target_id") or "",
                "remark": result.get("remark") or "",
                "error": result.get("error") or "",
            }
            update_task(task_uid, updates)
            append_event("task_finished", {
                "task_uid": task_uid,
                "status": updates["status"],
                "duration_seconds": duration,
                "worker_id": updates["worker_id"],
                "account_id": updates["account_id"],
                "cdp_port": updates["cdp_port"],
            })
            return updates

        with ThreadPoolExecutor(max_workers=run_max_workers, thread_name_prefix="yingdao-worker") as executor:
            while True:
                with _state_lock:
                    pause_requested = _pause_requested
                while not pause_requested and len(active) < run_max_workers:
                    account = next_account_slot()
                    if not account:
                        break
                    worker_id = next_worker_id()
                    task = claim_pending_task(worker_id, account)
                    if not task:
                        break
                    task_uid = task["task_uid"]
                    append_event("task_started", {
                        "task_uid": task_uid,
                        "id": task.get("id"),
                        "worker_id": worker_id,
                        "account_id": account["account_id"],
                        "cdp_port": account["cdp_port"],
                    })
                    future = executor.submit(collect_deepseek, task)
                    active[future] = {
                        "task_uid": task_uid,
                        "worker_id": worker_id,
                        "account_id": account["account_id"],
                        "cdp_port": account["cdp_port"],
                        "profile_dir": account["profile_dir"],
                        "started_seconds": time.time(),
                    }
                    time.sleep(0.6)
                    with _state_lock:
                        pause_requested = _pause_requested

                if not active:
                    with _state_lock:
                        if _pause_requested:
                            append_event("runner_paused", {})
                            return
                    append_event("runner_idle", {})
                    return

                done, _pending = wait(active.keys(), timeout=1, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    meta = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = exception_result(exc, meta)
                    updates = finish_task(meta["task_uid"], result, meta["started_seconds"])
                    if updates["status"] == "manual_required":
                        disabled_accounts.add(str(updates.get("account_id") or meta["account_id"]))
                        append_event("account_paused_manual_required", {
                            "task_uid": meta["task_uid"],
                            "worker_id": meta["worker_id"],
                            "account_id": updates.get("account_id") or meta["account_id"],
                            "cdp_port": updates.get("cdp_port") or meta["cdp_port"],
                        })
                    elif updates.get("stage") == "deepseek_busy" or "deepseek_busy" in str(updates.get("remark") or ""):
                        disabled_accounts.add(str(updates.get("account_id") or meta["account_id"]))
                        append_event("account_paused_deepseek_busy", {
                            "task_uid": meta["task_uid"],
                            "worker_id": meta["worker_id"],
                            "account_id": updates.get("account_id") or meta["account_id"],
                            "cdp_port": updates.get("cdp_port") or meta["cdp_port"],
                        })
    finally:
        with _state_lock:
            _pause_requested = False
            _runner_enabled_account_ids = []
        append_event("runner_finished", {})


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.get("/api/tasks")
def api_tasks() -> dict[str, Any]:
    return task_summary()


@app.get("/api/tasks/{task_uid}")
def api_task(task_uid: str) -> dict[str, Any]:
    with _state_lock:
        task = next((item for item in load_tasks() if item.get("task_uid") == task_uid), None)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.post("/api/import-default")
def api_import_default() -> dict[str, Any]:
    return import_workbook(DEFAULT_WORKBOOK)


@app.post("/api/import-excel")
async def api_import_excel(request: Request) -> dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = RUNTIME_DIR / f"uploaded_{int(time.time())}.xlsx"
    source_label = request.headers.get("x-filename") or "uploaded workbook"
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty upload body")
    temp_path.write_bytes(body)
    try:
        return import_workbook(temp_path, source_label=source_label)
    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass


def import_workbook(path: Path, source_label: str | None = None) -> dict[str, Any]:
    rows = read_tasks_xlsx(path)
    tasks = [normalize_task(row, index) for index, row in enumerate(rows)]
    with _state_lock:
        save_tasks(tasks)
    source = source_label or str(path)
    append_event("excel_imported", {"path": source, "count": len(tasks)})
    return {"ok": True, "count": len(tasks), "path": source}


async def read_json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.body()
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.get("/api/accounts/status")
def api_accounts_status() -> dict[str, Any]:
    accounts = [account_status(slot) for slot in ACCOUNT_SLOTS]
    return {
        "ok": True,
        "accounts": accounts,
        "default_enabled_account_ids": default_enabled_account_ids(),
    }


@app.post("/api/accounts/open")
async def api_accounts_open(request: Request) -> dict[str, Any]:
    payload = await read_json_body(request)
    enabled_ids = parse_enabled_account_ids(payload)
    slots = select_account_slots(enabled_ids)
    if not slots:
        return {"ok": False, "message": "请至少选择一个账号"}
    results = [open_account_chrome(slot) for slot in slots]
    append_event("accounts_open_requested", {"enabled_account_ids": enabled_ids, "results": results})
    return {"ok": True, "enabled_account_ids": enabled_ids, "results": results}


@app.post("/api/run")
async def api_run(request: Request) -> dict[str, Any]:
    global _runner_thread, _pause_requested, _runner_enabled_account_ids
    payload = await read_json_body(request)
    enabled_ids = parse_enabled_account_ids(payload)
    selected_accounts = select_account_slots(enabled_ids)
    if not selected_accounts:
        return {"ok": False, "message": "请至少选择一个账号"}
    with _state_lock:
        if _runner_thread is not None and _runner_thread.is_alive():
            return {"ok": False, "message": "已有采集任务正在运行"}
        tasks = load_tasks()
        if not tasks:
            return {"ok": False, "message": "还没有导入任务。请先点击“导入默认 Excel”，或选择文件后点击“上传导入”。"}
        pending = [
            task
            for task in tasks
            if task.get("status") == "pending"
            and task.get("platform") == "deepseek"
            and task.get("question")
        ]
        if not pending:
            return {"ok": False, "message": "没有可执行的 pending/deepseek 任务。请先导入任务，或把需要重跑的任务重新排队。"}
        available_accounts = [
            slot
            for slot in selected_accounts
            if cdp_collector.cdp_is_available(int(slot["cdp_port"]))
        ]
        if not available_accounts:
            ports = ", ".join(str(slot["cdp_port"]) for slot in selected_accounts)
            return {"ok": False, "message": f"选中的账号窗口还没有启动。请先点击“打开账号窗口”，或确认端口 {ports} 已启动。"}
        _pause_requested = False
        _runner_enabled_account_ids = [str(slot["account_id"]) for slot in selected_accounts]
        _runner_thread = threading.Thread(target=run_pending_tasks, args=(_runner_enabled_account_ids,), name="yingdao-runner", daemon=True)
        _runner_thread.start()
    return {"ok": True, "message": "采集已启动", "enabled_account_ids": _runner_enabled_account_ids}


@app.post("/api/pause")
def api_pause() -> dict[str, Any]:
    global _pause_requested
    with _state_lock:
        runner_alive = _runner_thread is not None and _runner_thread.is_alive()
        if not runner_alive:
            _pause_requested = False
            return {"ok": True, "message": "后台未运行，无需暂停"}
        _pause_requested = True
    append_event("pause_requested", {})
    return {"ok": True, "message": "已请求暂停；当前任务会先跑完"}


@app.post("/api/tasks/{task_uid}/retry")
def api_retry(task_uid: str) -> dict[str, Any]:
    with _state_lock:
        current = next((item for item in load_tasks() if item.get("task_uid") == task_uid), None)
    if not current:
        raise HTTPException(status_code=404, detail="task not found")
    if current.get("status") not in {"failed", "manual_required"}:
        raise HTTPException(status_code=400, detail="only failed/manual_required tasks can be retried")
    try:
        task = update_task(
            task_uid,
            {
                "status": "pending",
                "started_at": "",
                "finished_at": "",
                "duration_seconds": "",
                "stage": "",
                "answer_text_length": "",
                "screenshot_mode": "",
                "search_results_path": "",
                "search_result_count": "",
                "search_read_count": "",
                "worker_id": "",
                "account_id": "",
                "cdp_port": "",
                "profile_dir": "",
                "cdp_target_id": "",
                "remark": "已重新排队",
                "error": "",
            },
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found") from None
    append_event("task_retry", {"task_uid": task_uid})
    return {"ok": True, "task": task}


@app.get("/api/export-excel")
def api_export_excel() -> FileResponse:
    with _state_lock:
        tasks = load_tasks()
    if not tasks:
        raise HTTPException(status_code=400, detail="no tasks to export")
    write_tasks_xlsx(EXPORT_PATH, tasks)
    append_event("excel_exported", {"path": str(EXPORT_PATH), "count": len(tasks)})
    return FileResponse(
        str(EXPORT_PATH),
        filename="yingdao_tasks_export.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/file")
def api_file(path: str) -> FileResponse:
    target = Path(path).expanduser().resolve()
    allowed_roots = [BASE_DIR.resolve(), RESULTS_DIR.resolve(), RUNTIME_DIR.resolve()]
    if not any(str(target).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="file path outside allowed directories")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(target), filename=target.name)


INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>yingdao_mvp 采集后台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef2f6;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --line: #d7dde6;
      --line-soft: #e9edf3;
      --text: #172033;
      --muted: #687386;
      --blue: #2563eb;
      --blue-soft: #eef4ff;
      --green: #16833a;
      --green-soft: #effaf2;
      --red: #b42318;
      --red-soft: #fff4f2;
      --amber: #a15c07;
      --amber-soft: #fff8e8;
      --shadow: 0 10px 30px rgba(23, 32, 51, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 22px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 2;
      box-shadow: 0 1px 0 rgba(23, 32, 51, .03);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 260px;
    }
    .brand-mark {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #162033;
      color: #ffffff;
      font-weight: 700;
      letter-spacing: 0;
    }
    h1 { font-size: 17px; line-height: 1.2; margin: 0; }
    .subtitle { color: var(--muted); font-size: 12px; margin-top: 3px; }
    button, .button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      min-height: 34px;
      padding: 7px 12px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 14px;
      transition: background .15s ease, border-color .15s ease, color .15s ease, box-shadow .15s ease;
    }
    button:hover, .button:hover { border-color: #b8c2d2; box-shadow: 0 1px 2px rgba(23, 32, 51, .08); }
    button.primary { background: var(--blue); color: #fff; border-color: var(--blue); }
    button.primary:hover { background: #1d4ed8; border-color: #1d4ed8; }
    button.danger { color: var(--red); background: var(--red-soft); border-color: #f2b8b5; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    main {
      display: grid;
      grid-template-columns: minmax(420px, 1fr) minmax(440px, .95fr);
      gap: 18px;
      padding: 18px;
      max-width: 1480px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .section-head {
      min-height: 48px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 11px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .runner-banner {
      margin: 12px 14px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #f8fafc;
      color: var(--muted);
      font-size: 13px;
    }
    .runner-banner.running { background: var(--blue-soft); border-color: #c8d9ff; color: #1d4ed8; }
    .runner-banner.pause-requested { background: var(--amber-soft); border-color: #f3c87b; color: #92400e; }
    .runner-banner.idle { background: #f8fafc; border-color: var(--line); color: var(--muted); }
    .runner-banner.paused { background: var(--green-soft); border-color: #bfe6c7; color: #166534; }
    .account-panel {
      padding: 14px;
      display: grid;
      gap: 12px;
      background: #fff;
    }
    .segmented {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      width: fit-content;
      background: #fff;
    }
    .segmented button {
      border: 0;
      border-right: 1px solid var(--line);
      border-radius: 0;
      min-width: 86px;
      justify-content: center;
      box-shadow: none;
    }
    .segmented button:last-child { border-right: 0; }
    .segmented button.active { background: var(--blue); color: #fff; }
    .account-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .account-list {
      display: grid;
      gap: 8px;
    }
    .account-item {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) auto;
      gap: 10px;
      align-items: center;
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--panel-soft);
    }
    .account-item.disabled { opacity: .55; }
    .account-name { font-weight: 650; }
    .account-sub { color: var(--muted); font-size: 12px; margin-top: 2px; overflow-wrap: anywhere; }
    h2 { font-size: 14px; margin: 0; letter-spacing: 0; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }
    .toolbar input[type=file] {
      border: 1px dashed #b8c2d2;
      background: #f8fafc;
      border-radius: 6px;
      padding: 6px;
      max-width: 250px;
      min-height: 34px;
    }
    .grid { display: grid; gap: 18px; }
    .summary {
      display: grid;
      grid-template-columns: repeat(6, minmax(80px, 1fr));
      gap: 10px;
      padding: 14px;
    }
    .metric {
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      min-height: 76px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .metric strong { display: block; font-size: 24px; line-height: 1; }
    .metric span { color: var(--muted); font-size: 12px; }
    .metric.total { border-top: 3px solid #475569; }
    .metric.pending { border-top: 3px solid var(--blue); }
    .metric.running { border-top: 3px solid #7c3aed; }
    .metric.success { border-top: 3px solid var(--green); }
    .metric.manual_required { border-top: 3px solid var(--amber); }
    .metric.failed { border-top: 3px solid var(--red); }
    .count-pill {
      color: var(--muted);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      background: #fff;
    }
    .task-list { max-height: 430px; overflow: auto; background: #fff; }
    .task {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line-soft);
      cursor: pointer;
      transition: background .12s ease;
    }
    .task:hover { background: #f8fafc; }
    .task.selected {
      background: var(--blue-soft);
      box-shadow: inset 3px 0 0 var(--blue);
    }
    .task-title {
      font-weight: 650;
      overflow-wrap: anywhere;
      color: #111827;
      line-height: 1.45;
    }
    .task-meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 7px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .status {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: #fff;
      font-weight: 600;
    }
    .status.success { color: var(--green); border-color: #9bd8ad; background: var(--green-soft); }
    .status.failed { color: var(--red); border-color: #f2b8b5; background: var(--red-soft); }
    .status.manual_required { color: var(--amber); border-color: #f3c87b; background: var(--amber-soft); }
    .status.running { color: var(--blue); border-color: #b6cff7; background: var(--blue-soft); }
    .empty {
      color: var(--muted);
      padding: 22px 14px;
      background: repeating-linear-gradient(135deg, #fff, #fff 10px, #fafbfc 10px, #fafbfc 20px);
    }
    .detail {
      padding: 14px;
      display: grid;
      gap: 8px;
      max-height: calc(100vh - 126px);
      overflow: auto;
    }
    .row {
      display: grid;
      grid-template-columns: 118px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      padding: 9px 0;
      border-bottom: 1px solid var(--line-soft);
    }
    .row:last-child { border-bottom: 0; }
    .label { color: var(--muted); font-size: 12px; padding-top: 2px; }
    .value { overflow-wrap: anywhere; white-space: pre-wrap; color: #111827; }
    .paths a {
      color: var(--blue);
      text-decoration: none;
      border-bottom: 1px solid rgba(37, 99, 235, .25);
    }
    .current-card { padding: 14px; min-height: 74px; }
    .current-card .task-title { font-size: 15px; }
    @media (max-width: 1100px) {
      main { grid-template-columns: 1fr; }
      .detail { max-height: none; }
    }
    @media (max-width: 760px) {
      header { align-items: flex-start; flex-direction: column; }
      .toolbar { justify-content: flex-start; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .row { grid-template-columns: 1fr; gap: 3px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark">GEO</div>
      <div>
        <h1>DeepSeek GEO 采集后台</h1>
        <div class="subtitle">Excel 导入 · 多账号并发采集 · 结果归档</div>
      </div>
    </div>
    <div class="toolbar">
      <button id="importDefaultBtn">导入默认 Excel</button>
      <input id="fileInput" type="file" accept=".xlsx">
      <button id="uploadBtn">上传导入</button>
      <button id="runBtn" class="primary">开始采集</button>
      <button id="pauseBtn" class="danger">暂停</button>
      <a class="button" href="/api/export-excel">导出 Excel</a>
    </div>
  </header>
  <main>
    <div class="grid">
      <section>
        <div class="section-head">
          <h2>账号准备</h2>
          <button id="refreshAccountsBtn">检测状态</button>
        </div>
        <div class="account-panel">
          <div>
            <div class="label">本轮账号数量</div>
            <div id="accountCountGroup" class="segmented"></div>
          </div>
          <div class="account-actions">
            <button id="openAccountsBtn" class="primary">打开账号窗口</button>
            <span id="accountHint" class="subtitle">默认启用 2 个账号。</span>
          </div>
          <div id="accountsList" class="account-list"></div>
        </div>
      </section>
      <section>
        <div class="section-head"><h2>任务概览</h2><span id="runnerState" class="status">未运行</span></div>
        <div id="runnerBanner" class="runner-banner idle">后台未运行。</div>
        <div class="summary" id="summary"></div>
      </section>
      <section>
        <div class="section-head"><h2>当前任务</h2></div>
        <div id="current" class="current-card"></div>
      </section>
      <section>
        <div class="section-head"><h2>待执行动作</h2><span id="pendingCount" class="count-pill"></span></div>
        <div id="pending" class="task-list"></div>
      </section>
      <section>
        <div class="section-head"><h2>已执行任务</h2><span id="executedCount" class="count-pill"></span></div>
        <div id="executed" class="task-list"></div>
      </section>
    </div>
    <section>
      <div class="section-head">
        <h2>任务详情</h2>
        <button id="retryBtn" style="display:none">重新排队</button>
      </div>
      <div id="detail" class="detail empty">选择一个任务查看数据情况。</div>
    </section>
  </main>
  <script>
    let selectedTaskUid = null;
    let selectedTask = null;
    let accountStatuses = [];
    let selectedAccountCount = Number(localStorage.getItem('yingdaoAccountCount') || '2') || 2;

    const statusText = {
      pending: '待执行',
      running: '正在采集',
      success: '完成',
      manual_required: '人工处理',
      failed: '失败'
    };

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      return response.json();
    }

    function statusBadge(status) {
      const safe = status || 'pending';
      return `<span class="status ${safe}">${statusText[safe] || safe}</span>`;
    }

    function fileLink(path) {
      if (!path) return '';
      return `<a href="/api/file?path=${encodeURIComponent(path)}" target="_blank">${escapeHtml(path)}</a>`;
    }

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[ch]));
    }

    function selectedAccountIds() {
      return accountStatuses.slice(0, selectedAccountCount).map(account => account.account_id);
    }

    function accountStatusBadge(account, enabled) {
      if (!enabled) return '<span class="status">未启用</span>';
      if (!account.cdp_available) return '<span class="status failed">未启动</span>';
      if (!account.deepseek_open) return '<span class="status manual_required">未打开</span>';
      if (account.gate_reason) return '<span class="status manual_required">需登录/验证</span>';
      if (account.ready) return '<span class="status success">可用</span>';
      return '<span class="status manual_required">待确认</span>';
    }

    function renderAccountControls() {
      const maxCount = Math.max(1, accountStatuses.length || 3);
      selectedAccountCount = Math.max(1, Math.min(selectedAccountCount, maxCount));
      localStorage.setItem('yingdaoAccountCount', String(selectedAccountCount));
      document.getElementById('accountCountGroup').innerHTML = Array.from({ length: maxCount }, (_, index) => {
        const count = index + 1;
        const active = count === selectedAccountCount ? ' active' : '';
        return `<button class="${active}" data-account-count="${count}">${count} 个账号</button>`;
      }).join('');
      document.querySelectorAll('[data-account-count]').forEach(button => {
        button.onclick = () => {
          selectedAccountCount = Number(button.dataset.accountCount || '2');
          localStorage.setItem('yingdaoAccountCount', String(selectedAccountCount));
          renderAccountControls();
        };
      });
      const enabledIds = new Set(selectedAccountIds());
      document.getElementById('accountsList').innerHTML = accountStatuses.length ? accountStatuses.map(account => {
        const enabled = enabledIds.has(account.account_id);
        const disabledClass = enabled ? '' : ' disabled';
        return `
          <div class="account-item${disabledClass}">
            <div>
              <div class="account-name">${escapeHtml(account.account_id)} · ${escapeHtml(account.cdp_port)}</div>
              <div class="account-sub">${escapeHtml(account.profile_dir)} · 并发 ${escapeHtml(account.max_concurrency)}</div>
              ${account.current_url ? `<div class="account-sub">${escapeHtml(account.current_url)}</div>` : ''}
            </div>
            <div>${accountStatusBadge(account, enabled)}</div>
          </div>
        `;
      }).join('') : '<div class="empty">还没有读取账号配置。</div>';
      const selectedNames = selectedAccountIds().join('，');
      document.getElementById('accountHint').textContent = selectedNames ? `本轮启用：${selectedNames}` : '请选择本轮账号数量。';
    }

    async function loadAccountStatus() {
      const data = await api('/api/accounts/status');
      accountStatuses = data.accounts || [];
      if (!localStorage.getItem('yingdaoAccountCount')) {
        selectedAccountCount = (data.default_enabled_account_ids || []).length || 2;
      }
      renderAccountControls();
      return data;
    }

    function renderTask(task) {
      const selected = task.task_uid === selectedTaskUid ? ' selected' : '';
      return `
        <div class="task${selected}" data-task="${task.task_uid}">
          <div class="task-title">${escapeHtml(task.question)}</div>
          <div class="task-meta">
            ${statusBadge(task.status)}
          <span>${escapeHtml(task.id || '')}</span>
            ${task.account_id ? `<span>${escapeHtml(task.account_id)}:${escapeHtml(task.cdp_port || '')}</span>` : ''}
            <span>row ${escapeHtml(task.source_row || '')}</span>
            <span>round ${escapeHtml(task.round || '')}</span>
          </div>
        </div>`;
    }

    function bindTaskClicks() {
      document.querySelectorAll('[data-task]').forEach(el => {
        el.onclick = () => {
          selectedTaskUid = el.dataset.task;
          loadDetail(selectedTaskUid);
        };
      });
    }

    function renderSummary(counts) {
      const items = [
        ['total', '全部'],
        ['pending', '待执行'],
        ['running', '当前'],
        ['success', '完成'],
        ['manual_required', '人工处理'],
        ['failed', '失败']
      ];
      document.getElementById('summary').innerHTML = items.map(([key, label]) => `
        <div class="metric ${key}"><strong>${counts[key] || 0}</strong><span>${label}</span></div>
      `).join('');
    }

    function renderCurrent(task) {
      const node = document.getElementById('current');
      if (!task) {
        node.innerHTML = '<div class="empty">当前没有正在执行的任务。</div>';
        return;
      }
      node.innerHTML = `
        <div class="task-title">${escapeHtml(task.question)}</div>
        <div class="task-meta">
          ${statusBadge(task.status)}
          <span>${escapeHtml(task.id)}</span>
          ${task.account_id ? `<span>${escapeHtml(task.account_id)}:${escapeHtml(task.cdp_port || '')}</span>` : ''}
          <span>开始：${escapeHtml(task.started_at)}</span>
          <span>执行次数：${escapeHtml(task.attempt_count)}</span>
        </div>
      `;
    }

    function renderDetail(task) {
      selectedTask = task;
      const node = document.getElementById('detail');
      const retryBtn = document.getElementById('retryBtn');
      retryBtn.style.display = ['failed', 'manual_required'].includes(task.status) ? 'inline-flex' : 'none';
      node.classList.remove('empty');
      node.innerHTML = `
        ${detailRow('任务 ID', task.id)}
        ${detailRow('Excel 行号', task.source_row)}
        ${detailRow('问题', task.question)}
        ${detailRow('平台', task.platform)}
        ${detailRow('轮次', task.round)}
        ${detailRow('状态', statusBadge(task.status), true)}
        ${detailRow('开始时间', task.started_at)}
        ${detailRow('完成时间', task.finished_at)}
        ${detailRow('耗时', task.duration_seconds ? task.duration_seconds + ' 秒' : '')}
        ${detailRow('执行次数', task.attempt_count)}
        ${detailRow('Worker', task.worker_id)}
        ${detailRow('账号', task.account_id)}
        ${detailRow('CDP 端口', task.cdp_port)}
        ${detailRow('Profile', task.profile_dir)}
        ${detailRow('CDP Target', task.cdp_target_id)}
        ${detailRow('采集器', task.collector)}
        ${detailRow('阶段', task.stage)}
        ${detailRow('文本长度', task.answer_text_length)}
        ${detailRow('截图模式', task.screenshot_mode)}
        ${detailRow('搜索已读网页数', task.search_read_count)}
        ${detailRow('搜索结果数量', task.search_result_count)}
        ${detailRow('截图路径', fileLink(task.screenshot_path), true)}
        ${detailRow('回答文本', fileLink(task.answer_text_path), true)}
        ${detailRow('对话链接', task.answer_url ? `<a href="${escapeHtml(task.answer_url)}" target="_blank">${escapeHtml(task.answer_url)}</a>` : '', true)}
        ${detailRow('链接文件', fileLink(task.url_text_path), true)}
        ${detailRow('搜索结果 JSON', fileLink(task.search_results_path), true)}
        ${detailRow('HTML 片段', fileLink(task.html_path), true)}
        ${detailRow('备注', task.remark)}
        ${detailRow('错误', task.error)}
      `;
    }

    function detailRow(label, value, raw = false) {
      return `<div class="row"><div class="label">${label}</div><div class="value paths">${raw ? (value || '') : escapeHtml(value || '')}</div></div>`;
    }

    async function loadDetail(taskUid) {
      const task = await api(`/api/tasks/${encodeURIComponent(taskUid)}`);
      renderDetail(task);
      await refresh();
    }

    async function refresh() {
      const data = await api('/api/tasks');
      renderSummary(data.counts);
      renderCurrent(data.current);
      renderRunnerState(data.runner);
      document.getElementById('pendingCount').textContent = data.pending.length;
      document.getElementById('executedCount').textContent = data.executed.length;
      document.getElementById('pending').innerHTML = data.pending.length ? data.pending.map(renderTask).join('') : '<div class="empty">没有待执行任务。</div>';
      document.getElementById('executed').innerHTML = data.executed.length ? data.executed.map(renderTask).join('') : '<div class="empty">没有已执行任务。</div>';
      bindTaskClicks();
      if (selectedTaskUid) {
        const all = [...data.pending, ...data.executed, ...(data.running || [])];
        const fresh = all.find(task => task.task_uid === selectedTaskUid);
        if (fresh) renderDetail(fresh);
      }
    }

    function renderRunnerState(runner) {
      const badge = document.getElementById('runnerState');
      const banner = document.getElementById('runnerBanner');
      if (runner.running && runner.pause_requested) {
        badge.textContent = '暂停中';
        badge.className = 'status manual_required';
        banner.textContent = `已请求暂停，运行中的 ${runner.active_workers || 0} 个任务完成后会停止继续执行。`;
        banner.className = 'runner-banner pause-requested';
        return;
      }
      if (runner.running) {
        badge.textContent = '运行中';
        badge.className = 'status running';
        const accountText = (runner.accounts || []).map(item => `${item.account_id} ${item.active || 0}/${item.max_concurrency}`).join('，');
        banner.textContent = `后台正在多账号并发采集：${runner.active_workers || 0}/${runner.max_workers || 1} 个 worker 正在运行。${accountText ? '账号：' + accountText : ''}`;
        banner.className = 'runner-banner running';
        return;
      }
      if (runner.pause_requested) {
        badge.textContent = '暂停请求';
        badge.className = 'status manual_required';
        banner.textContent = '暂停请求已提交，等待当前任务结束。';
        banner.className = 'runner-banner pause-requested';
        return;
      }
      badge.textContent = '未运行';
      badge.className = 'status';
      banner.textContent = '后台未运行。';
      banner.className = 'runner-banner idle';
    }

    document.getElementById('importDefaultBtn').onclick = async () => {
      const result = await api('/api/import-default', { method: 'POST' });
      selectedTaskUid = null;
      await refresh();
      const banner = document.getElementById('runnerBanner');
      banner.textContent = `已导入 ${result.count || 0} 条任务，可以开始采集。`;
      banner.className = 'runner-banner paused';
    };

    document.getElementById('fileInput').onchange = () => {
      const file = document.getElementById('fileInput').files[0];
      const banner = document.getElementById('runnerBanner');
      if (!file) return;
      banner.textContent = `已选择 ${file.name}，还需要点击“上传导入”才会进入任务列表。`;
      banner.className = 'runner-banner pause-requested';
    };

    document.getElementById('uploadBtn').onclick = async () => {
      const file = document.getElementById('fileInput').files[0];
      if (!file) return alert('请选择 .xlsx 文件');
      const result = await api('/api/import-excel', {
        method: 'POST',
        headers: {
          'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'x-filename': encodeURIComponent(file.name)
        },
        body: file
      });
      selectedTaskUid = null;
      await refresh();
      const banner = document.getElementById('runnerBanner');
      banner.textContent = `已导入 ${result.count || 0} 条任务，可以开始采集。`;
      banner.className = 'runner-banner paused';
    };

    document.getElementById('refreshAccountsBtn').onclick = async () => {
      const banner = document.getElementById('runnerBanner');
      banner.textContent = '正在检测账号窗口状态。';
      banner.className = 'runner-banner idle';
      await loadAccountStatus();
      banner.textContent = '账号状态已更新。';
      banner.className = 'runner-banner paused';
    };

    document.getElementById('openAccountsBtn').onclick = async () => {
      if (!accountStatuses.length) await loadAccountStatus();
      const ids = selectedAccountIds();
      if (!ids.length) return alert('请至少选择一个账号');
      const banner = document.getElementById('runnerBanner');
      banner.textContent = `正在打开账号窗口：${ids.join('，')}`;
      banner.className = 'runner-banner running';
      const result = await api('/api/accounts/open', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ enabled_account_ids: ids })
      });
      if (!result.ok) {
        alert(result.message || '打开账号窗口失败');
        banner.textContent = result.message || '打开账号窗口失败';
        banner.className = 'runner-banner pause-requested';
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1200));
      await loadAccountStatus();
      banner.textContent = '账号窗口已打开。请在对应 Chrome 窗口完成 DeepSeek 登录后，再点击“开始采集”。';
      banner.className = 'runner-banner paused';
    };

    document.getElementById('runBtn').onclick = async () => {
      if (!accountStatuses.length) await loadAccountStatus();
      const ids = selectedAccountIds();
      if (!ids.length) return alert('请至少选择一个账号');
      const result = await api('/api/run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ enabled_account_ids: ids })
      });
      if (!result.ok) {
        alert(result.message || '启动失败');
        const banner = document.getElementById('runnerBanner');
        banner.textContent = result.message || '启动失败';
        banner.className = 'runner-banner pause-requested';
      }
      await refresh();
    };

    document.getElementById('pauseBtn').onclick = async () => {
      const result = await api('/api/pause', { method: 'POST' });
      const banner = document.getElementById('runnerBanner');
      banner.textContent = result.message || '已请求暂停。';
      banner.className = 'runner-banner pause-requested';
      await refresh();
    };

    document.getElementById('retryBtn').onclick = async () => {
      if (!selectedTask) return;
      await api(`/api/tasks/${encodeURIComponent(selectedTask.task_uid)}/retry`, { method: 'POST' });
      await refresh();
    };

    loadAccountStatus().catch(() => {});
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, reload=False)
