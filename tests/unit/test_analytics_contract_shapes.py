"""Contract-shape tests for ozon_mcp.tools.analytics.

Freezes the request/response projection contract for the analytics tools
using a recording executor and a stubbed evidence snapshot. No live data.
"""

import asyncio
import hashlib
import json
from datetime import date
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from ozon_mcp.tools import analytics
from ozon_mcp.tools import analytics_core as core

SECRET = "unit-test-secret"


def _parse(call_result: Any) -> Any:
    return json.loads(call_result[0][0].text)


class RecordingExecutor:
    """Doubles the HTTP executor: records requests, pops canned responses."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.requests: list[tuple[str, str, dict]] = []

    async def seller(self, operation_id: str, json_body: dict) -> Any:
        self.requests.append(("seller", operation_id, json_body))
        return self.responses["seller"].pop(0)

    async def performance(
        self,
        operation_id: str,
        query_params: dict | None = None,
        path_params: dict | None = None,
    ) -> Any:
        self.requests.append(("performance", operation_id, query_params or {}))
        return self.responses["performance"].pop(0)


def _mcp(monkeypatch: pytest.MonkeyPatch, recorder: RecordingExecutor, secret: str = SECRET) -> FastMCP:
    snapshot = {
        "meta": {
            "observed_at": "2026-04-16T21:47:53Z",
            "content_sha256": {"seller": "a" * 64, "performance": "b" * 64},
        }
    }
    monkeypatch.setattr(analytics, "_Executor", lambda *_, **__: recorder)
    monkeypatch.setattr(core, "_snapshot_evidence", lambda *_, **__: snapshot)
    mcp = FastMCP("test")
    analytics.register(mcp, None, None, None, hmac_secret=secret)
    return mcp


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    return _parse(await mcp.call_tool(name, kwargs))


# ---------------------------------------------------------------- test 1


class _DummyCatalog:
    def __init__(self) -> None:
        self.calls = 0

    def get_by_operation_id(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        return None


def test_meta_and_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = core._normalize_meta(
        {
            "refreshed_at": "2026-04-16T21:47:53Z",
            "seller": {"sha256": "a" * 64},
            "performance": {"sha256": "b" * 64},
        }
    )
    assert meta["observed_at"] == "2026-04-16T21:47:53Z"
    assert meta["content_sha256"]["seller"] == "a" * 64
    assert meta["content_sha256"]["performance"] == "b" * 64

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise core._LocalContractError("spec_unavailable", "bad")

    monkeypatch.setattr(core, "_snapshot_evidence", boom)
    monkeypatch.setattr(core, "require_allowed_operation", lambda *a, **k: None)
    catalog = _DummyCatalog()
    executor = core._Executor(catalog, object(), None)
    before = catalog.calls
    with pytest.raises(core._LocalContractError):
        executor._resolve("SellerAPI_SellerInfo", "seller")
    assert catalog.calls == before


# ---------------------------------------------------------------- tests 2-3


def _posting(prefix: str) -> dict:
    return {
        "posting_number": f"{prefix}-0001",
        "status": "delivered",
        "products": [
            {
                "offer_id": "OFF-1",
                "sku": 123,
                "quantity": 2,
                "price": "10.5",
                "currency_code": "RUB",
            }
        ],
    }


def test_sales_helpers_project_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingExecutor(
        {
            "seller": [
                {"result": [_posting("FBO")]},
                {"result": {"postings": [], "has_next": False}},
            ]
        }
    )
    _mcp(monkeypatch, recorder)

    fetched = asyncio.run(
        core._fetch_sales_rows(recorder, date(2026, 8, 1), date(2026, 8, 2), 10)
    )
    assert "rows" in fetched
    rows = fetched["rows"]
    assert all(r["_fulfillment"] in {"fbo", "fbs"} for r in rows)
    assert any(r["_fulfillment"] == "fbo" for r in rows)

    fbo_body, fbs_body = recorder.requests[0][2], recorder.requests[1][2]
    for body in (fbo_body, fbs_body):
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert body["dir"] == "ASC"
        assert set(body["filter"].keys()) == {"since", "to"}
    assert fbo_body["filter"] == fbs_body["filter"]
    assert fbo_body["with"] == {
        "analytics_data": False,
        "financial_data": True,
        "legal_info": False,
    }
    assert fbs_body["with"] == {
        "analytics_data": False,
        "financial_data": True,
        "legal_info": False,
        "barcodes": False,
        "translit": False,
    }

    projected, summary = core._project_sales_rows(fetched["rows"])
    assert projected[0]["units"] == 2
    assert projected[0]["gross_revenue"] == 21
    assert projected[0]["posting_count"] == 1
    assert projected[0]["fulfillment"] == "fbo"
    assert summary["currency_codes"] == ["RUB"]


def test_returns_helper_projects_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "returns": [
            {
                "product": {
                    "offer_id": "OFF-1",
                    "sku": 123,
                    "quantity": 2,
                    "price": {"price": "5"},
                },
                "visual": {"status": {"sys_name": "returned_to_seller"}},
                "schema": "standard",
                "return_reason_name": "did_not_fit",
            }
        ],
        "has_next": True,
    }
    recorder = RecordingExecutor({"seller": [response]})
    _mcp(monkeypatch, recorder)

    start, end = date(2026, 8, 1), date(2026, 8, 2)
    fetched = asyncio.run(core._fetch_returns_rows(recorder, start, end, 10))

    body = recorder.requests[0][2]
    assert set(body.keys()) == {"filter", "limit", "last_id"}
    assert body["limit"] == 10
    assert body["last_id"] == 0
    assert set(body["filter"].keys()) == {"logistic_return_date"}
    window = body["filter"]["logistic_return_date"]
    assert set(window.keys()) == {"time_from", "time_to"}

    assert window["time_from"].startswith("2026-08-01T00:00:00")
    assert window["time_to"].startswith("2026-08-02T23:59:59")
    assert window["time_from"].endswith(("Z", "+00:00"))
    assert window["time_to"].endswith(("Z", "+00:00"))

    projected = core._project_returns_rows(fetched["rows"])
    assert projected[0]["visual_status"] == "returned_to_seller"
    assert projected[0]["schema"] == "standard"
    assert projected[0]["return_reason_name"] == "did_not_fit"


# ---------------------------------------------------------------- test 4


def test_registered_inventory_and_price_strip_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    stock_with_noise = {
        "shipment_type": "FBO",
        "sku": 123,
        "present": 5,
        "reserved": 1,
        # injected PII noise that must never reach the client
        "customer_name": "Ivan Petrov",
        "address": "Secret Lane 1",
    }
    inventory = {
        "items": [{"offer_id": "OFF-1", "product_id": 1, "stocks": [stock_with_noise]}],
        "total": 1,
        "cursor": "",
    }
    price = {
        "items": [
            {
                "offer_id": "OFF-1",
                "product_id": 1,
                "price": {
                    "currency_code": "RUB",
                    "price": 10.5,
                    "old_price": 12,
                    "min_price": 9,
                    "marketing_seller_price": 9.5,
                    "net_price": 8,
                    "vat": 0.1,
                },
                "marketing_actions": {
                    "actions": [
                        {
                            "title": "promo",
                            "date_from": "2026-08-01",
                            "date_to": "2026-08-02",
                            "value": 2,
                        }
                    ],
                    "ozon_actions_exist": True,
                },
            }
        ],
        "total": 1,
        "cursor": "",
    }
    recorder = RecordingExecutor({"seller": [inventory, price]})
    mcp = _mcp(monkeypatch, recorder)

    inv_out = asyncio.run(
        _call(mcp, "ozon_inventory_analytics", offer_ids=["OFF-1"], limit=100)
    )
    price_out = asyncio.run(
        _call(mcp, "ozon_price_promotion_analytics", offer_ids=["OFF-1"], limit=100)
    )

    inv_row = inv_out["rows"][0]
    assert inv_row["offer_id"] == "OFF-1"
    assert inv_row["skus"] == [123]
    assert inv_row["shipment_types"] == ["FBO"]
    price_row = price_out["rows"][0]
    assert price_row["offer_id"] == "OFF-1"
    assert price_row["currency"] == "RUB"
    assert price_row["price"] == 10.5
    assert price_row["vat"] == 0.1
    assert price_row["actions"][0]["title"] == "promo"

    blob = json.dumps(inv_out) + json.dumps(price_out)
    assert "customer_name" not in blob
    assert "Ivan Petrov" not in blob
    assert "Secret Lane" not in blob


# ---------------------------------------------------------------- test 5


def test_registered_advertising_maps_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    campaigns = {
        "list": [
            {
                "id": "7",
                "title": "Camp",
                "state": "ON",
                "advObjectType": "SKU",
                "paymentType": "CPC",
                "dailyBudget": "1000000",
                "budget": "2000000",
                "fromDate": "2026-08-01",
                "toDate": "2026-08-31",
            }
        ]
    }
    reports = {
        "items": [
            {
                "name": "Report",
                "meta": {
                    "state": "DONE",
                    "kind": "STATS",
                    "request": {
                        "dateFrom": "2026-08-01",
                        "dateTo": "2026-08-02",
                        "groupBy": "DATE",
                        "campaigns": ["7"],
                    },
                },
            }
        ],
        "total": "1",
    }
    expense = {
        "rows": [
            {
                "campaignId": 7,
                "date": "2026-08-01",
                "title": "Camp",
                "moneySpent": 150.5,
                "subscriptionSpent": 10.25,
                "bonusSpent": 5.0,
                # injected PII noise that must never reach the client
                "customer_name": "Ivan Petrov",
                "address": "Secret Lane 1",
            }
        ]
    }
    daily = {
        "rows": [
            {
                "campaignId": 7,
                "date": "2026-08-01",
                "title": "Camp",
                "views": 1000,
                "clicks": 40,
                "moneySpent": 150.5,
                "orders": 3,
                "ordersMoney": 2000.0,
                # injected PII noise that must never reach the client
                "customer_name": "Ivan Petrov",
                "address": "Secret Lane 1",
            }
        ]
    }
    recorder = RecordingExecutor(
        {"performance": [campaigns, reports, expense, daily]}
    )
    mcp = _mcp(monkeypatch, recorder)

    out = asyncio.run(
        _call(
            mcp,
            "ozon_advertising_analytics",
            date_from="2026-08-01",
            date_to="2026-08-02",
            page=2,
            page_size=10,
            campaign_ids=[7],
        )
    )

    assert [r[:2] for r in recorder.requests] == [
        ("performance", "ListCampaigns"),
        ("performance", "ListReports"),
        ("performance", "GetCampaignExpense"),
        ("performance", "GetCampaignDailyStats"),
    ]
    assert recorder.requests[0][2] == {"page": 2, "pageSize": 10, "campaignIds": [7]}
    assert recorder.requests[1][2] == {"page": 2, "pageSize": 10}
    stats_query = {
        "dateFrom": "2026-08-01",
        "dateTo": "2026-08-02",
        "campaignIds": [7],
    }
    assert recorder.requests[2][2] == stats_query
    assert recorder.requests[3][2] == stats_query

    camp = out["campaigns"][0]
    assert camp["id"] == 7
    assert camp["title"] == "Camp"
    assert camp["state"] == "ON"
    assert camp["daily_budget"] == 1.0
    assert camp["total_budget"] == 2.0
    rep = out["reports"][0]
    assert rep["name"] == "Report"
    assert rep["state"] == "DONE"
    assert rep["campaign_ids"] == [7]

    assert out["spend_rows"] == [
        {
            "campaign_id": 7,
            "date": "2026-08-01",
            "title": "Camp",
            "spend": 150.5,
            "subscription_spend": 10.25,
            "bonus_spend": 5.0,
        }
    ]
    assert out["spend_rows_count"] == 1
    assert out["daily_rows"] == [
        {
            "campaign_id": 7,
            "date": "2026-08-01",
            "title": "Camp",
            "impressions": 1000,
            "clicks": 40,
            "orders": 3,
            "spend": 150.5,
            "orders_revenue": 2000.0,
        }
    ]
    assert out["daily_rows_count"] == 1
    assert out["metrics"] == {
        "spend": 150.5,
        "subscription_spend": 10.25,
        "bonus_spend": 5.0,
        "impressions": 1000,
        "clicks": 40,
        "orders": 3,
        "orders_revenue": 2000.0,
    }
    assert out["spend_source"] == "expense"
    assert out["provenance"] == [
        "ListCampaigns",
        "ListReports",
        "GetCampaignExpense",
        "GetCampaignDailyStats",
    ]
    assert "drr" not in out
    assert "drr" not in out["metrics"]

    blob = json.dumps(out)
    assert "customer_name" not in blob
    assert "Ivan Petrov" not in blob
    assert "address" not in blob
    assert "Secret Lane" not in blob


# ---------------------------------------------------------------- test 6


def test_registered_catalog_and_retail_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    product_list = {"result": {"items": [{"offer_id": "OFF-1", "product_id": 1}], "last_id": ""}}
    stocks = {
        "items": [
            {
                "offer_id": "OFF-1",
                "product_id": 1,
                "stocks": [{"shipment_type": "FBO", "sku": 123, "present": 5, "reserved": 1}],
            }
        ]
    }
    prices = {
        "items": [
            {
                "offer_id": "OFF-1",
                "product_id": 1,
                "price": {"currency_code": "RUB", "price": 10.5, "vat": 0.1},
            }
        ]
    }
    fbo_posting = _posting("FBO")
    fbs_response = {"result": {"postings": [], "has_next": False}}

    recorder = RecordingExecutor(
        {"seller": [product_list, stocks, prices, {"result": [fbo_posting]}, fbs_response]}
    )
    mcp = _mcp(monkeypatch, recorder)

    cat = asyncio.run(_call(mcp, "ozon_catalog_mapping", offer_ids=["OFF-1"], limit=100))
    retail = asyncio.run(
        _call(
            mcp,
            "ozon_retailcrm_projection",
            date_from="2026-08-01",
            date_to="2026-08-02",
            limit=10,
        )
    )

    ops = [r[1] for r in recorder.requests[:3]]
    assert ops == [
        "ProductAPI_GetProductList",
        "ProductAPI_GetProductInfoStocks",
        "ProductAPI_GetProductInfoPrices",
    ]
    assert recorder.requests[0][2]["filter"] == {"offer_id": ["OFF-1"]}
    assert recorder.requests[1][2]["filter"] == {"offer_id": ["OFF-1"]}
    assert recorder.requests[2][2]["filter"] == {"offer_id": ["OFF-1"]}

    row = cat["rows"][0]
    assert row["offer_id"] == "OFF-1"
    assert row["skus"] == [123]
    assert row["stock"]["present"] == 5
    assert row["price"] == 10.5

    blob = json.dumps(retail)
    assert "Ivan Petrov" not in blob
    assert "customer_name" not in blob
    assert "Secret Lane" not in blob
    item = retail["items"][0]
    join_key = item["join_key"]
    assert len(join_key) == 64
    int(join_key, 16)  # hex HMAC digest
    assert join_key != hashlib.sha256(b"FBO-0001").hexdigest()
    assert item["currency"] == "RUB"


# ------------------------------------------- snapshot evidence (content-bound)


_META_BYTES = json.dumps(
    {
        "refreshed_at": "2026-04-16T21:47:53Z",
        "seller": {
            "sha256": "c54962e9481ac776e14c0fe4f987e0ff74fde68a793e6b640f70db6cdaabdba5"
        },
        "performance": {
            "sha256": "27ad70709fdcc6323c4ba9d7d9af20e21a39397023e2776a6c171ab08aa05a06"
        },
    }
).encode("utf-8")


def test_bundled_specs_hash_to_metadata_and_disclose_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) real bundled bytes hash exactly to metadata; stale + nonblocking."""
    evidence = core._snapshot_evidence()["spec_snapshot"]
    assert evidence["seller_sha"] == (
        "c54962e9481ac776e14c0fe4f987e0ff74fde68a793e6b640f70db6cdaabdba5"
    )
    assert evidence["performance_sha"] == (
        "27ad70709fdcc6323c4ba9d7d9af20e21a39397023e2776a6c171ab08aa05a06"
    )
    actual = core._actual_content_hashes()
    assert actual["seller"] == evidence["seller_sha"]
    assert actual["performance"] == evidence["performance_sha"]
    assert evidence["hashes_verified"] is True
    assert evidence["stale"] is True  # April snapshot at the current date
    assert evidence["blocking"] is False
    assert evidence["reason"]


