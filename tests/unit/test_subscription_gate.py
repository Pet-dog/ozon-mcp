"""Subscription tier gate unit tests.

Covers the pure helper `tier_sufficient` and the offline discovery
serialization of subscription knowledge. Uses only the real knowledge
YAML bundled with the package; no network access.
"""

from __future__ import annotations

from ozon_mcp.tools.subscription import tier_sufficient

# ── pure helper: tier_sufficient ─────────────────────────────────────────────


def test_equal_tier_is_sufficient() -> None:
    assert tier_sufficient("PREMIUM_PRO", "PREMIUM_PRO") is True


def test_lower_tier_is_not_sufficient() -> None:
    assert tier_sufficient("PREMIUM_PLUS", "PREMIUM_PRO") is False


def test_higher_tier_is_sufficient() -> None:
    assert tier_sufficient("PREMIUM_PRO", "PREMIUM_PLUS") is True


def test_unknown_cabinet_tier_allowed() -> None:
    # No cabinet info → let Ozon decide.
    assert tier_sufficient(None, "PREMIUM_PRO") is True


def test_unknown_required_tier_allowed() -> None:
    # No curated requirement → don't gate.
    assert tier_sufficient("PREMIUM_PLUS", None) is True
    assert tier_sufficient("PREMIUM_PLUS", "unknown") is True


def test_unrecognised_tier_names_do_not_block() -> None:
    # Future-proofing: if Ozon invents a new tier we don't model,
    # fall back to permissive.
    assert tier_sufficient("GALAXY_BRAIN", "PREMIUM_PRO") is True
    assert tier_sufficient("PREMIUM_PRO", "ULTRA") is True


def test_premium_lite_alias_equals_lite() -> None:
    # seller/info returns PREMIUM_LITE; we model LITE as the same slot.
    assert tier_sufficient("PREMIUM_LITE", "LITE") is True
    assert tier_sufficient("PREMIUM_LITE", "PREMIUM") is False


def test_tier_names_are_case_insensitive() -> None:
    assert tier_sufficient("premium_pro", "PREMIUM_PLUS") is True


# ── offline discovery: subscription serialization ───────────────────────────


async def test_describe_surfaces_pre_check_available_true_for_curated() -> None:
    """ozon_describe_method must signal pre_check_available=True for
    methods with a concrete curated subscription requirement."""
    from ozon_mcp.knowledge.loader import load_knowledge
    from ozon_mcp.schema import MethodGraph, load_catalog
    from ozon_mcp.tools.discovery import _serialize_method

    catalog = load_catalog()
    graph = MethodGraph(catalog)
    knowledge = load_knowledge()

    method = catalog.get_by_operation_id("ProductPricesDetails")
    assert method is not None
    serialised = _serialize_method(method, graph, knowledge)
    sub = serialised.get("subscription")
    assert sub is not None
    assert sub["required"] == "PREMIUM_PRO"
    assert sub["pre_check_available"] is True


async def test_describe_marks_pre_check_unavailable_for_unknown() -> None:
    """When required_tier is 'unknown' the agent cannot pre-check."""
    from ozon_mcp.knowledge.loader import load_knowledge
    from ozon_mcp.schema import MethodGraph, load_catalog
    from ozon_mcp.tools.discovery import _serialize_method

    catalog = load_catalog()
    graph = MethodGraph(catalog)
    knowledge = load_knowledge()

    method = catalog.get_by_operation_id("ProductAPI_GetProductRatingBySku")
    assert method is not None
    serialised = _serialize_method(method, graph, knowledge)
    sub = serialised.get("subscription")
    assert sub is not None
    assert sub["required"] == "unknown"
    assert sub["pre_check_available"] is False
