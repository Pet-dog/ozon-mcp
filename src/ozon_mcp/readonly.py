"""Read-only Ozon boundary primitives for the PetDog analytics surface.

This module centralizes the safety envelope shared by all network-capable
tools: an exact allowlist of read/query operation ids, a pre-flight gate
that refuses anything else before an HTTP request can be constructed, a
recursive sanitizer that strips personal and credential material from
provider payloads, an HMAC-based anonymized join key for reconciliation,
a closed reconciliation projection, and snapshot freshness evaluation.

Standard library only; no logging (stdout is reserved for MCP framing).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "READ_ONLY_OPERATION_IDS",
    "ReadOnlyOperationRefused",
    "SnapshotStatus",
    "build_reconciliation_projection",
    "evaluate_snapshot_status",
    "make_join_key",
    "require_allowed_operation",
    "sanitize_provider_payload",
]

# Exact allowlist of read/query operation ids. Membership is decided by
# this literal only -- never inferred from HTTP method or upstream safety
# classification.
READ_ONLY_OPERATION_IDS: set[str] = {
    # Seller API
    "SellerAPI_SellerInfo",
    "ProductAPI_GetProductList",
    "ProductAPI_GetProductInfoStocks",
    "ProductAPI_GetProductInfoPrices",
    "PostingAPI_GetFboPostingList",
    "PostingAPI_GetFbsPostingListV3",
    "returnsList",
    "FinanceAPI_FinanceTransactionListV3",
    # Performance API
    "ListCampaigns",
    "ListReports",
    "GetCampaignExpense",
    "GetCampaignDailyStats",
}

_REFUSAL_MESSAGE = "operation is not on the read-only allowlist"


class ReadOnlyOperationRefused(Exception):  # noqa: N818
    """Raised when an operation id is not an allowlisted read-only call.

    Carries only the rejected operation id and a fixed safe message; it
    never carries provider payloads or credentials.
    """

    def __init__(self, operation_id: str) -> None:
        super().__init__(_REFUSAL_MESSAGE)
        self.operation_id = operation_id

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{_REFUSAL_MESSAGE}: {self.operation_id!r}"


def require_allowed_operation(operation_id: str) -> None:
    """Refuse blank, unknown, write, and destructive operation ids.

    Must be called before any caller constructs an HTTP request.
    """
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ReadOnlyOperationRefused(repr(operation_id))
    if operation_id not in READ_ONLY_OPERATION_IDS:
        raise ReadOnlyOperationRefused(operation_id)


# --- payload sanitization -------------------------------------------------

# Substrings matched against normalized (lowercase, alphanumeric-only)
# mapping keys; a hit drops the whole entry.
_SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "name",
    "phone",
    "email",
    "address",
    "recipient",
    "customer",
    "buyer",
    "comment",
    "authorization",
    "apikey",
    "token",
    "secret",
    "password",
    "passport",
    "payment",
    "card",
)

# Conservative structural bounds.
MAX_RECURSION_DEPTH: int = 8
MAX_LIST_LENGTH: int = 100
MAX_MAPPING_KEYS: int = 100
MAX_STRING_LENGTH: int = 2_000

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _is_sensitive_key(key: str) -> bool:
    normalized = _NON_ALNUM_RE.sub("", key.lower())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def sanitize_provider_payload(value: Any) -> Any:
    """Return a safe recursive copy of a provider payload.

    Drops mapping entries whose normalized key indicates personal data or
    credentials, bounds depth, list length, mapping key count, and string
    length, and replaces unsupported objects with their type name only
    (never their repr content).
    """
    return _sanitize(value, 0)


def _sanitize(value: Any, depth: int) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    if isinstance(value, bytes):
        return value[:MAX_STRING_LENGTH].decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        if depth >= MAX_RECURSION_DEPTH:
            return {}
        safe: dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_MAPPING_KEYS]:
            key_str = key if isinstance(key, str) else str(key)
            if _is_sensitive_key(key_str):
                continue
            safe[key_str[:MAX_STRING_LENGTH]] = _sanitize(item, depth + 1)
        return safe
    if isinstance(value, Sequence):
        if depth >= MAX_RECURSION_DEPTH:
            return []
        return [_sanitize(item, depth + 1) for item in list(value)[:MAX_LIST_LENGTH]]
    # Unsupported object: type name only, no repr content.
    return type(value).__name__


# --- anonymized join key ---------------------------------------------------

_MAX_IDENTIFIER_LENGTH: int = 256


def _normalize_identifier(raw_order_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw_order_id).strip()
    if not normalized:
        raise ValueError("order identifier is empty after normalization")
    if len(normalized) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError("order identifier exceeds 256 characters")
    return normalized


def make_join_key(raw_order_id: str, secret: str) -> str:
    """Return a deterministic lowercase HMAC-SHA256 hex join key.

    The identifier is normalized by Unicode NFKC and whitespace-stripped;
    the raw identifier never appears in the key.
    """
    identifier = _normalize_identifier(raw_order_id)
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    digest = hmac.new(
        secret.encode("utf-8"), identifier.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest.lower()


# --- reconciliation projection ---------------------------------------------

_PROJECTION_FIELDS: tuple[str, ...] = (
    "status",
    "created_at",
    "shipped_at",
    "offer_id",
    "sku",
    "currency",
)


def _row_identity(row: Mapping[str, Any]) -> str | None:
    for field in ("posting_number", "order_id"):
        candidate = row.get(field)
        if candidate is None or isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)):
            candidate = str(candidate)
        if isinstance(candidate, str) and candidate.strip():
            return unicodedata.normalize("NFKC", candidate.strip())
    return None


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def build_reconciliation_projection(
    rows: Sequence[Mapping[str, Any]], secret: str
) -> dict[str, Any]:
    """Build a closed, anonymized reconciliation projection of Ozon rows.

    Identity is taken from non-empty ``posting_number`` then ``order_id``,
    deduplicated by join key (first duplicate kept); neither identity is
    ever output. Only the projection fields plus ``join_key`` and numeric
    ``amount`` are copied -- never arbitrary keys.
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    skipped_count = 0
    for row in rows:
        identity = _row_identity(row)
        if identity is None:
            skipped_count += 1
            continue
        join_key = make_join_key(identity, secret)
        if join_key in seen:
            duplicate_count += 1
            continue
        seen.add(join_key)
        item: dict[str, Any] = {"join_key": join_key, "amount": None}
        amount = _numeric(row.get("amount"))
        if amount is not None:
            item["amount"] = amount
        for field in _PROJECTION_FIELDS:
            candidate = row.get(field)
            if candidate is None or candidate == "":
                continue
            if isinstance(candidate, str):
                item[field] = candidate[:MAX_STRING_LENGTH]
            elif isinstance(candidate, bool):
                continue
            elif isinstance(candidate, (int, float)):
                item[field] = candidate
            else:
                item[field] = _sanitize(candidate, 0)
        items.append(item)
    return {
        "source_channel": "ozon",
        "items": items,
        "item_count": len(items),
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
    }


