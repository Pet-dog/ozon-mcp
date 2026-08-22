"""Internal infrastructure for the PetDog read-only Ozon analytics surface.

Constants, local exceptions, validation/coercion helpers, swagger metadata
evidence helpers, the exact-operation executor, path rendering, the envelope
guard, and the shared posting/return fetch and projection helpers used by
the MCP tools registered in :mod:`ozon_mcp.tools.analytics`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from typing import Any

from mcp.types import ToolAnnotations

from ozon_mcp.errors import OzonError, OzonRateLimitError
from ozon_mcp.readonly import (
    ReadOnlyOperationRefused,
    evaluate_snapshot_status,
    require_allowed_operation,
    sanitize_provider_payload,
)
from ozon_mcp.schema.catalog import Catalog
from ozon_mcp.schema.extractor import Method
from ozon_mcp.transport.performance import PerformanceClient
from ozon_mcp.transport.seller import SellerClient

# --- fixed bounds -----------------------------------------------------------

_MAX_WINDOW_DAYS = 62
_MAX_OFFER_IDS = 1000
_MAX_CAMPAIGN_IDS = 100
_MAX_ROWS = 2000
_MAX_DATE_AGE_DAYS = 14

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PATH_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")
_SAFE_PATH_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_RO = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


class _LocalContractError(Exception):
    """Local refusal/validation error with a fixed safe message only."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def _safe_error(
    category: str,
    message: str,
    *,
    operation_id: str | None = None,
    retryable: bool = False,
    status_code: int | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Closed error envelope: category, fixed local message, retryability."""
    envelope: dict[str, Any] = {
        "source_channel": "ozon",
        "ok": False,
        "error": {
            "category": category,
            "message": message,
            "retryable": retryable,
        },
    }
    if operation_id is not None:
        envelope["error"]["operation_id"] = operation_id
    if status_code is not None:
        envelope["error"]["status_code"] = status_code
    if retry_after_seconds is not None:
        envelope["error"]["retry_after_seconds"] = retry_after_seconds
    return envelope


_RETRYABLE_ERRORS = frozenset(
    {"OzonRateLimitError", "OzonServerError"}
)

_JSON_SUFFIX_OPERATIONS = frozenset(
    {"GetCampaignExpense", "GetCampaignDailyStats"}
)


def _provider_error(e: OzonError) -> dict[str, Any]:
    """Map a transport error to a closed envelope without its payload."""
    name = type(e).__name__
    category = {
        "OzonAuthError": "auth",
        "OzonForbiddenError": "forbidden",
        "OzonValidationError": "provider_validation",
        "OzonNotFoundError": "not_found",
        "OzonConflictError": "conflict",
        "OzonRateLimitError": "rate_limited",
        "OzonServerError": "server",
    }.get(name, "transport")
    retry_after_seconds: int | None = None
    if isinstance(e, OzonRateLimitError):
        raw = getattr(e, "retry_after", None)
        if isinstance(raw, (int, float)) and math.isfinite(raw) and raw >= 0:
            retry_after_seconds = math.ceil(raw)
    status = getattr(e, "status_code", None)
    status_code = status if isinstance(status, int) and not isinstance(status, bool) else None
    return _safe_error(
        category,
        f"provider call failed ({name})",
        operation_id=e.operation_id,
        retryable=name in _RETRYABLE_ERRORS,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )


# --- date / money coercion --------------------------------------------------

def _parse_iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise _LocalContractError(
            "invalid_params", f"{label} must be an ISO YYYY-MM-DD string"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _LocalContractError(
            "invalid_params", f"{label} is not a valid calendar date"
        ) from exc


def _validate_period(date_from: Any, date_to: Any) -> tuple[date, date]:
    start = _parse_iso_date(date_from, "date_from")
    end = _parse_iso_date(date_to, "date_to")
    if start > end:
        raise _LocalContractError(
            "invalid_params", "date_from must not be after date_to"
        )
    if (end - start) > timedelta(days=_MAX_WINDOW_DAYS):
        raise _LocalContractError(
            "invalid_params", f"period exceeds {_MAX_WINDOW_DAYS} days"
        )
    return start, end


def _utc_boundary(day: date, *, end: bool) -> str:
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if end:
        moment = moment.replace(hour=23, minute=59, second=59)
    return moment.isoformat()


def _clamp_int(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _LocalContractError(
            "invalid_params", f"{label} must be an integer"
        )
    if value < low or value > high:
        raise _LocalContractError(
            "invalid_params", f"{label} must be between {low} and {high}"
        )
    return int(value)


def _clean_offer_ids(offer_ids: Sequence[str] | None) -> list[str]:
    if offer_ids is None:
        return []
    if not isinstance(offer_ids, Sequence) or isinstance(offer_ids, (str, bytes)):
        raise _LocalContractError(
            "invalid_params", "offer_ids must be a list of strings"
        )
    out: list[str] = []
    for item in offer_ids:
        if not isinstance(item, str) or not item.strip():
            raise _LocalContractError(
                "invalid_params", "offer_ids entries must be non-empty strings"
            )
        out.append(item.strip()[:128])
        if len(out) > _MAX_OFFER_IDS:
            raise _LocalContractError(
                "invalid_params", f"at most {_MAX_OFFER_IDS} offer_ids allowed"
            )
    return out


def _money(value: Any) -> float | None:
    """Coerce numeric or numeric-string money; invalid stays None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        try:
            parsed = float(text)
        except ValueError:
            return None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return None
        return parsed
    return None


