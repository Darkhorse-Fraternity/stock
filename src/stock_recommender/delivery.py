from __future__ import annotations

import os
import subprocess
from typing import Callable

from .markets import US_MARKET, strategy_market
from .parameters import load_strategy_config, normalize_report_delivery
from .runtime import strategy_runtime_issues


PLATFORM_CHANNELS = {"feishu", "telegram", "discord", "signal"}
PORTFOLIO_TRACKING_CRON = "0 2,3,5,6,7 * * 1-5"
PORTFOLIO_RISK_CRON = "*/5 1-7 * * 1-5"
US_PORTFOLIO_TRACKING_CRON = "0 13-21 * * 1-5"
US_PORTFOLIO_RISK_CRON = "*/5 13-21 * * 1-5"


def portfolio_delivery_crons(config: dict) -> tuple[str, str]:
    """Return broad UTC windows; the exchange-local schedule guard is final."""

    if strategy_market(config) == US_MARKET:
        # Covers both EDT (UTC-4) and EST (UTC-5). Invocations outside the
        # active New York session are rejected by the market schedule guard.
        return US_PORTFOLIO_TRACKING_CRON, US_PORTFOLIO_RISK_CRON
    return PORTFOLIO_TRACKING_CRON, PORTFOLIO_RISK_CRON


def delivery_target(config: dict) -> str:
    delivery = normalize_report_delivery(config.get("delivery"))
    channel = delivery["channel"]
    target = delivery["target"]
    if channel in PLATFORM_CHANNELS:
        if not target:
            raise ValueError(f"{channel} 推送需要填写接收目标")
        return target if target.startswith(f"{channel}:") else f"{channel}:{target}"
    return channel


def delivery_cron(config: dict) -> str:
    delivery = normalize_report_delivery(config.get("delivery"))
    local_minutes = delivery["hour"] * 60 + delivery["minute"]
    utc_minutes = (local_minutes - 8 * 60) % (24 * 60)
    hour, minute = divmod(utc_minutes, 60)
    weekdays = ("0-4" if local_minutes < 8 * 60 else "1-5") if delivery["frequency"] == "weekdays" else "*"
    return f"{minute} {hour} * * {weekdays}"


def should_deliver_report(report: str, config: dict) -> bool:
    delivery = normalize_report_delivery(config.get("delivery"))
    if not delivery["enabled"]:
        return False
    if strategy_runtime_issues(config, execution_kind="scheduled", mode="report"):
        return False
    text = str(report or "")
    is_error = any(marker in text for marker in ["实时行情不可用", "AI 分析失败", "数据源错误", "执行失败"])
    is_empty = any(marker in text for marker in ["当前策略无匹配股票", "当前策略没有匹配股票"])
    if is_error and not delivery["push_on_error"]:
        return False
    if is_empty and not delivery["push_on_empty"]:
        return False
    return True


def sync_hermes_delivery(config: dict, *, runner: Callable | None = None) -> dict:
    job_id = os.getenv("STOCK_AGENT_HERMES_JOB_ID", "").strip()
    hermes_bin = os.getenv("STOCK_AGENT_HERMES_BIN", "hermes").strip()
    if not job_id:
        return {"status": "unavailable", "message": "未配置 Hermes job id"}
    delivery = normalize_report_delivery(config.get("delivery"))
    execute = runner or subprocess.run
    tracking_cron, risk_cron = portfolio_delivery_crons(config)
    auxiliary_jobs = [
        ("tracking", os.getenv("STOCK_AGENT_HERMES_TRACKING_JOB_ID", "").strip(), tracking_cron),
        ("risk", os.getenv("STOCK_AGENT_HERMES_RISK_JOB_ID", "").strip(), risk_cron),
    ]
    runtime_issues = strategy_runtime_issues(config, execution_kind="scheduled", mode="report") if delivery["enabled"] else []
    try:
        if not delivery["enabled"] or runtime_issues:
            paused = []
            for _, target_job_id, _ in [("daily", job_id, ""), *auxiliary_jobs]:
                if not target_job_id:
                    continue
                completed = execute([hermes_bin, "cron", "pause", target_job_id], capture_output=True, text=True, timeout=15)
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout).strip())
                paused.append(target_job_id)
            reason = "；".join(runtime_issues)
            message = f"策略通知任务已暂停：{reason}" if reason else "策略通知任务已暂停"
            return {"status": "paused", "job_id": job_id, "jobs": paused, "message": message}

        target = delivery_target(config)
        schedule = delivery_cron(config)
        edit = execute(
            [hermes_bin, "cron", "edit", job_id, "--schedule", schedule, "--deliver", target],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if edit.returncode != 0:
            raise RuntimeError((edit.stderr or edit.stdout).strip())
        resume = execute([hermes_bin, "cron", "resume", job_id], capture_output=True, text=True, timeout=15)
        if resume.returncode != 0 and "already" not in (resume.stderr or resume.stdout).lower():
            raise RuntimeError((resume.stderr or resume.stdout).strip())
        synced_jobs = [{"kind": "daily", "job_id": job_id, "schedule": schedule}]
        if config.get("portfolio", {}).get("enabled", True):
            for kind, target_job_id, target_schedule in auxiliary_jobs:
                if not target_job_id:
                    continue
                edit = execute(
                    [hermes_bin, "cron", "edit", target_job_id, "--schedule", target_schedule, "--deliver", target],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if edit.returncode != 0:
                    raise RuntimeError((edit.stderr or edit.stdout).strip())
                resume = execute([hermes_bin, "cron", "resume", target_job_id], capture_output=True, text=True, timeout=15)
                if resume.returncode != 0 and "already" not in (resume.stderr or resume.stdout).lower():
                    raise RuntimeError((resume.stderr or resume.stdout).strip())
                synced_jobs.append({"kind": kind, "job_id": target_job_id, "schedule": target_schedule})
        return {
            "status": "synced",
            "job_id": job_id,
            "schedule": schedule,
            "deliver": target,
            "jobs": synced_jobs,
            "message": f"策略通知任务已同步（{len(synced_jobs)} 个）",
        }
    except Exception as exc:
        return {"status": "error", "job_id": job_id, "message": str(exc)[:1000]}


def sync_active_strategy_delivery() -> dict:
    strategy = load_strategy_config()
    if not strategy.get("id"):
        return {"status": "unavailable", "message": "没有活动策略，未修改 Hermes 任务"}
    return sync_hermes_delivery(strategy)


def pause_hermes_delivery() -> dict:
    return sync_hermes_delivery({"delivery": {"enabled": False}})