# --- snapshot freshness -----------------------------------------------------

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class SnapshotStatus:
    """Outcome of evaluating snapshot metadata freshness and validity."""

    stale: bool
    blocking: bool
    age_days: float
    observed_at: datetime | None
    reason: str


def _parse_observed_at(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("observed_at must be a non-empty string")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _blocking_status(reason: str, now: datetime) -> SnapshotStatus:
    return SnapshotStatus(
        stale=False,
        blocking=True,
        age_days=-1.0,
        observed_at=None,
        reason=reason,
    )


def evaluate_snapshot_status(
    meta: Any, now: datetime, max_age_days: int | float = 14
) -> SnapshotStatus:
    """Evaluate snapshot metadata against a freshness bound.

    Metadata must be a mapping with a parseable UTC-aware ``observed_at``
    and a non-empty ``content_sha256`` mapping whose values are exactly
    64 hex characters. Missing, malformed, or future timestamps, or bad
    hashes, are blocking. Age over the bound is stale but not blocking.
    Naive ``now`` is normalized to UTC; ``max_age_days`` must be positive.
    """
    if not isinstance(max_age_days, (int, float)) or isinstance(max_age_days, bool):
        raise ValueError("max_age_days must be a positive number")
    if max_age_days <= 0:
        raise ValueError("max_age_days must be a positive number")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if not isinstance(meta, Mapping):
        return _blocking_status("snapshot metadata is not a mapping", now)
    try:
        observed = _parse_observed_at(meta.get("observed_at"))
    except (ValueError, TypeError, OverflowError):
        return _blocking_status("observed_at is missing or malformed", now)
    if observed > now:
        return _blocking_status("observed_at is in the future", now)
    hashes = meta.get("content_sha256")
    if not isinstance(hashes, Mapping) or not hashes:
        return _blocking_status("content_sha256 is missing or empty", now)
    for name, digest in hashes.items():
        if not isinstance(digest, str) or not _HEX64_RE.match(digest):
            return _blocking_status(
                f"content hash for {name!r} is malformed", now
            )
    age_seconds = (now - observed).total_seconds()
    age_days = age_seconds / 86_400
    if age_days > float(max_age_days):
        return SnapshotStatus(
            stale=True,
            blocking=False,
            age_days=age_days,
            observed_at=observed,
            reason="snapshot is older than the freshness bound",
        )
    return SnapshotStatus(
        stale=False,
        blocking=False,
        age_days=age_days,
        observed_at=observed,
        reason="snapshot is fresh",
    )
