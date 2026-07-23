from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from .historical_data import AkshareCniProvider, build_historical_dataset, write_historical_dataset


def _day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须为 YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    today = date.today()
    result = argparse.ArgumentParser(description="构建隔离的历史时点回测数据集")
    result.add_argument("--output", required=True, help="输出 JSON 路径；建议使用 /private/tmp")
    result.add_argument("--start", type=_day, default=today - timedelta(days=30), help="请求评估起点")
    result.add_argument("--end", type=_day, default=today, help="评估终点")
    result.add_argument("--universe-symbol", default="399284", help="国证历史成分快照指数代码")
    result.add_argument("--universe-name", default="国证 AI 50", help="股票池名称")
    result.add_argument("--benchmark-symbol", default=None, help="独立基准代码；默认与股票池指数一致")
    result.add_argument("--benchmark-name", default=None, help="独立基准名称")
    result.add_argument("--warmup-calendar-days", type=int, default=400, help="因子预热自然日数")
    result.add_argument("--workers", type=int, default=4, help="行情下载并发数")
    result.add_argument("--include-intraday-execution", action="store_true", help="补齐 09:35/15:00 分时价量和涨跌停数据")
    return result


def main() -> None:
    arguments = parser().parse_args()
    provider = AkshareCniProvider(
        universe_symbol=arguments.universe_symbol,
        universe_name=arguments.universe_name,
        benchmark_symbol=arguments.benchmark_symbol,
        benchmark_name=arguments.benchmark_name,
    )
    dataset = build_historical_dataset(
        provider,
        evaluation_start=arguments.start,
        evaluation_end=arguments.end,
        warmup_calendar_days=arguments.warmup_calendar_days,
        workers=arguments.workers,
        include_intraday_execution=arguments.include_intraday_execution,
    )
    target = write_historical_dataset(dataset, arguments.output)
    print(
        json.dumps(
            {
                "path": str(target),
                "evaluation_period": dataset["evaluation_period"],
                "quality_audit": dataset["metadata"]["quality_audit"],
                "warnings": dataset["metadata"]["warnings"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
