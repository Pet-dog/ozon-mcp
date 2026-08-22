"""Closed advertising projection helpers for the PetDog read-only surface.

Builds the ``ozon_advertising_analytics`` result from four strict Performance
executor calls (ListCampaigns, ListReports, GetCampaignExpense,
GetCampaignDailyStats) without generating reports or downloading files.
Only closed, sanitized rows leave this module -- never raw provider
payloads, credentials, PII, or upstream error text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ozon_mcp.tools.analytics_core import (
    _MAX_CAMPAIGN_IDS,
    _MAX_ROWS,
    _as_mapping_list,
    _int_or,
    _money,
    _text,
    _walk_list,
    _walk_mapping,
)

__all__ = ["build_advertising_result"]


def _campaign_id(value: Any) -> int | None:
    """Normalize a safe campaign id (int or digit string); else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _millionth_rubles(value: Any) -> float | None:
    amount = _money(value)
    if amount is None:
        return None
    return round(amount / 1_000_000, 2)


def _flatten_stats_rows(payload: Any) -> list[Mapping[str, Any]]:
    """Flatten only recognized Performance expense/daily payload shapes.

    Accepted shapes:
    * BaseClient list wrapper ``{"data": [...]}``;
    * a mapping whose ``rows``/``list``/``items``/``data`` keys hold lists;
    * a mapping whose ``data`` value is itself a mapping with
      ``rows``/``list``/``items``/``data`` lists (one bounded level only);
    * a mapping whose digit campaign-id keys map to mappings or lists --
      the key is injected into rows lacking a campaign id.

    Inputs and outputs are capped at ``_MAX_ROWS``.
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return _as_mapping_list(data, _MAX_ROWS)
    if isinstance(data, Mapping):
        nested: list[Mapping[str, Any]] = []
        for key in ("rows", "list", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                nested.extend(_as_mapping_list(value, _MAX_ROWS))
                if len(nested) >= _MAX_ROWS:
                    return nested[:_MAX_ROWS]
        if nested:
            return nested[:_MAX_ROWS]
    rows: list[Mapping[str, Any]] = []
    for key in ("rows", "list", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(_as_mapping_list(value, _MAX_ROWS))
            if len(rows) >= _MAX_ROWS:
                return rows[:_MAX_ROWS]
    if rows:
        return rows[:_MAX_ROWS]
    for key, value in payload.items():
        if not (isinstance(key, str) and key.isdigit()):
            continue
        if isinstance(value, Mapping):
            candidates: list[Mapping[str, Any]] = [value]
        elif isinstance(value, list):
            candidates = _as_mapping_list(value, _MAX_ROWS)
        else:
            continue
        for row in candidates:
            if _campaign_id(row.get("campaignId")) is None and (
                _campaign_id(row.get("campaign_id")) is None
                and _campaign_id(row.get("id")) is None
            ):
                row = dict(row)
                row.setdefault("campaignId", int(key))
            rows.append(row)
            if len(rows) >= _MAX_ROWS:
                return rows[:_MAX_ROWS]
    return rows[:_MAX_ROWS]


def _pick(row: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    return None


_CAMPAIGN_ALIASES = ("campaignId", "campaign_id", "id")
_DATE_ALIASES = ("date",)
_TITLE_ALIASES = ("title", "name")
_SPEND_ALIASES = ("moneySpent", "spend", "expense")
_SUB_SPEND_ALIASES = ("subscriptionSpent", "subscription_spend")
_BONUS_SPEND_ALIASES = ("bonusSpent", "bonus_spend")
_IMPRESSIONS_ALIASES = ("views", "impressions")
_CLICKS_ALIASES = ("clicks",)
_ORDERS_ALIASES = ("orders",)
_REVENUE_ALIASES = ("ordersMoney", "orders_revenue", "revenue")


def _project_spend_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:_MAX_ROWS]:
        campaign_id = _campaign_id(_pick(row, _CAMPAIGN_ALIASES))
        if campaign_id is None:
            continue
        projected: dict[str, Any] = {"campaign_id": campaign_id}
        date = _text(_pick(row, _DATE_ALIASES))
        if date is not None:
            projected["date"] = date
        title = _text(_pick(row, _TITLE_ALIASES))
        if title is not None:
            projected["title"] = title
        for field, aliases in (
            ("spend", _SPEND_ALIASES),
            ("subscription_spend", _SUB_SPEND_ALIASES),
            ("bonus_spend", _BONUS_SPEND_ALIASES),
        ):
            value = _money(_pick(row, aliases))
            if value is not None:
                projected[field] = value
        out.append(projected)
    return out


def _project_daily_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:_MAX_ROWS]:
        campaign_id = _campaign_id(_pick(row, _CAMPAIGN_ALIASES))
        if campaign_id is None:
            continue
        projected: dict[str, Any] = {"campaign_id": campaign_id}
        date = _text(_pick(row, _DATE_ALIASES))
        if date is not None:
            projected["date"] = date
        title = _text(_pick(row, _TITLE_ALIASES))
        if title is not None:
            projected["title"] = title
        for field, aliases in (
            ("impressions", _IMPRESSIONS_ALIASES),
            ("clicks", _CLICKS_ALIASES),
            ("orders", _ORDERS_ALIASES),
        ):
            value = _int_or(_pick(row, aliases), 0)
            projected[field] = value
        spend = _money(_pick(row, _SPEND_ALIASES))
        if spend is not None:
            projected["spend"] = spend
        revenue = _money(_pick(row, _REVENUE_ALIASES))
        if revenue is not None:
            projected["orders_revenue"] = revenue
        out.append(projected)
    return out


async def build_advertising_result(
    executor: Any,
    date_from: str,
    date_to: str,
    campaign_ids: list[int] | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Run the four strict Performance calls and build the closed result."""
    campaign_query: dict[str, Any] = {"page": page, "pageSize": page_size}
    if campaign_ids is not None:
        campaign_query["campaignIds"] = list(campaign_ids)[:_MAX_CAMPAIGN_IDS]
    stats_query: dict[str, Any] = {"dateFrom": date_from, "dateTo": date_to}
    if campaign_ids is not None:
        stats_query["campaignIds"] = list(campaign_ids)[:_MAX_CAMPAIGN_IDS]
    campaigns_data = await executor.performance(
        "ListCampaigns", query_params=campaign_query
    )
    reports_data = await executor.performance(
        "ListReports", query_params={"page": page, "pageSize": page_size}
    )
    expense_data = await executor.performance(
        "GetCampaignExpense", query_params=stats_query
    )
    daily_data = await executor.performance(
        "GetCampaignDailyStats", query_params=stats_query
    )

    campaigns = [
        {
            "id": _int_or(c.get("id"), 0),
            "title": _text(c.get("title")),
            "state": _text(c.get("state")),
            "type": _text(c.get("advObjectType")),
            "payment_type": _text(c.get("paymentType")),
            "daily_budget": _millionth_rubles(c.get("dailyBudget")),
            "total_budget": _millionth_rubles(c.get("budget")),
            "start_date": _text(c.get("fromDate")),
            "end_date": _text(c.get("toDate")),
        }
        for c in _as_mapping_list(campaigns_data.get("list"), _MAX_ROWS)
    ]
    wanted: set[int] | None = None
    if campaign_ids is not None:
        wanted = set(campaign_ids)
    campaigns = [c for c in campaigns if wanted is None or c["id"] in wanted]

    report_rows: list[dict[str, Any]] = []
    for r in _as_mapping_list(reports_data.get("items"), 500):
        meta = _walk_mapping(r.get("meta"))
        request = _walk_mapping(meta.get("request"))
        ids: list[int] = []
        for raw in _walk_list(request.get("campaigns"), _MAX_CAMPAIGN_IDS):
            normalized = _campaign_id(raw)
            if normalized is not None:
                ids.append(normalized)
        report_rows.append(
            {
                "name": _text(r.get("name")),
                "state": _text(meta.get("state")),
                "kind": _text(meta.get("kind")),
                "date_from": _text(request.get("dateFrom")),
                "date_to": _text(request.get("dateTo")),
                "grouping": _text(request.get("groupBy")),
                "campaign_ids": ids[:_MAX_CAMPAIGN_IDS],
            }
        )
    reports_total = _int_or(reports_data.get("total"), len(report_rows))

    spend_rows = _project_spend_rows(_flatten_stats_rows(expense_data))
    daily_rows = _project_daily_rows(_flatten_stats_rows(daily_data))

    totals = {
        "spend": 0.0,
        "subscription_spend": 0.0,
        "bonus_spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "orders": 0,
        "orders_revenue": 0.0,
    }
    for row in spend_rows:
        for field in ("subscription_spend", "bonus_spend"):
            value = row.get(field)
            if value is not None:
                totals[field] += value
    if spend_rows:
        spend_source = "expense"
        for row in spend_rows:
            value = row.get("spend")
            if value is not None:
                totals["spend"] += value
    else:
        spend_source = "daily"
        for row in daily_rows:
            value = row.get("spend")
            if value is not None:
                totals["spend"] += value
    for row in daily_rows:
        for field in ("impressions", "clicks", "orders"):
            totals[field] += row.get(field, 0)
        value = row.get("orders_revenue")
        if value is not None:
            totals["orders_revenue"] += value
    metrics = {
        "spend": round(totals["spend"], 2),
        "subscription_spend": round(totals["subscription_spend"], 2),
        "bonus_spend": round(totals["bonus_spend"], 2),
        "impressions": totals["impressions"],
        "clicks": totals["clicks"],
        "orders": totals["orders"],
        "orders_revenue": round(totals["orders_revenue"], 2),
    }

    truncated = (
        reports_total > len(report_rows)
        or len(report_rows) >= page_size
        or len(spend_rows) >= _MAX_ROWS
        or len(daily_rows) >= _MAX_ROWS
    )
    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "date_basis": "performance_expense_and_daily_statistics",
        "campaigns": campaigns[:_MAX_ROWS],
        "campaign_count": len(campaigns),
        "reports": report_rows,
        "report_count": len(report_rows),
        "spend_rows": spend_rows,
        "spend_rows_count": len(spend_rows),
        "daily_rows": daily_rows,
        "daily_rows_count": len(daily_rows),
        "metrics": metrics,
        "spend_source": spend_source,
        "completeness": "partial" if truncated else "complete",
        "truncation": (
            {"truncated": True, "reason": "row cap or page window reached"}
            if truncated
            else None
        ),
        "provenance": [
            "ListCampaigns",
            "ListReports",
            "GetCampaignExpense",
            "GetCampaignDailyStats",
        ],
    }