@pytest.mark.parametrize(
    ("seller_sha", "perf_sha"),
    [
        ("f" * 64, None),  # seller digest mismatch
        (None, "0" * 64),  # performance digest mismatch
        (None, None),  # both hashes absent from metadata
    ],
)
def test_bad_hash_blocks_before_catalog_and_transport(
    monkeypatch: pytest.MonkeyPatch, seller_sha: str | None, perf_sha: str | None
) -> None:
    """(b) mismatch/absent hash blocks before catalog lookup or transport."""
    meta = json.loads(_META_BYTES)
    if seller_sha is None:
        meta["seller"].pop("sha256")
    else:
        meta["seller"]["sha256"] = seller_sha
    if perf_sha is None:
        meta["performance"].pop("sha256")
    else:
        meta["performance"]["sha256"] = perf_sha

    real_files = core.files("ozon_mcp.data")

    class _FakeDir:
        def __truediv__(self, name: str) -> Any:
            if name == "swagger_meta.json":
                return _FakeMetaFile(json.dumps(meta).encode("utf-8"))
            return real_files / name

    class _FakeMetaFile:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read_text(self, encoding: str = "utf-8") -> str:
            return self._payload.decode("utf-8")

    monkeypatch.setattr(core, "files", lambda pkg: _FakeDir())
    catalog = _DummyCatalog()
    executor = core._Executor(catalog, object(), None)
    calls = catalog.calls
    with pytest.raises(core._LocalContractError) as err:
        executor._resolve("SellerAPI_SellerInfo", "seller")
    assert err.value.category == "spec_unavailable"
    assert catalog.calls == calls  # catalog never consulted


