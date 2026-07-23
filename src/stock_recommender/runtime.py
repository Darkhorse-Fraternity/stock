from __future__ import annotations


RUNNABLE_STRATEGY_STAGES = {"paper", "live"}
PREVIEW_STRATEGY_STAGES = {"draft", "backtesting", "paper", "live", "paused"}


class StrategyRuntimeError(RuntimeError):
    pass


def strategy_runtime_issues(
    strategy: dict,
    *,
    execution_kind: str = "scheduled",
    mode: str = "report",
) -> list[str]:
    kind = str(execution_kind or "scheduled").strip().lower()
    stage = str((strategy.get("lifecycle") or {}).get("stage") or "draft").strip().lower()
    issues: list[str] = []
    if kind == "preview":
        if stage not in PREVIEW_STRATEGY_STAGES:
            issues.append(f"策略阶段 {stage} 不允许预览执行")
        return issues
    if kind != "scheduled":
        issues.append(f"不支持的执行类型: {execution_kind}")
        return issues
    if stage not in RUNNABLE_STRATEGY_STAGES:
        issues.append(f"策略阶段 {stage} 不允许定时运行，仅 paper/live 可运行")
    if stage == "live" and not bool((strategy.get("validation") or {}).get("approval_gate", {}).get("passed")):
        issues.append("live 策略的样本外审批门禁未通过")
    if mode in {"track", "risk"} and not bool((strategy.get("portfolio") or {}).get("enabled", True)):
        issues.append("组合未启用，不能运行持仓跟踪或风控")
    return issues


def assert_strategy_runnable(
    strategy: dict,
    *,
    execution_kind: str = "scheduled",
    mode: str = "report",
) -> None:
    issues = strategy_runtime_issues(strategy, execution_kind=execution_kind, mode=mode)
    if issues:
        raise StrategyRuntimeError("；".join(issues))
