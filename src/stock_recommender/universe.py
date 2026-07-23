from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping


_LIST_SEPARATOR = re.compile(r"[,，;；\n]+")
_SECTOR_SEPARATOR = re.compile(r"[,，;；|\n]+")


def normalize_stock_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(?:SH|SZ)?(\d{6})(?:\.(?:SH|SZ))?", text)
    if not match:
        raise ValueError(f"无效的 A 股代码：{value!r}")
    return match.group(1)


def normalize_sector_filters(values: str | Iterable[object] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values: Iterable[object] = _SECTOR_SEPARATOR.split(values)
    else:
        raw_values = values

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        label = str(value or "").strip()
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        normalized.append(label)
    return normalized


def _normalize_watchlist_entry(entry: object) -> dict:
    if isinstance(entry, str):
        parts = [part.strip() for part in entry.split(":", 2)]
        item: dict[str, object] = {"symbol": parts[0]}
        if len(parts) >= 2 and parts[1]:
            item["name"] = parts[1]
        if len(parts) >= 3 and parts[2]:
            item["sector"] = parts[2]
    elif isinstance(entry, Mapping):
        item = dict(entry)
    else:
        raise ValueError(f"自选股配置项必须是字符串或对象：{entry!r}")

    symbol = normalize_stock_symbol(item.get("symbol") or item.get("code"))
    name = str(item.get("name") or "").strip()
    sector = str(item.get("sector") or "").strip()
    sectors = normalize_sector_filters(item.get("sectors") or item.get("tags"))
    if sector and sector.casefold() not in {value.casefold() for value in sectors}:
        sectors.insert(0, sector)
    if not sector and sectors:
        sector = sectors[0]

    normalized = {"symbol": symbol}
    if name:
        normalized["name"] = name
    if sector:
        normalized["sector"] = sector
    if sectors:
        normalized["sectors"] = sectors
    return normalized


def normalize_watchlist(entries: str | Iterable[object] | None) -> list[dict]:
    if entries is None:
        return []
    if isinstance(entries, str):
        return parse_watchlist(entries)

    normalized: list[dict] = []
    by_symbol: dict[str, dict] = {}
    for entry in entries:
        item = _normalize_watchlist_entry(entry)
        symbol = item["symbol"]
        existing = by_symbol.get(symbol)
        if existing is None:
            by_symbol[symbol] = item
            normalized.append(item)
            continue

        if not existing.get("name") and item.get("name"):
            existing["name"] = item["name"]
        merged_sectors = normalize_sector_filters(
            [
                *(existing.get("sectors") or []),
                existing.get("sector"),
                *(item.get("sectors") or []),
                item.get("sector"),
            ]
        )
        if merged_sectors:
            existing["sector"] = existing.get("sector") or merged_sectors[0]
            existing["sectors"] = merged_sectors
    return normalized


def parse_watchlist(value: str | None) -> list[dict]:
    text = str(value or "").strip()
    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"STOCK_AGENT_WATCHLIST 不是有效 JSON：{exc}") from exc
        if isinstance(decoded, Mapping):
            entries = []
            for symbol, metadata in decoded.items():
                if isinstance(metadata, Mapping):
                    entries.append({**metadata, "symbol": symbol})
                elif metadata:
                    entries.append({"symbol": symbol, "sector": metadata})
                else:
                    entries.append({"symbol": symbol})
            return normalize_watchlist(entries)
        if not isinstance(decoded, list):
            raise ValueError("STOCK_AGENT_WATCHLIST JSON 必须是数组或股票代码映射")
        return normalize_watchlist(decoded)

    return normalize_watchlist(item for item in _LIST_SEPARATOR.split(text) if item.strip())


def row_sector_tags(row: Mapping[str, object]) -> list[str]:
    values: list[object] = [row.get("sector")]
    for key in ("sectors", "tags"):
        configured = row.get(key)
        if isinstance(configured, str):
            values.extend(_SECTOR_SEPARATOR.split(configured))
        elif isinstance(configured, Iterable):
            values.extend(configured)
    return normalize_sector_filters(values)


def row_matches_sector(row: Mapping[str, object], sector_filters: str | Iterable[object] | None) -> bool:
    filters = normalize_sector_filters(sector_filters)
    if not filters:
        return True
    wanted = {value.casefold() for value in filters}
    return bool(wanted.intersection(value.casefold() for value in row_sector_tags(row)))


def filter_rows_by_sector(rows: Iterable[dict], sector_filters: str | Iterable[object] | None) -> list[dict]:
    return [row for row in rows if row_matches_sector(row, sector_filters)]


def constrain_to_watchlist(rows: Iterable[dict], watchlist: str | Iterable[object]) -> list[dict]:
    entries = normalize_watchlist(watchlist)
    metadata = {item["symbol"]: item for item in entries}
    constrained: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            symbol = normalize_stock_symbol(row.get("symbol"))
        except ValueError:
            continue
        configured = metadata.get(symbol)
        if configured is None or symbol in seen:
            continue
        enriched = {**row, "symbol": symbol}
        if configured.get("name"):
            enriched["name"] = configured["name"]
        if configured.get("sector"):
            enriched["sector"] = configured["sector"]
        elif not enriched.get("sector"):
            enriched["sector"] = "未分类"
        if configured.get("sectors"):
            enriched["sectors"] = configured["sectors"]
        seen.add(symbol)
        constrained.append(enriched)
    return constrained
