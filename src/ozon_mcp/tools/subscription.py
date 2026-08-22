"""Pure offline subscription tier utility helpers.

This module intentionally contains no MCP tool registrations, no network
imports, no SellerInfo calls, and no error translation: it is a pure
offline tier-comparison utility used by unit/knowledge tests. The former
dormant ``register()`` and its network-capable tools were removed so no
hidden request path remains.
"""

from __future__ import annotations

# Ascending order: every tier implicitly grants access to everything below it.
TIER_HIERARCHY: list[str] = [
    "LITE",
    "STANDARD",
    "PREMIUM",
    "PREMIUM_PLUS",
    "PREMIUM_PRO",
]

TIER_ALIASES: dict[str, str] = {
    "PREMIUM_LITE": "LITE",
}


def _normalize_tier(tier: str | None) -> str | None:
    if tier is None:
        return None
    upper = tier.upper()
    return TIER_ALIASES.get(upper, upper)


def tier_sufficient(cabinet_tier: str | None, required_tier: str | None) -> bool:
    """Return True if ``cabinet_tier`` meets or exceeds ``required_tier``."""
    if required_tier is None or str(required_tier).lower() == "unknown":
        return True
    if cabinet_tier is None:
        return True
    cab = _normalize_tier(cabinet_tier)
    req = _normalize_tier(required_tier)
    if cab is None or req is None:
        return True
    try:
        return TIER_HIERARCHY.index(cab) >= TIER_HIERARCHY.index(req)
    except ValueError:
        return True
