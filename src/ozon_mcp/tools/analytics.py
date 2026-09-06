"""PetDog strict read-only Ozon analytics surface.

Ten contract-bound MCP tools backed by an internal exact-operation executor.
The executor resolves each call from the :class:`~ozon_mcp.schema.catalog.Catalog`,
refuses anything off the read-only allowlist before an HTTP request can be
constructed, selects the Seller or Performance client by ``method.api``, and
returns only closed, sanitized projections -- never raw provider payloads,
credentials, PII, or upstream error text.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ozon_mcp.readonly import build_reconciliation_projection
from ozon_mcp.schema.catalog import Catalog
from ozon_mcp.tools.analytics_advertising import build_advertising_result
from ozon_mcp.tools.analytics_core import (
    _MAX_CAMPAIGN_IDS,
    _MAX_ROWS,
    _RO,
    _as_mapping_list,
    _clamp_int,
    _clean_offer_ids,
    _ContractFailureError,
    _Executor,
    _fetch_returns_rows,
    _fetch_sales_rows,
    _int_or,
    _iter_posting_products,
    _LocalContractError,
    _money,
    _posting_amount,
    _project_returns_rows,
    _project_sales_rows,
    _safe_error,
    _text,
    _tool_guard,
    _utc_boundary,
    _validate_period,
    _walk_mapping,
)
from ozon_mcp.transport.performance import PerformanceClient
from ozon_mcp.transport.seller import SellerClient

__all__ = ["register"]


# --- registration ------------------------------------------------------------

def register(
    mcp: FastMCP,
    catalog: Catalog,
    seller_client: SellerClient | None,
    performance_client: PerformanceClient | None,
    *,
    hmac_secret: str | None,
) -> None:
    """Register the ten PetDog read-only analytics tools."""

    def _make_executor() -> _Executor:
        return _Executor(catalog, seller_client, performance_client)

    # 1. seller_status -------------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_seller_status() -> dict[str, Any]:
        """Ozon Seller API reachability and scalar subscription/tariff codes."""
        try:
            executor = _make_executor()
            data = await executor.seller("SellerAPI_SellerInfo", {})
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        subscription = _walk_mapping(data.get("subscription"))
        tariffication = _walk_mapping(data.get("tariffication"))
        raw_premium = subscription.get("is_premium")
        if isinstance(raw_premium, bool):
            is_premium: bool | None = raw_premium
        elif raw_premium in (0, 1):
            is_premium = bool(raw_premium)
        else:
            is_premium = None
        result = {
            "source_channel": "ozon",
            "reachable": True,
            "period": None,
            "date_basis": "current_provider_snapshot",
            "completeness": "complete",
            "truncation": None,
            "configured": {
                "seller_api": seller_client is not None,
                "performance_api": performance_client is not None,
            },
            "subscription": {
                "type": _text(subscription.get("type")),
                "is_premium": is_premium,
                "valid_until": _text(subscription.get("valid_until")),
            },
            "tariffication": {
                "fbo_count": _int_or(tariffication.get("fbo_count")),
                "fbs_count": _int_or(tariffication.get("fbs_count")),
            },
            "provenance": ["SellerAPI_SellerInfo"],
        }
        return _tool_guard(result)

    # 2. sales_analytics -----------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_sales_analytics(
        date_from: str, date_to: str, limit: int = 500
    ) -> dict[str, Any]:
        """Aggregate Ozon sales by offer_id+sku+status; no order identifiers."""
        try:
            start, end = _validate_period(date_from, date_to)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            fetched = await _fetch_sales_rows(executor, start, end, limit)
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        rows, summary = _project_sales_rows(fetched["rows"])
        return _tool_guard(
            {
                "period": {"date_from": date_from, "date_to": date_to},
                "date_basis": "posting_creation_date",
                "rows": rows,
                "row_count": len(rows),
                "fetched_postings": fetched["row_count"],
                "currencies": summary["currency_codes"],
                "invalid_money_rows": summary["invalid_money_rows"],
                "completeness": "partial" if fetched["truncated"] else "complete",
                "truncation": (
                    {"truncated": True, "reason": "page limit reached"}
                    if fetched["truncated"]
                    else None
                ),
                "provenance": [
                    "PostingAPI_GetFboPostingList",
                    "PostingAPI_GetFbsPostingListV3",
                ],
            }
        )

    # 3. finance_analytics ---------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_finance_analytics(
        date_from: str, date_to: str, page: int = 1, page_size: int = 100
    ) -> dict[str, Any]:
        """Aggregate Ozon finance operations by operation_type."""
        try:
            start, end = _validate_period(date_from, date_to)
            page = _clamp_int(page, 1, 10_000, "page")
            # The provider silently caps FinanceTransactionListV3 pages at
            # 100 operations regardless of the requested page_size, so a
            # larger request would be reported as fetched while the window
            # is actually provider-truncated. Reject before the executor.
            page_size = _clamp_int(page_size, 1, 100, "page_size")
            executor = _make_executor()
            data = await executor.seller(
                "FinanceAPI_FinanceTransactionListV3",
                {
                    "filter": {
                        "date": {
                            "from": _utc_boundary(start, end=False),
                            "to": _utc_boundary(end, end=True),
                        }
                    },
                    "page": page,
                    "page_size": page_size,
                },
            )
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        result = _walk_mapping(data.get("result"))
        operations = _as_mapping_list(result.get("operations"))
        aggregate: dict[str, dict[str, Any]] = {}
        for op in operations:
            op_type = _text(op.get("operation_type")) or "unknown"
            bucket = aggregate.setdefault(
                op_type,
                {
                    "operation_type": op_type,
                    "operation_count": 0,
                    "amount": 0.0,
                    "accruals_for_sale": 0.0,
                    "sale_commission": 0.0,
                    "delivery_charge": 0.0,
                    "return_delivery_charge": 0.0,
                    "services_total": 0.0,
                },
            )
            bucket["operation_count"] += 1
            for field in (
                "amount",
                "accruals_for_sale",
                "sale_commission",
                "delivery_charge",
                "return_delivery_charge",
            ):
                value = _money(op.get(field))
                if value is not None:
                    bucket[field] = round(bucket[field] + value, 2)
            for service in _as_mapping_list(op.get("services"), 500):
                value = _money(service.get("price"))
                if value is not None:
                    bucket["services_total"] = round(
                        bucket["services_total"] + value, 2
                    )
        # Completeness must be proven by usable provider evidence, never
        # defaulted. A total is usable only when it was actually reported
        # and is intelligible (a non-negative integer). A first page is
        # complete only when row_count equals the fetched operations or
        # page_count proves there are no later pages, and no reported
        # total contradicts the fetched page or the requested page.
        row_count_raw = result.get("row_count")
        page_count_raw = result.get("page_count")
        reported_row_count = _int_or(row_count_raw, -1)
        reported_page_count = _int_or(page_count_raw, -1)
        row_usable = row_count_raw is not None and reported_row_count >= 0
        page_usable = page_count_raw is not None and reported_page_count >= 0
        fetched = len(operations)
        reasons: list[str] = []
        if row_usable and reported_row_count > fetched:
            reasons.append("row_count exceeds fetched operations")
        if row_usable and reported_row_count < fetched:
            reasons.append("row_count below fetched operations")
        if page_usable and reported_page_count > page:
            reasons.append("later pages reported")
        if page_usable and reported_page_count < page:
            reasons.append("page_count behind requested page")
        if page > 1:
            reasons.append("non-first page requested")
        if page == 1 and not row_usable and not page_usable:
            # No usable total exists to prove first-page coverage — not
            # even for a short page, since absence of totals is not
            # evidence of a single-page result set.
            reasons.append(
                "full first page without provider totals"
                if fetched == page_size
                else "provider totals absent or unintelligible"
            )
        fetched_all = not reasons
        total = reported_row_count if reported_row_count >= 0 else fetched
        page_count = reported_page_count if reported_page_count >= 0 else (
            page if fetched_all else page + 1
        )
        return _tool_guard(
            {
                "period": {"date_from": date_from, "date_to": date_to},
                "date_basis": "operation_accrual_date",
                "page": page,
                "page_size": page_size,
                "rows": sorted(aggregate.values(), key=lambda b: b["operation_type"]),
                "row_count": len(aggregate),
                "fetched_operations": fetched,
                "total_reported": total,
                "page_count_reported": page_count,
                "completeness": "complete" if fetched_all else "partial",
                "truncation": (
                    None
                    if fetched_all
                    else {"truncated": True, "reason": "; ".join(reasons)}
                ),
                "provenance": ["FinanceAPI_FinanceTransactionListV3"],
            }
        )

    # 4. returns_analytics ---------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_returns_analytics(
        date_from: str, date_to: str, limit: int = 100
    ) -> dict[str, Any]:
        """Aggregate Ozon returns by offer/SKU/status/reason."""
        try:
            start, end = _validate_period(date_from, date_to)
            limit = _clamp_int(limit, 1, 500, "limit")
            executor = _make_executor()
            fetched = await _fetch_returns_rows(executor, start, end, limit)
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        rows = _project_returns_rows(fetched["rows"])
        return _tool_guard(
            {
                "period": {"date_from": date_from, "date_to": date_to},
                "date_basis": "logistic_return_date",
                "rows": rows,
                "row_count": len(rows),
                "fetched_returns": fetched["row_count"],
                "completeness": "partial" if fetched["truncated"] else "complete",
                "truncation": (
                    {"truncated": True, "reason": "limit reached"}
                    if fetched["truncated"]
                    else None
                ),
                "provenance": ["returnsList"],
            }
        )

    # 5. inventory_analytics -------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_inventory_analytics(
        offer_ids: list[str] | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Ozon stock by offer_id: present/reserved/available, SKUs, shipment."""
        try:
            ids = _clean_offer_ids(offer_ids)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            body: dict[str, Any] = {
                "filter": {"offer_id": ids} if ids else {},
                "limit": limit,
            }
            data = await executor.seller(
                "ProductAPI_GetProductInfoStocks", body
            )
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        items = _as_mapping_list(data.get("items"))
        total = _int_or(data.get("total"), len(items))
        cursor = _text(data.get("cursor")) or ""
        rows: list[dict[str, Any]] = []
        for item in items[:limit]:
            present = reserved = 0
            skus: set[int] = set()
            shipment_types: set[str] = set()
            for stock in _as_mapping_list(item.get("stocks"), 500):
                shipment_type = _text(stock.get("shipment_type")) or ""
                if shipment_type:
                    shipment_types.add(shipment_type)
                present += _int_or(stock.get("present"), 0)
                reserved += _int_or(stock.get("reserved"), 0)
                sku = _int_or(stock.get("sku"), 0)
                if sku:
                    skus.add(sku)
            rows.append(
                {
                    "offer_id": _text(item.get("offer_id")),
                    "product_id": _int_or(item.get("product_id"), 0) or None,
                    "total_present": present,
                    "total_reserved": reserved,
                    "available": max(present - reserved, 0),
                    "skus": sorted(skus)[:50],
                    "shipment_types": sorted(shipment_types),
                }
            )
        truncated = total > len(rows) or bool(cursor)
        return _tool_guard(
            {
                "period": None,
                "date_basis": "current_provider_snapshot",
                "rows": rows,
                "row_count": len(rows),
                "total": total,
                "cursor": cursor or None,
                "completeness": "partial" if truncated else "complete",
                "truncation": (
                    {"truncated": True, "reason": "more pages available"}
                    if truncated
                    else None
                ),
                "provenance": ["ProductAPI_GetProductInfoStocks"],
            }
        )

    # 6. price_promotion_analytics -------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_price_promotion_analytics(
        offer_ids: list[str] | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Ozon prices and promotion flags by offer_id (closed field set)."""
        try:
            ids = _clean_offer_ids(offer_ids)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            body: dict[str, Any] = {
                "filter": {"offer_id": ids} if ids else {},
                "limit": limit,
            }
            data = await executor.seller(
                "ProductAPI_GetProductInfoPrices", body
            )
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        items = _as_mapping_list(data.get("items"))
        total = _int_or(data.get("total"), len(items))
        cursor = _text(data.get("cursor")) or ""
        rows: list[dict[str, Any]] = []
        for item in items[:limit]:
            price = _walk_mapping(item.get("price"))
            marketing_actions = _walk_mapping(item.get("marketing_actions"))
            action_items = _as_mapping_list(
                marketing_actions.get("actions"), 20
            )
            row_actions: list[dict[str, Any]] = []
            for action in action_items:
                row_actions.append(
                    {
                        "title": _text(action.get("title")),
                        "date_from": _text(action.get("date_from")),
                        "date_to": _text(action.get("date_to")),
                        "value": _money(action.get("value")),
                    }
                )
            rows.append(
                {
                    "offer_id": _text(item.get("offer_id")),
                    "product_id": _int_or(item.get("product_id"), 0) or None,
                    "currency": _text(price.get("currency_code")),
                    "price": _money(price.get("price")),
                    "old_price": _money(price.get("old_price")),
                    "min_price": _money(price.get("min_price")),
                    "marketing_seller_price": _money(
                        price.get("marketing_seller_price")
                    ),
                    "net_price": _money(price.get("net_price")),
                    "vat": _money(price.get("vat")),
                    "ozon_actions_exist": bool(
                        marketing_actions.get("ozon_actions_exist")
                    ),
                    "actions": row_actions,
                }
            )
        truncated = total > len(rows) or bool(cursor)
        return _tool_guard(
            {
                "period": None,
                "date_basis": "current_provider_snapshot",
                "rows": rows,
                "row_count": len(rows),
                "total": total,
                "cursor": cursor or None,
                "completeness": "partial" if truncated else "complete",
                "truncation": (
                    {"truncated": True, "reason": "more pages available"}
                    if truncated
                    else None
                ),
                "provenance": ["ProductAPI_GetProductInfoPrices"],
            }
        )

    # 7. advertising_analytics ------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_advertising_analytics(
        date_from: str,
        date_to: str,
        campaign_ids: list[int] | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Closed campaign, spend and daily performance analytics.

        Returns campaign configuration, per-campaign spend and daily
        performance rows from synchronous read-only Performance endpoints
        (no report generation or file downloads).
        """
        try:
            _validate_period(date_from, date_to)
            page = _clamp_int(page, 1, 1000, "page")
            page_size = _clamp_int(page_size, 1, 200, "page_size")
            if campaign_ids is not None and len(campaign_ids) > _MAX_CAMPAIGN_IDS:
                raise _LocalContractError(
                    "invalid_params",
                    f"at most {_MAX_CAMPAIGN_IDS} campaign_ids allowed",
                )
            executor = _make_executor()
            result = await build_advertising_result(
                executor, date_from, date_to, campaign_ids, page, page_size
            )
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        return _tool_guard(result)

    # 8. sku_performance -----------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_sku_performance(
        date_from: str, date_to: str, limit: int = 500
    ) -> dict[str, Any]:
        """Per-SKU sold/returned units and return rate from sales+returns."""
        try:
            start, end = _validate_period(date_from, date_to)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            sales = await _fetch_sales_rows(executor, start, end, limit)
            returns = await _fetch_returns_rows(executor, start, end, min(limit, 500))
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        sales_rows, _ = _project_sales_rows(sales["rows"])
        returns_rows = _project_returns_rows(returns["rows"])
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for row in sales_rows:
            key = (row["offer_id"], str(row["sku"]))
            bucket = combined.setdefault(
                key,
                {
                    "offer_id": row["offer_id"],
                    "sku": row["sku"],
                    "sold_units": 0,
                    "gross_revenue": 0.0,
                    "returned_units": 0,
                    "return_rate": None,
                },
            )
            bucket["sold_units"] += row["units"]
            bucket["gross_revenue"] = round(
                bucket["gross_revenue"] + row["gross_revenue"], 2
            )
        for row in returns_rows:
            key = (row["offer_id"], str(row["sku"]))
            bucket = combined.setdefault(
                key,
                {
                    "offer_id": row["offer_id"],
                    "sku": row["sku"],
                    "sold_units": 0,
                    "gross_revenue": 0.0,
                    "returned_units": 0,
                    "return_rate": None,
                },
            )
            bucket["returned_units"] += row["units"]
        for bucket in combined.values():
            if bucket["sold_units"] > 0:
                bucket["return_rate"] = round(
                    bucket["returned_units"] / bucket["sold_units"], 6
                )
        truncated = sales["truncated"] or returns["truncated"]
        return _tool_guard(
            {
                "period": {"date_from": date_from, "date_to": date_to},
                "date_basis": "mixed_sales_and_return_dates",
                "rows": sorted(
                    combined.values(), key=lambda b: (b["offer_id"], str(b["sku"]))
                )[:_MAX_ROWS],
                "row_count": len(combined),
                "completeness": "partial" if truncated else "complete",
                "truncation": (
                    {"truncated": True, "reason": "page limit reached"}
                    if truncated
                    else None
                ),
                "provenance": [
                    "PostingAPI_GetFboPostingList",
                    "PostingAPI_GetFbsPostingListV3",
                    "returnsList",
                ],
            }
        )

    # 9. catalog_mapping -----------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_catalog_mapping(
        offer_ids: list[str] | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Closed offer_id->product_id/SKU/stock/price mapping with completeness."""
        try:
            ids = _clean_offer_ids(offer_ids)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            body: dict[str, Any] = {"filter": {}, "limit": limit, "last_id": ""}
            if ids:
                body["filter"] = {"offer_id": ids}
            listing = await executor.seller("ProductAPI_GetProductList", body)
            source_body: dict[str, Any] = {"filter": {}, "limit": limit}
            if ids:
                source_body["filter"] = {"offer_id": ids}
            stocks = await executor.seller(
                "ProductAPI_GetProductInfoStocks", source_body
            )
            prices = await executor.seller(
                "ProductAPI_GetProductInfoPrices", source_body
            )
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        listing_result = _walk_mapping(listing.get("result"))
        listing_last_id = _text(listing_result.get("last_id"))
        base: dict[str, dict[str, Any]] = {}
        for item in _as_mapping_list(listing_result.get("items"), limit):
            offer = _text(item.get("offer_id"))
            if not offer:
                continue
            base[offer] = {
                "offer_id": offer,
                "product_id": _int_or(item.get("product_id"), 0) or None,
                "skus": [],
                "stock": None,
                "price": None,
                "currency": None,
                "vat": None,
            }
        for stock in _as_mapping_list(stocks.get("items"), limit):
            offer = _text(stock.get("offer_id"))
            if offer in base:
                nested = _as_mapping_list(stock.get("stocks"), 200)
                present = sum(_int_or(s.get("present"), 0) for s in nested)
                reserved = sum(_int_or(s.get("reserved"), 0) for s in nested)
                base[offer]["stock"] = {
                    "present": present,
                    "reserved": reserved,
                    "available": max(present - reserved, 0),
                }
                skus = set(base[offer]["skus"])
                skus.update(
                    s.get("sku")
                    for s in nested
                    if isinstance(s.get("sku"), int)
                    and not isinstance(s.get("sku"), bool)
                )
                base[offer]["skus"] = sorted(skus)
        for price in _as_mapping_list(prices.get("items"), limit):
            offer = _text(price.get("offer_id"))
            if offer in base:
                node = _walk_mapping(price.get("price"))
                base[offer]["price"] = _money(node.get("price"))
                base[offer]["currency"] = _text(node.get("currency_code"))
                base[offer]["vat"] = _money(node.get("vat"))
        rows = sorted(base.values(), key=lambda r: r["offer_id"])[:_MAX_ROWS]
        complete_stocks = sum(1 for r in rows if r["stock"] is not None)
        complete_prices = sum(1 for r in rows if r["price"] is not None)
        completeness = (
            "complete"
            if rows
            and not listing_last_id
            and complete_stocks == len(rows)
            and complete_prices == len(rows)
            else "partial"
        )
        return _tool_guard(
            {
                "period": None,
                "date_basis": "current_provider_snapshot",
                "rows": rows,
                "row_count": len(rows),
                "completeness": completeness,
                "truncation": (
                    {
                        "truncated": True,
                        "reason": "more pages available or source projection incomplete",
                    }
                    if completeness == "partial"
                    else None
                ),
                "per_source_completeness": {
                    "ProductAPI_GetProductList": len(rows),
                    "ProductAPI_GetProductInfoStocks": complete_stocks,
                    "ProductAPI_GetProductInfoPrices": complete_prices,
                },
                "provenance": [
                    "ProductAPI_GetProductList",
                    "ProductAPI_GetProductInfoStocks",
                    "ProductAPI_GetProductInfoPrices",
                ],
            }
        )

    # 10. retailcrm_projection ------------------------------------------------
    @mcp.tool(annotations=_RO)
    async def ozon_retailcrm_projection(
        date_from: str, date_to: str, limit: int = 500
    ) -> dict[str, Any]:
        """HMAC-anonymized Ozon rows for RetailCRM reconciliation (no raw ids)."""
        if not isinstance(hmac_secret, str) or not hmac_secret:
            return _safe_error(
                "unavailable",
                "reconciliation projection requires a configured hmac secret",
            )
        try:
            start, end = _validate_period(date_from, date_to)
            limit = _clamp_int(limit, 1, 1000, "limit")
            executor = _make_executor()
            fetched = await _fetch_sales_rows(executor, start, end, limit)
        except _ContractFailureError as failure:
            return failure.envelope
        except _LocalContractError as exc:
            return _safe_error(exc.category, exc.message)
        dedupe_rows: list[dict[str, Any]] = []
        for posting in fetched["rows"]:
            products = list(_iter_posting_products(posting))
            first = products[0] if products else {}
            currency = _text(first.get("currency_code")) or ""
            amount_total = 0.0
            for product in products:
                quantity = max(_int_or(product.get("quantity"), 1), 1)
                amount_total += _posting_amount(product, quantity)
            dedupe_rows.append(
                {
                    "posting_number": _text(posting.get("posting_number")),
                    "order_id": _text(posting.get("order_number"))
                    or _text(posting.get("order_id")),
                    "amount": round(amount_total, 2) if amount_total else None,
                    "status": _text(posting.get("status")),
                    "created_at": _text(posting.get("created_at"))
                    or _text(posting.get("in_process_at")),
                    "currency": currency,
                    "offer_id": _text(first.get("offer_id")),
                    "sku": first.get("sku"),
                }
            )
        projection = build_reconciliation_projection(dedupe_rows, hmac_secret)
        return _tool_guard(
            {
                "period": {"date_from": date_from, "date_to": date_to},
                "date_basis": "posting_creation_date",
                "items": projection["items"],
                "item_count": projection["item_count"],
                "duplicate_count": projection["duplicate_count"],
                "skipped_count": projection["skipped_count"],
                "completeness": "partial" if fetched["truncated"] else "complete",
                "truncation": (
                    {"truncated": True, "reason": "page limit reached"}
                    if fetched["truncated"]
                    else None
                ),
                "provenance": [
                    "PostingAPI_GetFboPostingList",
                    "PostingAPI_GetFbsPostingListV3",
                ],
            }
        )
