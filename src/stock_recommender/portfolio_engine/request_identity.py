"""Stable identities for fully captured portfolio workflow requests."""

from __future__ import annotations

from .canonical import canonical_digest
from .contracts import PlanRequest, ProcessRequest


def request_fingerprint(request: PlanRequest | ProcessRequest) -> str:
    if type(request) not in {PlanRequest, ProcessRequest}:
        raise TypeError("request must be PlanRequest or ProcessRequest")
    return canonical_digest(request)


__all__ = ("request_fingerprint",)