def test_missing_or_malformed_evidence_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) missing spec file and malformed spec bytes both block."""
    real_files = core.files("ozon_mcp.data")

    class _MissingSpecDir:
        def __truediv__(self, name: str) -> Any:
            if name == "perf_swagger.json":
                raise FileNotFoundError(name)
            return real_files / name

        def read_text(self, encoding: str = "utf-8") -> str:
            return "{ not json"

    monkeypatch.setattr(core, "files", lambda pkg: _MissingSpecDir())
    with pytest.raises(core._LocalContractError) as err:
        core._snapshot_evidence()
    assert err.value.category == "spec_unavailable"


def test_stale_exact_contract_continues_with_stale_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) verified-stale snapshot: exact allowlisted call proceeds."""
    real_evidence = core._snapshot_evidence
    recorder = RecordingExecutor({"seller": [{"result": {}}]})
    mcp = _mcp(monkeypatch, recorder)
    # restore the real (content-bound) evidence for the response guard
    monkeypatch.setattr(core, "_snapshot_evidence", real_evidence)
    payload = asyncio.run(_call(mcp, "ozon_seller_status"))
    snapshot = payload["spec_snapshot"]
    assert snapshot["stale"] is True
    assert snapshot["blocking"] is False
    assert snapshot["hashes_verified"] is True
    assert recorder.requests[0][1] == "SellerAPI_SellerInfo"


