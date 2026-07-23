from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Protocol


class HistoricalDataError(RuntimeError):
    pass


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _records(value: object) -> list[dict]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        result = converter(orient="records")
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, dict)]
    return []


def _iso_day(value: object) -> str | None:
    parsed = _date_value(value)
    return parsed.isoformat() if parsed else None


def _normalize_market_rows(rows: object, *, name: str | None = None) -> list[dict]:
    normalized = []
    for row in _records(rows):
        day = _iso_day(row.get("date", row.get("日期")))
        close = _number(row.get("close", row.get("收盘")))
        if not day or close <= 0:
            continue
        open_price = _number(row.get("open", row.get("开盘")), close) or close
        normalized.append(
            {
                "date": day,
                "open": open_price,
                "close": close,
                "high": _number(row.get("high", row.get("最高")), close) or close,
                "low": _number(row.get("low", row.get("最低")), close) or close,
                "volume": _number(row.get("volume", row.get("成交量"))),
                "turnover": _number(row.get("turnover", row.get("成交额"))),
                **({"name": name} if name else {}),
            }
        )
    return sorted(normalized, key=lambda item: item["date"])


@dataclass(frozen=True)
class UniverseSnapshot:
    as_of: date
    symbols: tuple[str, ...]
    names: dict[str, str]


class HistoricalDataProvider(Protocol):
    universe_name: str
    universe_symbol: str
    benchmark_name: str
    benchmark_symbol: str

    def universe_snapshots(self) -> list[UniverseSnapshot]: ...

    def security_history(self, symbol: str, start: date, end: date, *, name: str | None = None) -> list[dict]: ...

    def benchmark_history(self, start: date, end: date) -> list[dict]: ...

    def intraday_execution(self, symbol: str, start: date, end: date) -> dict[str, dict]: ...