def _int_or(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:200]
    return None


def _walk_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _walk_list(value: Any, cap: int = _MAX_ROWS) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)[:cap]
    return []


def _as_mapping_list(value: Any, cap: int = _MAX_ROWS) -> list[Mapping[str, Any]]:
    return [m for m in _walk_list(value, cap) if isinstance(m, Mapping)]


# --- snapshot evidence ------------------------------------------------------

def _load_swagger_meta() -> Any:
    try:
        text = (files("ozon_mcp.data") / "swagger_meta.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError):
        return None
    import json

    try:
        return json.loads(text)
    except ValueError:
        return None


def _normalize_meta(meta: Any) -> Mapping[str, Any] | None:
    """Normalize current swagger_meta.json shape for evaluate_snapshot_status."""
    if not isinstance(meta, Mapping):
        return None
    observed = meta.get("refreshed_at") or meta.get("observed_at")
    if not isinstance(observed, str):
        return None
    hashes: dict[str, str] = {}
    legacy = meta.get("content_sha256")
    if isinstance(legacy, Mapping):
        for name, digest in legacy.items():
            if isinstance(digest, str):
                hashes[str(name)] = digest
    for name in ("seller", "performance"):
        section = meta.get(name)
        if isinstance(section, Mapping):
            digest = section.get("sha256")
            if isinstance(digest, str):
                hashes[name] = digest
    if not hashes:
        return None
    return {"observed_at": observed, "content_sha256": hashes}


_SWAGGER_FILES: Mapping[str, str] = {
    "seller": "seller_swagger.json",
    "performance": "perf_swagger.json",
}


def _actual_content_hashes() -> dict[str, str]:
    """SHA-256 over the exact bundled spec bytes, via importlib.resources.

    Uses a fixed internal mapping; never reflects caller-supplied names.
    Missing files and read errors raise _LocalContractError spec_unavailable.
    """
    import hashlib

    digests: dict[str, str] = {}
    for api, filename in _SWAGGER_FILES.items():
        try:
            payload = (files("ozon_mcp.data") / filename).read_bytes()
        except (FileNotFoundError, OSError, UnicodeError) as exc:
            raise _LocalContractError(
                "spec_unavailable",
                f"bundled spec file for {api!r} is missing or unreadable",
            ) from exc
        digests[api] = hashlib.sha256(payload).hexdigest()
    return digests


def _snapshot_evidence() -> dict[str, Any]:
    """Content-bound snapshot evidence for the fixed allowlisted contracts.

    Computes SHA-256 over the actual bundled spec files and compares each
    digest to the normalized metadata. A missing file, read error, absent
    expected hash, malformed metadata, or digest mismatch raises
    _LocalContractError (spec_unavailable) before any catalog lookup or
    transport request construction. Age beyond the freshness bound is
    stale but non-blocking for these exact internal contracts, and the
    returned envelope discloses the truthful stale state.
    """
    meta = _normalize_meta(_load_swagger_meta())
    if meta is None:
        raise _LocalContractError(
            "spec_unavailable",
            "snapshot metadata is missing or malformed; network execution blocked",
        )
    status = evaluate_snapshot_status(
        meta, datetime.now(tz=UTC), max_age_days=_MAX_DATE_AGE_DAYS
    )
    if status.blocking:
        raise _LocalContractError(
            "spec_unavailable", "snapshot metadata failed validation"
        )
    expected = meta["content_sha256"]
    actual = _actual_content_hashes()
    for api in _SWAGGER_FILES:
        wanted = expected.get(api)
        if not isinstance(wanted, str) or not wanted.strip():
            raise _LocalContractError(
                "spec_unavailable",
                f"expected content hash for {api!r} is absent from metadata",
            )
        if actual[api] != wanted.strip().lower():
            raise _LocalContractError(
                "spec_unavailable",
                f"bundled spec content for {api!r} does not match metadata hash",
            )
    return {
        "spec_snapshot": {
            "refreshed_at": status.observed_at.isoformat()
            if status.observed_at
            else None,
            "seller_sha": expected.get("seller"),
            "performance_sha": expected.get("performance"),
            "verified_hashes": actual,
            "hashes_verified": True,
            "stale": status.stale,
            "blocking": False,
            "reason": status.reason,
        }
    }


# --- exact-operation executor ------------------------------------------------

class _Executor:
    """Internal-only network executor for fixed allowlisted operations.

    Callers never supply operation ids, paths, or HTTP verbs; every call is
    made by internal code with a literal allowlisted operation id.
    """

    def __init__(
        self,
        catalog: Catalog,
        seller_client: SellerClient | None,
        performance_client: PerformanceClient | None,
    ) -> None:
        self._catalog = catalog
        self._seller = seller_client
        self._performance = performance_client

    async def seller(
        self, operation_id: str, json_body: Mapping[str, Any]
    ) -> dict[str, Any]:
        method = self._resolve(operation_id, "seller")
        assert self._seller is not None
        return await self._run(
            method,
            self._seller.request(
                method.method,
                method.path,
                json_body=dict(json_body),
                operation_id=method.operation_id,
                section=method.section,
                with_retry=False,
            ),
        )

    async def performance(
        self,
        operation_id: str,
        query_params: Mapping[str, Any] | None = None,
        path_params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        method = self._resolve(operation_id, "performance")
        if self._performance is None:  # pragma: no cover - guarded in _resolve
            raise _LocalContractError(
                "unavailable", "performance client is not configured"
            )
        path = _render_path(method.path, path_params or {})
        if operation_id in _JSON_SUFFIX_OPERATIONS:
            # Official OpenAPI evidence: the JSON flavour of these two
            # synchronous statistics endpoints is the same path + "/json".
            # Appended here from a literal allowlist only -- callers can
            # never supply a path or suffix.
            path += "/json"
        return await self._run(
            method,
            self._performance.request(
                method.method,
                path,
                query_params=dict(query_params or {}),
                operation_id=method.operation_id,
                section=method.section,
                with_retry=False,
            ),
        )

    def _resolve(self, operation_id: str, api: str) -> Method:
        require_allowed_operation(operation_id)
        _snapshot_evidence()
        method = self._catalog.get_by_operation_id(operation_id)
        if method is None or method.api != api:
            raise _LocalContractError(
                "not_allowed", "operation is not resolvable for this surface"
            )
        if api == "seller" and self._seller is None:
            raise _LocalContractError(
                "unavailable", "seller client is not configured"
            )
        if api == "performance" and self._performance is None:
            raise _LocalContractError(
                "unavailable", "performance client is not configured"
            )
        return method

    async def _run(self, method: Method, coro: Any) -> dict[str, Any]:
        try:
            result = await coro
        except OzonError as exc:
            raise _ContractFailureError(_provider_error(exc)) from exc
        except ReadOnlyOperationRefused as exc:  # pragma: no cover - pre-checked
            raise _ContractFailureError(
                _safe_error("not_allowed", str(exc), operation_id=method.operation_id)
            ) from exc
        except Exception as exc:  # closed envelope, no details
            raise _ContractFailureError(
                _safe_error(
                    "internal",
                    "internal execution failure without provider detail",
                    operation_id=method.operation_id,
                )
            ) from exc
        if not isinstance(result, Mapping):
            return {}
        cleaned = sanitize_provider_payload(dict(result))
        cleaned.pop("_raw", None)
        return cleaned if isinstance(cleaned, dict) else {}


class _ContractFailureError(Exception):
    """Carries a pre-built closed error envelope out of the executor."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        super().__init__("contract failure")
        self.envelope = envelope


def _render_path(path: str, path_params: Mapping[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = path_params.get(key)
        if value is None or not _SAFE_PATH_VALUE_RE.match(value):
            raise _LocalContractError(
                "invalid_params", f"path parameter {key!r} is missing or unsafe"
            )
        return value

    return _PATH_TOKEN_RE.sub(_sub, path)


def _tool_guard(envelope_out: dict[str, Any]) -> dict[str, Any] | dict[str, Any]:
    """Attach mandatory evidence fields to a successful tool response.

    Returns a closed error envelope when snapshot metadata is missing or
    malformed, so no tool ever raises out of the evidence step.
    """
    try:
        evidence = _snapshot_evidence()
    except _LocalContractError as exc:
        return _safe_error(exc.category, exc.message)
    envelope_out.setdefault("source_channel", "ozon")
    envelope_out.setdefault("timezone", "UTC")
    for key, value in evidence.items():
        envelope_out.setdefault(key, value)
    return envelope_out


# --- shared sales/returns fetch (also reused by sku_performance) -------------

_SALES_FIELDS = ("posting_number", "offer_id", "sku", "status", "currency")


def _iter_posting_products(posting: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("products", "product_items"):
        items = posting.get(key)
        if isinstance(items, list):
            return [p for p in items if isinstance(p, Mapping)][:_MAX_ROWS]
    return []


def _product_price(product: Mapping[str, Any]) -> float | None:
    """Parse a product price from a scalar or a nested pricing mapping."""
    scalar = _money(product.get("price"))
    if scalar is not None:
        return scalar
    for key in ("price", "pricing_product", "prices"):
        node = product.get(key)
        if isinstance(node, Mapping):
            amount = _money(node.get("amount", node.get("price")))
            if amount is not None:
                return amount
    return None


def _posting_amount(product: Mapping[str, Any], quantity: int) -> float:
    amount = _product_price(product)
    if amount is None:
        return 0.0
    return amount * max(quantity, 1)


async def _fetch_sales_rows(
    executor: _Executor, start: date, end: date, limit: int
) -> dict[str, Any]:
    """Fetch one bounded page of FBO + FBS postings; return raw-ish safe rows."""
    time_filter = {
        "since": _utc_boundary(start, end=False),
        "to": _utc_boundary(end, end=True),
    }
    fbo_body: dict[str, Any] = {
        "filter": dict(time_filter),
        "limit": limit,
        "offset": 0,
        "dir": "ASC",
        "with": {
            "analytics_data": False,
            "financial_data": True,
            "legal_info": False,
        },
    }
    fbs_body: dict[str, Any] = {
        "filter": dict(time_filter),
        "limit": limit,
        "offset": 0,
        "dir": "ASC",
        "with": {
            "analytics_data": False,
            "financial_data": True,
            "legal_info": False,
            "barcodes": False,
            "translit": False,
        },
    }
    fbo = await executor.seller("PostingAPI_GetFboPostingList", fbo_body)
    fbs = await executor.seller("PostingAPI_GetFbsPostingListV3", fbs_body)
    fbo_result = fbo.get("result")
    fbo_rows = (
        [dict(row) for row in _as_mapping_list(fbo_result)]
        if isinstance(fbo_result, list)
        else []
    )
    for row in fbo_rows:
        row["_fulfillment"] = "fbo"
    fbs_result = fbs.get("result")
    fbs_rows = (
        [dict(row) for row in _as_mapping_list(fbs_result.get("postings"))]
        if isinstance(fbs_result, Mapping)
        else []
    )
    for row in fbs_rows:
        row["_fulfillment"] = "fbs"
    rows = fbo_rows + fbs_rows
    truncated = False
    if isinstance(fbs_result, Mapping) and fbs_result.get("has_next") is True:
        truncated = True
    if len(fbo_rows) >= limit or len(fbs_rows) >= limit:
        truncated = True
    if len(rows) > _MAX_ROWS * 2:
        truncated = True
    return {
        "rows": rows[: _MAX_ROWS * 2],
        "fetched_fbo": len(fbo_rows),
        "fetched_fbs": len(fbs_rows),
        "row_count": len(rows),
        "truncated": truncated,
    }


def _project_sales_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    aggregate: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    currencies: set[str] = set()
    invalid_money = 0
    posting_count: dict[tuple[str, str, str, str], set[str]] = {}
    for posting in rows:
        status = _text(posting.get("status")) or "unknown"
        fulfillment = (
            posting.get("_fulfillment")
            if isinstance(posting.get("_fulfillment"), str)
            else "unknown"
        ) or "unknown"
        posting_keys: set[tuple[str, str, str, str]] = set()
        for product in _iter_posting_products(posting):
            offer_id = _text(product.get("offer_id")) or ""
            sku = str(_int_or(product.get("sku"), 0)) if product.get("sku") else ""
            quantity = max(_int_or(product.get("quantity"), 1), 1)
            currency = _text(product.get("currency_code"))
            if currency:
                currencies.add(currency)
            amount = _posting_amount(product, quantity)
            if _product_price(product) is None:
                invalid_money += 1
            key = (offer_id, sku, fulfillment, status)
            bucket = aggregate.setdefault(
                key,
                {
                    "offer_id": offer_id,
                    "sku": sku if sku else None,
                    "fulfillment": fulfillment,
                    "status": status,
                    "units": 0,
                    "gross_revenue": 0.0,
                    "posting_count": 0,
                },
            )
            bucket["units"] += quantity
            bucket["gross_revenue"] = round(bucket["gross_revenue"] + amount, 2)
            posting_keys.add(key)
        identity = _text(posting.get("posting_number")) or ""
        for key in posting_keys:
            posting_count.setdefault(key, set()).add(identity)
    for key, bucket in aggregate.items():
        bucket["posting_count"] = len(posting_count.get(key, ()))
    summary = {
        "currency_codes": sorted(currencies),
        "invalid_money_rows": invalid_money,
    }
    return sorted(
        aggregate.values(),
        key=lambda b: (b["offer_id"], str(b["sku"]), b["fulfillment"], b["status"]),
    )[:_MAX_ROWS], summary


async def _fetch_returns_rows(
    executor: _Executor, start: date, end: date, limit: int
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "filter": {
            "logistic_return_date": {
                "time_from": _utc_boundary(start, end=False),
                "time_to": _utc_boundary(end, end=True),
            }
        },
        "limit": limit,
        "last_id": 0,
    }
    data = await executor.seller("returnsList", body)
    rows = _as_mapping_list(data.get("returns"))
    truncated = data.get("has_next") is True or len(rows) >= limit
    return {"rows": rows, "row_count": len(rows), "truncated": truncated}


def _returns_unit_price(product: Mapping[str, Any]) -> float | None:
    """Unit price from product.price (scalar or mapping with .price)."""
    node = product.get("price")
    if isinstance(node, Mapping):
        return _money(node.get("price"))
    return _money(node)


def _project_returns_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    aggregate: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for item in rows:
        product = _walk_mapping(item.get("product"))
        offer_id = _text(product.get("offer_id")) or ""
        sku = str(_int_or(product.get("sku"), 0)) if product.get("sku") else ""
        quantity = max(_int_or(product.get("quantity"), 1), 1)
        visual_status_node = _walk_mapping(_walk_mapping(item.get("visual")).get("status"))
        visual = (
            _text(visual_status_node.get("sys_name"))
            or _text(visual_status_node.get("display_name"))
            or "unknown"
        )
        schema = _text(item.get("schema")) or "unknown"
        reason = _text(item.get("return_reason_name")) or "unknown"
        key = (offer_id, sku, visual, schema, reason)
        bucket = aggregate.setdefault(
            key,
            {
                "offer_id": offer_id,
                "sku": sku if sku else None,
                "visual_status": visual,
                "schema": schema,
                "return_reason_name": reason,
                "units": 0,
                "returned_amount": 0.0,
            },
        )
        bucket["units"] += quantity
        price = _returns_unit_price(product)
        if price is not None:
            bucket["returned_amount"] = round(
                bucket["returned_amount"] + price * quantity, 2
            )
    return sorted(
        aggregate.values(),
        key=lambda b: (b["offer_id"], str(b["sku"]), b["return_reason_name"]),
    )[:_MAX_ROWS]
