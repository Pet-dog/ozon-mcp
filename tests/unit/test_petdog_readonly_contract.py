"""Red-first contract tests for the PetDog read-only Ozon boundary.

Defines the target contract for workstream `ozon-readonly-sxo-20260822`
before implementation. All data is synthetic; no network access and no
ambient Ozon credentials are used (Ozon-related env vars are scrubbed).

The planned module `ozon_mcp.readonly` is imported inside tests so the
whole file collects and reports per-test red failures on the unchanged
implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ozon_mcp.config import Config
from ozon_mcp.server import create_server

SECRET = "unit-test-secret"

PETDOG_TOOLS = {
    "ozon_seller_status",
    "ozon_sales_analytics",
    "ozon_finance_analytics",
    "ozon_returns_analytics",
    "ozon_inventory_analytics",
    "ozon_price_promotion_analytics",
    "ozon_advertising_analytics",
    "ozon_sku_performance",
    "ozon_catalog_mapping",
    "ozon_retailcrm_projection",
}


def _readonly():
    from ozon_mcp import readonly

    return readonly


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith("OZON_") or key.upper().startswith("PERF_"):
            monkeypatch.delenv(key, raising=False)


async def _tool_map() -> dict[str, Any]:
    mcp = create_server(Config(client_id="test-client", api_key="test-key", performance_client_id="test-perf", performance_client_secret="test-secret"))
    tools = await mcp.list_tools()
    return {tool.name: tool for tool in tools}


def _tool_schema(tool: Any) -> dict[str, Any]:
    for attr in ("parameters", "inputSchema", "input_schema"):
        schema = getattr(tool, attr, None)
        if schema is not None:
            return schema
    return {}


# 1 -----------------------------------------------------------------------
async def test_petdog_network_tools_registered() -> None:
    tools = await _tool_map()
    missing = PETDOG_TOOLS - set(tools)
    assert not missing, f"missing PetDog tools: {sorted(missing)}"


# 2 -----------------------------------------------------------------------
async def test_no_generic_write_surface() -> None:
    tools = await _tool_map()
    assert "ozon_call_method" not in tools
    assert "ozon_fetch_all" not in tools
    for name, tool in tools.items():
        blob = json.dumps(_tool_schema(tool))
        assert "confirm_write" not in blob, name
        assert "i_understand_this_modifies_data" not in blob, name


def test_legacy_generic_execution_module_physically_absent() -> None:
    """The legacy generic network execution surface must be deleted, not
    merely unregistered — find_spec must not resolve it. The subscription
    module must remain present as a pure offline tier utility (no network
    path, no tool registration)."""
    assert importlib.util.find_spec("ozon_mcp.tools.execution") is None
    assert importlib.util.find_spec("ozon_mcp.tools.subscription") is not None


# 3 -----------------------------------------------------------------------
async def test_network_tools_read_only_annotations() -> None:
    tools = await _tool_map()
    for name in PETDOG_TOOLS:
        annotations = getattr(tools[name], "annotations", None)
        assert annotations is not None, name
        assert getattr(annotations, "readOnlyHint", None) is True, name
        assert getattr(annotations, "destructiveHint", None) is False, name


# 4 -----------------------------------------------------------------------
def test_read_only_operation_ids_exact_set() -> None:
    ro = _readonly()
    ids = ro.READ_ONLY_OPERATION_IDS
    assert isinstance(ids, set)
    assert ids == {
        "SellerAPI_SellerInfo",
        "ProductAPI_GetProductList",
        "ProductAPI_GetProductInfoStocks",
        "ProductAPI_GetProductInfoPrices",
        "PostingAPI_GetFboPostingList",
        "PostingAPI_GetFbsPostingListV3",
        "returnsList",
        "FinanceAPI_FinanceTransactionListV3",
        "ListCampaigns",
        "ListReports",
        "GetCampaignExpense",
        "GetCampaignDailyStats",
    }


# 5 -----------------------------------------------------------------------
def test_require_allowed_operation_gates() -> None:
    ro = _readonly()
    assert ro.require_allowed_operation("SellerAPI_SellerInfo") is None
    with pytest.raises(ro.ReadOnlyOperationRefused) as excinfo:
        ro.require_allowed_operation("ProductAPI_ProductsStocksV2")
    assert "ozon_mcp.readonly" in type(excinfo.value).__module__
    with pytest.raises(ro.ReadOnlyOperationRefused):
        ro.require_allowed_operation("Totally_Unknown_Operation")


# 6 -----------------------------------------------------------------------
def test_sanitize_provider_payload_strips_pii_and_secrets() -> None:
    ro = _readonly()
    payload = {
        "offer_id": "PET-100",
        "sku": 1_000_001,
        "revenue": 1234.56,
        "status": "delivered",
        "shipped_at": "2026-08-01T00:00:00Z",
        "customer_name": "Ivan Petrov",
        "Phone": "+7 900 000 00 00",
        "buyer_email": "buyer@example.test",
        "delivery_address": "Moscow, Red Square 1",
        "AddRess": "SPb, Nevsky 2",
        "customer_comment": "leave at door",
        "Authorization": "Bearer xyz",
        "api_key": "supersecret",
        "Client-Token": "tok",
        "nested": {"recipient_name": "Anna", "post": {"phone": "+79001112233"}},
    }
    clean = ro.sanitize_provider_payload(payload)
    blob = json.dumps(clean).lower()
    for leaked in (
        "ivan petrov",
        "+7 900",
        "buyer@example.test",
        "red square",
        "nevsky",
        "leave at door",
        "supersecret",
        "bearer xyz",
        "anna",
        "+79001112233",
    ):
        assert leaked not in blob
    for secret_key in ("name", "phone", "email", "address", "comment", "authorization", "api_key", "token"):
        assert secret_key not in blob
    assert clean["offer_id"] == "PET-100"
    assert clean["sku"] == 1_000_001
    assert clean["revenue"] == 1234.56
    assert clean["status"] == "delivered"
    assert clean["shipped_at"] == "2026-08-01T00:00:00Z"


# 7 -----------------------------------------------------------------------
def test_make_join_key_properties() -> None:
    ro = _readonly()
    raw = "00012345678-0001"
    a1 = ro.make_join_key(raw, SECRET)
    a2 = ro.make_join_key(raw, SECRET)
    assert isinstance(a1, str) and a1
    assert a1 == a2
    assert ro.make_join_key("00012345678-0002", SECRET) != a1
    assert ro.make_join_key(raw, "other-secret") != a1
    assert raw not in a1 and raw.replace("-", "") not in a1
    assert raw not in repr(a1)


# 8 -----------------------------------------------------------------------
def _order_rows() -> list[dict[str, Any]]:
    return [
        {
            "posting_number": "00012345678-0001",
            "order_id": 900_001,
            "offer_id": "PET-100",
            "sku": 1_000_001,
            "amount": 1500.00,
            "status": "delivered",
            "shipped_at": "2026-08-01T00:00:00Z",
            "customer_name": "Ivan Petrov",
            "customer_phone": "+7 900 000 00 00",
        },
        {
            "posting_number": "00012345678-0001",  # duplicate feed row
            "order_id": 900_001,
            "offer_id": "PET-100",
            "sku": 1_000_001,
            "amount": 1500.00,
            "status": "delivered",
            "shipped_at": "2026-08-01T00:00:00Z",
        },
        {
            "posting_number": "00012345678-0002",
            "order_id": 900_002,
            "offer_id": "PET-200",
            "sku": 1_000_002,
            "amount": 990.50,
            "status": "shipped",
            "shipped_at": "2026-08-05T00:00:00Z",
        },
    ]


def test_build_reconciliation_projection() -> None:
    ro = _readonly()
    result = ro.build_reconciliation_projection(_order_rows(), SECRET)
    assert result["source_channel"] == "ozon"
    keys = [item["join_key"] for item in result["items"]]
    assert len(keys) == len(set(keys)) == 2
    assert result["duplicate_count"] == 1
    amounts = {item["amount"] for item in result["items"]}
    assert amounts == {1500.00, 990.50}
    blob = json.dumps(result)
    for leaked in ("00012345678-0001", "00012345678-0002", "900_001", "Ivan Petrov", "+7 900"):
        assert leaked not in blob
    assert "customer" not in blob.lower()


# 9 -----------------------------------------------------------------------
def _snapshot_meta(observed_at: str) -> dict[str, Any]:
    return {
        "observed_at": observed_at,
        "content_sha256": {
            "seller_api.json": "a" * 64,
            "performance_api.json": "b" * 64,
        },
    }


def test_evaluate_snapshot_status_freshness() -> None:
    ro = _readonly()
    now = datetime(2026, 8, 22, tzinfo=UTC)
    stale = ro.evaluate_snapshot_status(
        _snapshot_meta("2026-04-16T00:00:00Z"), now, max_age_days=14
    )
    assert stale.stale is True
    assert stale.blocking is not True
    fresh_at = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = ro.evaluate_snapshot_status(_snapshot_meta(fresh_at), now, max_age_days=14)
    assert fresh.stale is False
    assert fresh.blocking is not True
    for malformed in ({}, {"observed_at": "not-a-date"}, {"observed_at": "2026-04-16T00:00:00Z"}):
        blocked = ro.evaluate_snapshot_status(malformed, now, max_age_days=14)
        assert blocked.blocking is True


def test_server_startup_keeps_stdout_clean(capsys: pytest.CaptureFixture[str]) -> None:
    create_server(Config())
    captured = capsys.readouterr()
    assert captured.out == ""