class AkshareCniProvider:
    """Public-data adapter; all network access stays behind this replaceable boundary."""

    def __init__(
        self,
        *,
        universe_symbol: str = "399284",
        universe_name: str = "国证 AI 50",
        benchmark_symbol: str | None = None,
        benchmark_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import akshare as client  # type: ignore[no-redef]
            except ImportError as exc:
                raise HistoricalDataError("构建公开历史数据集需要安装 analysis 依赖：pip install -e '.[analysis]'") from exc
        self.client = client
        self.universe_symbol = str(universe_symbol)
        self.universe_name = str(universe_name)
        self.benchmark_symbol = str(benchmark_symbol or universe_symbol)
        self.benchmark_name = str(benchmark_name or universe_name)

    def universe_snapshots(self) -> list[UniverseSnapshot]:
        rows = _records(self.client.index_detail_hist_cni(symbol=self.universe_symbol))
        grouped: dict[date, dict[str, str]] = {}
        for row in rows:
            as_of = _date_value(row.get("日期", row.get("date")))
            symbol = str(row.get("样本代码", row.get("symbol")) or "").strip().zfill(6)
            name = str(row.get("样本简称", row.get("name")) or symbol).strip()
            if (
                as_of is None
                or not symbol.startswith(("0", "3", "6"))
                or "ST" in name.upper()
                or "退" in name
            ):
                continue
            grouped.setdefault(as_of, {})[symbol] = name
        if not grouped:
            raise HistoricalDataError(f"{self.universe_name}（{self.universe_symbol}）没有可用的历史成分快照")
        return [
            UniverseSnapshot(as_of=as_of, symbols=tuple(sorted(names)), names=dict(sorted(names.items())))
            for as_of, names in sorted(grouped.items())
        ]

    def security_history(self, symbol: str, start: date, end: date, *, name: str | None = None) -> list[dict]:
        rows = self.client.stock_zh_a_hist(
            symbol=str(symbol),
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
        return _normalize_market_rows(rows, name=name)

    def benchmark_history(self, start: date, end: date) -> list[dict]:
        rows = self.client.index_zh_a_hist(
            symbol=self.benchmark_symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        return _normalize_market_rows(rows, name=self.benchmark_name)

    def intraday_execution(self, symbol: str, start: date, end: date) -> dict[str, dict]:
        rows = _records(
            self.client.stock_zh_a_hist_min_em(
                symbol=str(symbol),
                start_date=f"{start.isoformat()} 09:30:00",
                end_date=f"{end.isoformat()} 15:00:00",
                period="5",
                adjust="",
            )
        )
        bars: dict[str, dict[str, dict]] = {}
        for row in rows:
            raw_time = str(row.get("时间", row.get("time")) or "").strip()
            try:
                bar_time = datetime.fromisoformat(raw_time)
            except ValueError:
                continue
            clock = bar_time.strftime("%H:%M:%S")
            if clock not in {"09:35:00", "15:00:00"}:
                continue
            bars.setdefault(bar_time.date().isoformat(), {})[clock] = row
        result = {}
        for day, day_bars in bars.items():
            entry = day_bars.get("09:35:00")
            exit_bar = day_bars.get("15:00:00")
            if not entry or not exit_bar:
                continue
            entry_price = _number(entry.get("收盘", entry.get("close")))
            exit_price = _number(exit_bar.get("收盘", exit_bar.get("close")))
            if entry_price <= 0 or exit_price <= 0:
                continue
            result[day] = {
                "entry_price": entry_price,
                "exit_price": exit_price,
                "open_volume": _number(entry.get("成交量", entry.get("volume"))),
                "close_volume": _number(exit_bar.get("成交量", exit_bar.get("volume"))),
            }
        return result


def _limit_price(previous_close: float, rate: float) -> float:
    value = Decimal(str(previous_close)) * (Decimal("1") + Decimal(str(rate)))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _daily_limit_rate(symbol: str, name: str) -> float:
    if "ST" in name.upper():
        return 0.05
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "9")):
        return 0.30
    return 0.10


def _enrich_execution_rows(rows: list[dict], intraday: dict[str, dict], *, symbol: str, name: str) -> list[dict]:
    enriched = []
    previous_close = 0.0
    for row in rows:
        item = dict(row)
        execution = intraday.get(str(item.get("date")))
        if execution and previous_close > 0:
            rate = _daily_limit_rate(symbol, name)
            item.update(execution)
            item["upper_limit"] = _limit_price(previous_close, rate)
            item["lower_limit"] = _limit_price(previous_close, -rate)
        previous_close = _number(item.get("close")) or previous_close
        enriched.append(item)
    return enriched


def audit_historical_dataset(dataset: dict) -> dict:
    panel = dataset.get("panel") if isinstance(dataset.get("panel"), dict) else {}
    benchmark = dataset.get("benchmark") if isinstance(dataset.get("benchmark"), list) else []
    evaluation = dataset.get("evaluation_period") if isinstance(dataset.get("evaluation_period"), dict) else {}
    evaluation_start = _date_value(evaluation.get("start"))
    evaluation_end = _date_value(evaluation.get("end"))
    benchmark_dates = {_date_value(row.get("date")) for row in benchmark if isinstance(row, dict)}
    benchmark_dates.discard(None)
    history_rows = sum(len(rows) for rows in panel.values() if isinstance(rows, list))
    symbols_with_warmup = 0
    symbols_covering_period = 0
    for rows in panel.values():
        dates = sorted(
            day
            for row in rows if isinstance(row, dict)
            if (day := _date_value(row.get("date"))) is not None
        )
        if evaluation_start and sum(1 for day in dates if day < evaluation_start) >= 61:
            symbols_with_warmup += 1
        if evaluation_start and evaluation_end and any(evaluation_start <= day <= evaluation_end for day in dates):
            symbols_covering_period += 1
    evaluation_sessions = sum(
        1
        for day in benchmark_dates
        if evaluation_start and evaluation_end and evaluation_start <= day <= evaluation_end
    )
    issues = []
    if len(panel) < 3:
        issues.append("可用证券少于 3 只")
    if symbols_with_warmup < len(panel):
        issues.append("部分证券不足 61 个交易日预热数据")
    if evaluation_sessions <= 0:
        issues.append("评估区间没有基准交易日")
    if symbols_covering_period < len(panel):
        issues.append("部分证券在评估区间没有行情")
    return {
        "passed": not issues,
        "symbols": len(panel),
        "history_rows": history_rows,
        "symbols_with_61_day_warmup": symbols_with_warmup,
        "symbols_covering_period": symbols_covering_period,
        "benchmark_rows": len(benchmark),
        "evaluation_sessions": evaluation_sessions,
        "issues": issues,
    }


def build_historical_dataset(
    provider: HistoricalDataProvider,
    *,
    evaluation_start: date,
    evaluation_end: date,
    warmup_calendar_days: int = 400,
    workers: int = 4,
    include_intraday_execution: bool = False,
) -> dict:
    if evaluation_end < evaluation_start:
        raise HistoricalDataError("评估结束日期不能早于开始日期")
    snapshots = provider.universe_snapshots()
    usable = [snapshot for snapshot in snapshots if snapshot.as_of < evaluation_end]
    if not usable:
        raise HistoricalDataError("评估区间之前没有历史成分快照")
    earliest_safe_day = min(snapshot.as_of for snapshot in usable) + timedelta(days=1)
    effective_start = max(evaluation_start, earliest_safe_day)
    if effective_start > evaluation_end:
        raise HistoricalDataError("历史成分快照太晚，评估区间没有无未来信息的交易日")
    relevant_snapshots = [snapshot for snapshot in usable if snapshot.as_of < evaluation_end]
    symbols = sorted({symbol for snapshot in relevant_snapshots for symbol in snapshot.symbols})
    names = {symbol: name for snapshot in relevant_snapshots for symbol, name in snapshot.names.items()}
    history_start = effective_start - timedelta(days=max(120, int(warmup_calendar_days)))
    panel: dict[str, list[dict]] = {}
    errors: list[str] = []

    def fetch_symbol(symbol: str) -> list[dict]:
        name = names.get(symbol) or symbol
        rows = provider.security_history(symbol, history_start, evaluation_end, name=name)
        if not include_intraday_execution:
            return rows
        intraday = provider.intraday_execution(symbol, effective_start, evaluation_end)
        return _enrich_execution_rows(rows, intraday, symbol=symbol, name=name)

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(symbols)))) as executor:
        futures = {
            executor.submit(fetch_symbol, symbol): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue
            if rows:
                panel[symbol] = rows
            else:
                errors.append(f"{symbol}: 无行情")
    if len(panel) < 3:
        raise HistoricalDataError("历史行情可用证券不足 3 只")
    benchmark = provider.benchmark_history(history_start, evaluation_end)
    if not benchmark:
        raise HistoricalDataError("独立基准没有历史行情")
    universe_by_date = {
        snapshot.as_of.isoformat(): [symbol for symbol in snapshot.symbols if symbol in panel]
        for snapshot in relevant_snapshots
    }
    required_execution_fields = ("entry_price", "exit_price", "open_volume", "close_volume", "upper_limit", "lower_limit")
    execution_rows = [
        row
        for rows in panel.values()
        for row in rows
        if effective_start <= (_date_value(row.get("date")) or date.min) <= evaluation_end
    ]
    execution_complete = bool(execution_rows) and all(
        all(row.get(field) is not None and _number(row.get(field)) > 0 for field in required_execution_fields)
        for row in execution_rows
    )
    warnings = []
    if not execution_complete:
        warnings.append("09:35/15:00 分时价量或逐日涨跌停状态不完整，执行数据门禁保持关闭。")
    warnings.append("数据集使用不复权成交价，尚未向组合现金和持仓数量计入分红送转，公司行动门禁保持关闭。")
    if effective_start > evaluation_start:
        warnings.append(
            f"请求从 {evaluation_start.isoformat()} 开始，但首个可靠成分快照为 "
            f"{(effective_start - timedelta(days=1)).isoformat()}，评估起点已安全截到 {effective_start.isoformat()}。"
        )
    warnings.extend(errors[:20])
    dataset = {
        "panel": dict(sorted(panel.items())),
        "benchmark": benchmark,
        "universe_by_date": universe_by_date,
        "evaluation_period": {"start": effective_start.isoformat(), "end": evaluation_end.isoformat()},
        "metadata": {
            "source": "akshare_public_cnindex_eastmoney",
            "dataset_contract_version": 2,
            "universe_mode": "point_in_time_index_snapshot",
            "universe_symbol": provider.universe_symbol,
            "universe_name": provider.universe_name,
            "benchmark_mode": "official_index",
            "benchmark_symbol": provider.benchmark_symbol,
            "benchmark_name": provider.benchmark_name,
            "requested_evaluation_start": evaluation_start.isoformat(),
            "effective_evaluation_start": effective_start.isoformat(),
            "evaluation_end": evaluation_end.isoformat(),
            "history_start": history_start.isoformat(),
            "point_in_time_complete": True,
            "benchmark_complete": True,
            "strategy_parity_complete": True,
            "execution_data_complete": execution_complete,
            "execution_price_mode": "intraday_0935_1500" if execution_complete else "daily_open_close_proxy",
            "corporate_actions_complete": False,
            "price_adjustment": "unadjusted",
            "parameter_trials": 1,
            "warnings": warnings,
        },
    }
    dataset["metadata"]["quality_audit"] = audit_historical_dataset(dataset)
    return dataset


def write_historical_dataset(dataset: dict, path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target