# ------------------------------------------- executor transport kwargs (P1-1)


class _FakeMethod:
    def __init__(self, operation_id: str, api: str, section: str) -> None:
        self.operation_id = operation_id
        self.api = api
        self.section = section
        self.method = "POST"
        self.path = "/v1/seller/info"


class _FakeCatalog:
    def get_by_operation_id(self, operation_id: str) -> Any:
        if operation_id == "SellerAPI_SellerInfo":
            return _FakeMethod(operation_id, "seller", "Seller")
        if operation_id == "ListCampaigns":
            return _FakeMethod(operation_id, "performance", "Performance")
        return None


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"http_method": method, "path": path, **kwargs})
        return {"result": {}}


def test_executor_passes_operation_section_and_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seller and Performance request calls carry exact operation_id,
    section, and with_retry=False."""
    monkeypatch.setattr(core, "_snapshot_evidence", lambda: {})
    seller = _RecordingClient()
    performance = _RecordingClient()
    executor = core._Executor(_FakeCatalog(), seller, performance)

    asyncio.run(executor.seller("SellerAPI_SellerInfo", {}))
    asyncio.run(executor.performance("ListCampaigns"))

    assert seller.calls == [
        {
            "http_method": "POST",
            "path": "/v1/seller/info",
            "json_body": {},
            "operation_id": "SellerAPI_SellerInfo",
            "section": "Seller",
            "with_retry": False,
        }
    ]
    assert performance.calls == [
        {
            "http_method": "POST",
            "path": "/v1/seller/info",
            "query_params": {},
            "operation_id": "ListCampaigns",
            "section": "Performance",
            "with_retry": False,
        }
    ]


# ------------------------------------------- finance completeness (2026-09-06)


def _fin_op(op_type: str = "OrderCommission", amount: str = "10.00") -> dict:
    return {"operation_type": op_type, "amount": amount, "services": []}


def test_finance_page_size_1000_fails_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """page_size=1000 is rejected with invalid_params before the executor."""
    recorder = RecordingExecutor({"seller": []})
    mcp = _mcp(monkeypatch, recorder)
    out = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=1000,
        )
    )
    assert out["error"]["category"] == "invalid_params"
    assert "page_size" in out["error"]["message"]
    assert recorder.requests == []  # no provider call was made


def test_finance_measured_regression_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured RED: 100 fetched ops, row_count=985, no page_count.

    A provider-capped page must never be published as complete.
    """
    response = {
        "result": {
            "operations": [_fin_op() for _ in range(100)],
            "row_count": 985,
        }
    }
    recorder = RecordingExecutor({"seller": [response]})
    mcp = _mcp(monkeypatch, recorder)
    out = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=100,
        )
    )
    assert recorder.requests[0][2]["page_size"] == 100
    assert out["fetched_operations"] == 100
    assert out["total_reported"] == 985
    assert out["completeness"] == "partial"
    assert out["truncation"] == {
        "truncated": True,
        "reason": "row_count exceeds fetched operations",
    }
    # aggregated buckets hold fetched rows only — no invented duplicates
    assert out["row_count"] == 1
    assert out["rows"][0]["operation_count"] == 100


