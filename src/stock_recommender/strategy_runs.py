from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable

from .parameters import find_strategy_config, strategy_config_path


RUN_LOCK = threading.Lock()
ACTIVE_RUN_IDS: set[str] = set()
MAX_RUNS = 20


class StrategyRunInProgressError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def strategy_runs_path() -> Path:
    configured = os.getenv("STOCK_AGENT_RUNS_PATH", "").strip()
    return Path(configured).expanduser() if configured else strategy_config_path().with_name("strategy_runs.json")


def _load_runs_unlocked(path: str | Path | None = None) -> list[dict]:
    target = Path(path) if path is not None else strategy_runs_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _save_runs_unlocked(runs: list[dict], path: str | Path | None = None) -> None:
    target = Path(path) if path is not None else strategy_runs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(runs[:MAX_RUNS], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def list_strategy_runs(strategy_id: str, *, path: str | Path | None = None) -> list[dict]:
    with RUN_LOCK:
        runs = [run for run in _load_runs_unlocked(path) if run.get("strategy_id") == strategy_id]
    return [_run_summary(run) for run in runs]


def get_strategy_run(run_id: str, *, path: str | Path | None = None) -> dict | None:
    with RUN_LOCK:
        run = next((item for item in _load_runs_unlocked(path) if item.get("id") == run_id), None)
    return deepcopy(run) if run else None


def _run_summary(run: dict) -> dict:
    return {key: deepcopy(run.get(key)) for key in [
        "id", "strategy_id", "strategy_name", "status", "created_at", "started_at", "completed_at", "duration_seconds", "error"
    ]}


def _update_run(run_id: str, patch: dict, *, path: str | Path | None = None) -> dict:
    with RUN_LOCK:
        runs = _load_runs_unlocked(path)
        for run in runs:
            if run.get("id") == run_id:
                run.update(patch)
                _save_runs_unlocked(runs, path)
                return deepcopy(run)
    raise KeyError(run_id)


def run_strategy_command(strategy_id: str) -> str:
    python_bin = os.getenv("STOCK_AGENT_RUN_PYTHON", "").strip()
    if not python_bin or not Path(python_bin).is_file():
        python_bin = sys.executable
    env = os.environ.copy()
    env.update(
        {
            "STOCK_AGENT_MODE": os.getenv("STOCK_AGENT_RUN_MODE", "ai"),
            "STOCK_AGENT_STRATEGY_ID": strategy_id,
            "STOCK_AGENT_OUTPUT": "",
            "STOCK_AGENT_CANDIDATE_LIMIT": os.getenv("STOCK_AGENT_CANDIDATE_LIMIT", "6"),
            "STOCK_AGENT_ENABLE_TICK": os.getenv("STOCK_AGENT_ENABLE_TICK", "1"),
            "STOCK_AGENT_TICK_LIMIT": os.getenv("STOCK_AGENT_TICK_LIMIT", "2"),
        }
    )
    completed = subprocess.run(
        [python_bin, "-m", "stock_recommender.cli"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=int(os.getenv("STOCK_AGENT_RUN_TIMEOUT", "180")),
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "策略执行失败").strip()[-4000:])
    return completed.stdout.strip()


def _execute_run(run_id: str, executor: Callable[[str], str], *, path: str | Path | None = None) -> None:
    started = datetime.now().astimezone()
    _update_run(run_id, {"status": "running", "started_at": started.isoformat(timespec="seconds")}, path=path)
    try:
        current = get_strategy_run(run_id, path=path)
        report = executor(current["strategy_id"])
        status, error = "succeeded", None
    except Exception as exc:
        report, status, error = "", "failed", str(exc)[:4000]
    completed = datetime.now().astimezone()
    try:
        _update_run(
            run_id,
            {
                "status": status,
                "report": report,
                "error": error,
                "completed_at": completed.isoformat(timespec="seconds"),
                "duration_seconds": round((completed - started).total_seconds(), 1),
            },
            path=path,
        )
    finally:
        with RUN_LOCK:
            ACTIVE_RUN_IDS.discard(run_id)


def start_strategy_run(
    strategy_id: str,
    *,
    path: str | Path | None = None,
    config_path: str | Path | None = None,
    executor: Callable[[str], str] | None = None,
) -> dict:
    strategy = find_strategy_config(strategy_id, path=config_path)
    if strategy is None:
        raise KeyError(strategy_id)
    with RUN_LOCK:
        runs = _load_runs_unlocked(path)
        if ACTIVE_RUN_IDS:
            raise StrategyRunInProgressError("已有策略正在执行，请等待完成")
        for previous in runs:
            if previous.get("status") in {"queued", "running"}:
                previous.update({"status": "failed", "completed_at": _timestamp(), "error": "服务重启，运行已中断"})
        run = {
            "id": uuid.uuid4().hex,
            "strategy_id": strategy["id"],
            "strategy_name": strategy["name"],
            "status": "queued",
            "created_at": _timestamp(),
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "report": "",
            "error": None,
        }
        runs.insert(0, run)
        ACTIVE_RUN_IDS.add(run["id"])
        _save_runs_unlocked(runs, path)
    thread = threading.Thread(target=_execute_run, args=(run["id"], executor or run_strategy_command), kwargs={"path": path}, daemon=True)
    thread.start()
    return deepcopy(run)
