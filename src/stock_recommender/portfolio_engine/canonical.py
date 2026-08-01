"""Canonical, lossless JSON graphs for request identity and ledger replay."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping


class CanonicalGraphError(ValueError):
    """Raised when a canonical graph is malformed or contains an unknown type."""


def canonical_graph(value: Any) -> Any:
    """Encode supported immutable values without dropping type information."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            "kind": "dataclass",
            "name": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [field.name, canonical_graph(getattr(value, field.name))]
                for field in fields(value)
            ],
        }
    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if type(key) is not str:
                raise CanonicalGraphError("canonical mapping keys must be strings")
            items.append([key, canonical_graph(item)])
        items.sort(key=lambda pair: pair[0])
        return {"kind": "mapping", "items": items}
    if isinstance(value, (tuple, list)):
        return {
            "kind": "sequence",
            "items": [canonical_graph(item) for item in value],
        }
    if isinstance(value, (set, frozenset)):
        encoded = [canonical_graph(item) for item in value]
        encoded.sort(key=_encoded_sort_key)
        return {"kind": "set", "items": encoded}
    if isinstance(value, Enum):
        return {
            "kind": "enum",
            "name": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": canonical_graph(value.value),
        }
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"kind": "bytes", "value": value.hex()}
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            raise CanonicalGraphError("canonical number must be finite")
        return {"kind": "scalar", "value": value}
    raise CanonicalGraphError(f"unsupported canonical value: {type(value).__name__}")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        canonical_graph(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decode_canonical_graph(
    value: Any,
    *,
    dataclass_types: Mapping[str, type[Any]],
    enum_types: Mapping[str, type[Enum]],
) -> Any:
    """Decode one strict canonical graph using an explicit type allowlist."""

    if not isinstance(value, Mapping) or type(value.get("kind")) is not str:
        raise CanonicalGraphError("canonical node must be a tagged object")
    kind = value["kind"]
    if kind == "scalar":
        _require_exact_keys(value, {"kind", "value"})
        scalar = value["value"]
        if scalar is not None and type(scalar) not in {bool, int, float, str}:
            raise CanonicalGraphError("canonical scalar has unsupported type")
        if type(scalar) is float and not math.isfinite(scalar):
            raise CanonicalGraphError("canonical number must be finite")
        return scalar
    if kind in {"datetime", "date", "bytes"}:
        _require_exact_keys(value, {"kind", "value"})
        raw = value["value"]
        if type(raw) is not str:
            raise CanonicalGraphError(f"canonical {kind} value must be a string")
        try:
            if kind == "datetime":
                decoded = datetime.fromisoformat(raw)
                if decoded.tzinfo is None or decoded.utcoffset() is None:
                    raise ValueError("datetime must be timezone-aware")
                return decoded
            if kind == "date":
                return date.fromisoformat(raw)
            return bytes.fromhex(raw)
        except ValueError as exc:
            raise CanonicalGraphError(f"invalid canonical {kind}") from exc
    if kind in {"sequence", "set"}:
        _require_exact_keys(value, {"kind", "items"})
        items = value["items"]
        if type(items) is not list:
            raise CanonicalGraphError(f"canonical {kind} items must be a list")
        decoded = tuple(
            decode_canonical_graph(
                item,
                dataclass_types=dataclass_types,
                enum_types=enum_types,
            )
            for item in items
        )
        return frozenset(decoded) if kind == "set" else decoded
    if kind == "mapping":
        _require_exact_keys(value, {"kind", "items"})
        items = value["items"]
        if type(items) is not list:
            raise CanonicalGraphError("canonical mapping items must be a list")
        result: dict[str, Any] = {}
        previous_key: str | None = None
        for pair in items:
            if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
                raise CanonicalGraphError("canonical mapping pair is malformed")
            key = pair[0]
            if key in result or (previous_key is not None and key < previous_key):
                raise CanonicalGraphError(
                    "canonical mapping keys must be unique and sorted"
                )
            result[key] = decode_canonical_graph(
                pair[1],
                dataclass_types=dataclass_types,
                enum_types=enum_types,
            )
            previous_key = key
        return result
    if kind == "enum":
        _require_exact_keys(value, {"kind", "name", "value"})
        name = value["name"]
        if type(name) is not str or name not in enum_types:
            raise CanonicalGraphError("canonical enum type is not allowed")
        raw = decode_canonical_graph(
            value["value"],
            dataclass_types=dataclass_types,
            enum_types=enum_types,
        )
        try:
            return enum_types[name](raw)
        except (TypeError, ValueError) as exc:
            raise CanonicalGraphError("canonical enum value is invalid") from exc
    if kind == "dataclass":
        _require_exact_keys(value, {"kind", "name", "fields"})
        name = value["name"]
        raw_fields = value["fields"]
        if type(name) is not str or name not in dataclass_types:
            raise CanonicalGraphError("canonical dataclass type is not allowed")
        if type(raw_fields) is not list:
            raise CanonicalGraphError("canonical dataclass fields must be a list")
        decoded_fields: dict[str, Any] = {}
        for pair in raw_fields:
            if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
                raise CanonicalGraphError("canonical dataclass field is malformed")
            field_name = pair[0]
            if field_name in decoded_fields:
                raise CanonicalGraphError("canonical dataclass field is duplicated")
            decoded_fields[field_name] = decode_canonical_graph(
                pair[1],
                dataclass_types=dataclass_types,
                enum_types=enum_types,
            )
        expected_fields = {field.name for field in fields(dataclass_types[name])}
        if set(decoded_fields) != expected_fields:
            raise CanonicalGraphError("canonical dataclass fields are not exact")
        try:
            return dataclass_types[name](**decoded_fields)
        except (TypeError, ValueError) as exc:
            raise CanonicalGraphError("canonical dataclass value is invalid") from exc
    raise CanonicalGraphError(f"unsupported canonical node kind: {kind}")


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise CanonicalGraphError("canonical node fields are not exact")


def _encoded_sort_key(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


__all__ = (
    "CanonicalGraphError",
    "canonical_digest",
    "canonical_graph",
    "decode_canonical_graph",
)