def test_finance_complete_single_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full coverage proven by provider totals: page_count=1, row_count=fetched."""
    response = {
        "result": {
            "operations": [_fin_op("OrderCommission"), _fin_op("Delivery")],
            "row_count": 2,
            "page_count": 1,
        }
    }
    recorder = RecordingExecutor({"seller": [response]})
    mcp = _mcp(monkeypatch, recorder)
    out = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=100,
        )
    )
    assert out["completeness"] == "complete"
    assert out["truncation"] is None
    assert out["fetched_operations"] == 2
    assert out["total_reported"] == 2
    assert out["page_count_reported"] == 1
    assert [r["operation_type"] for r in out["rows"]] == ["Delivery", "OrderCommission"]


def test_finance_ambiguous_full_page_and_later_page_are_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full page without provider totals, and page>1, are both partial."""
    ambiguous = {
        "result": {"operations": [_fin_op() for _ in range(100)]}
    }
    recorder = RecordingExecutor({"seller": [ambiguous, ambiguous]})
    mcp = _mcp(monkeypatch, recorder)
    full_first = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=100,
        )
    )
    assert full_first["completeness"] == "partial"
    assert full_first["truncation"]["reason"] == (
        "full first page without provider totals"
    )
    later = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page=2,
            page_size=100,
        )
    )
    assert later["completeness"] == "partial"
    assert later["truncation"]["reason"] == "non-first page requested"


