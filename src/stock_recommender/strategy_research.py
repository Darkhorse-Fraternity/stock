from __future__ import annotations

from copy import deepcopy

from .backtest import run_walk_forward_backtest


FACTOR_PROFILES = {
    "equal": {
        "momentum20": 1.0,
        "momentum60": 1.0,
        "trend": 1.0,
        "volume_ratio": 1.0,
        "inverse_volatility": 1.0,
        "drawdown": 1.0,
    },
    "momentum": {
        "momentum20": 2.0,
        "momentum60": 1.5,
        "trend": 1.5,
        "volume_ratio": 0.5,
        "inverse_volatility": 1.0,
        "drawdown": 0.5,
    },
    "trend_risk": {
        "momentum20": 1.0,
        "momentum60": 1.0,
        "trend": 2.0,
        "volume_ratio": 0.25,
        "inverse_volatility": 2.0,
        "drawdown": 1.0,
    },
    "short_term": {
        "momentum20": 2.0,
        "momentum60": 0.5,
        "trend": 1.0,
        "volume_ratio": 0.25,
        "inverse_volatility": 1.0,
        "drawdown": 0.5,
    },
    "low_volatility": {
        "momentum20": 0.5,
        "momentum60": 0.5,
        "trend": 1.0,
        "volume_ratio": 0.25,
        "inverse_volatility": 3.0,
        "drawdown": 1.0,
    },
}


PORTFOLIO_PROFILES = {
    "focused_fast": {"top_n": 3, "signal_invalid_days": 2},
    "focused_stable": {"top_n": 3, "signal_invalid_days": 5},
    "balanced": {"top_n": 5, "signal_invalid_days": 5},
    "full_book": {"top_n": 10, "signal_invalid_days": 10},
}


def default_research_variants() -> list[dict]:
    return [
        {
            "id": f"{factor_id}__{portfolio_id}",
            "factor_profile": factor_id,
            "portfolio_profile": portfolio_id,
            "factor_weights": deepcopy(factor_weights),
            **deepcopy(portfolio_values),
        }
        for factor_id, factor_weights in FACTOR_PROFILES.items()
        for portfolio_id, portfolio_values in PORTFOLIO_PROFILES.items()
    ]


def apply_research_variant(strategy: dict, variant: dict) -> dict:
    candidate = deepcopy(strategy)
    candidate.setdefault("signal", {})["factor_weights"] = deepcopy(variant["factor_weights"])
    candidate.setdefault("validation", {})["top_n"] = int(variant["top_n"])
    candidate.setdefault("portfolio", {})["signal_invalid_days"] = int(variant["signal_invalid_days"])
    return candidate


def compare_strategy_variants(dataset: dict, strategy: dict, variants: list[dict] | None = None) -> dict:
    candidates = variants or default_research_variants()
    research_dataset = dict(dataset)
    research_dataset["metadata"] = {
        **deepcopy(dataset.get("metadata") or {}),
        "parameter_trials": len(candidates),
    }
    results = []
    for variant in candidates:
        candidate = apply_research_variant(strategy, variant)
        result = run_walk_forward_backtest(research_dataset, candidate)
        metrics = result["metrics"]
        results.append(
            {
                "id": variant["id"],
                "factor_profile": variant["factor_profile"],
                "portfolio_profile": variant["portfolio_profile"],
                "factor_weights": deepcopy(variant["factor_weights"]),
                "top_n": int(variant["top_n"]),
                "signal_invalid_days": int(variant["signal_invalid_days"]),
                "cumulative_return_pct": metrics["cumulative_return_pct"],
                "benchmark_cumulative_return_pct": metrics["benchmark_cumulative_return_pct"],
                "mean_excess_return_pct": metrics["mean_excess_return_pct"],
                "maximum_drawdown_pct": metrics["maximum_drawdown_pct"],
                "closed_trades": metrics["closed_trades"],
                "maximum_positions_observed": metrics["maximum_positions_observed"],
                "dsr_probability": metrics["dsr_probability"],
                "approval_gate_passed": result["approval_gate"]["passed"],
            }
        )
    results.sort(
        key=lambda item: (
            item["cumulative_return_pct"],
            item["mean_excess_return_pct"],
            item["maximum_drawdown_pct"],
        ),
        reverse=True,
    )
    return {
        "objective": "maximize_cumulative_return_with_drawdown_as_tiebreaker",
        "parameter_trials": len(candidates),
        "evaluation_period": deepcopy(dataset.get("evaluation_period")),
        "results": results,
    }


def rank_robust_candidates(studies: list[dict]) -> list[dict]:
    if not studies:
        return []
    common_ids = set.intersection(
        *[
            {str(item.get("id")) for item in study.get("results", []) if item.get("id")}
            for study in studies
        ]
    )
    aggregated = []
    for variant_id in sorted(common_ids):
        period_results = []
        ranks = []
        for study in studies:
            ordered = study.get("results", [])
            item = next(result for result in ordered if result.get("id") == variant_id)
            period_results.append(item)
            ranks.append(next(index for index, result in enumerate(ordered, 1) if result.get("id") == variant_id))
        template = period_results[0]
        aggregated.append(
            {
                "id": variant_id,
                "factor_profile": template["factor_profile"],
                "portfolio_profile": template["portfolio_profile"],
                "factor_weights": deepcopy(template["factor_weights"]),
                "top_n": template["top_n"],
                "signal_invalid_days": template["signal_invalid_days"],
                "mean_rank": sum(ranks) / len(ranks),
                "worst_rank": max(ranks),
                "positive_periods": sum(1 for item in period_results if item["cumulative_return_pct"] > 0),
                "worst_return_pct": min(item["cumulative_return_pct"] for item in period_results),
                "worst_drawdown_pct": min(item["maximum_drawdown_pct"] for item in period_results),
                "periods": len(period_results),
            }
        )
    return sorted(
        aggregated,
        key=lambda item: (
            item["mean_rank"],
            item["worst_rank"],
            -item["positive_periods"],
            -item["worst_return_pct"],
        ),
    )