def test_finance_short_page_without_totals_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both totals absent on a short first page: partial, never complete."""
    response = {
        "result": {"operations": [_fin_op() for _ in range(3)]}
    }
    recorder = RecordingExecutor({"seller": [response]})
    mcp = _mcp(monkeypatch, recorder)
    out = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=100,
        )
    )
    assert out["fetched_operations"] == 3
    assert out["completeness"] == "partial"
    assert out["truncation"] == {
        "truncated": True,
        "reason": "provider totals absent or unintelligible",
    }


def test_finance_contradictory_row_count_below_fetched_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """row_count smaller than the fetched page contradicts coverage."""
    response = {
        "result": {
            "operations": [_fin_op() for _ in range(5)],
            "row_count": 2,
        }
    }
    recorder = RecordingExecutor({"seller": [response]})
    mcp = _mcp(monkeypatch, recorder)
    out = asyncio.run(
        _call(
            mcp,
            "ozon_finance_analytics",
            date_from="2026-01-01",
            date_to="2026-01-31",
            page_size=100,
        )
    )
    assert out["fetched_operations"] == 5
    assert out["total_reported"] == 2
    assert out["completeness"] == "partial"
    assert out["truncation"] == {
        "truncated": True,
        "reason": "row_count below fetched operations",
    }


def test_provider_rate_limit_envelope_is_closed() -> None:
    """429 maps to rate_limited with status, operation id, integer
    retry_after_seconds, and no payload or raw provider message."""
    from ozon_mcp.errors import OzonRateLimitError

    exc = OzonRateLimitError(
        "too many requests: raw provider detail with secret-token",
        status_code=429,
        operation_id="SellerAPI_SellerInfo",
        payload={"internal": "raw-provider-data"},
        retry_after=1.2,
    )
    envelope = core._provider_error(exc)["error"]
    assert envelope["category"] == "rate_limited"
    assert envelope["status_code"] == 429
    assert envelope["operation_id"] == "SellerAPI_SellerInfo"
    assert envelope["retryable"] is True
    assert isinstance(envelope["retry_after_seconds"], int)
    assert envelope["retry_after_seconds"] == 2
    blob = json.dumps(envelope)
    assert "payload" not in blob
    assert "raw provider detail" not in blob
    assert "internal" not in blob
