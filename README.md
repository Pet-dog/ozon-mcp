# ozon-mcp (Pet-dog strict read-only fork)

> Strict read-only Ozon Seller & Performance analytics MCP server for the
> PetDog SEO-SXO pipeline. Fork of [PCDCK/ozon-mcp](https://github.com/PCDCK/ozon-mcp).

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-v1-orange)

## What this is

An MCP (Model Context Protocol) stdio server that exposes a fixed, literal
allowlist of Ozon read operations as closed, PII-sanitized projections,
plus offline API discovery/reference tools to help agents reason about the
Ozon API surface.

## Non-negotiable read-only boundary

- Exactly **12 literal allowlisted read-only operations** (see
  `src/ozon_mcp/readonly.py`). Anything not on the list is refused before
  an HTTP request is constructed — no inference from HTTP method or
  upstream classification.
- **No generic call/fetch executor.** The upstream `ozon_call_method` /
  `ozon_fetch_all` tools were removed. No tool accepts an arbitrary
  operation id, path, or method name.
- **No report generation** and no other provider-side state creation.
- **No mutation flags.** There is no `confirm_write`,
  `i_understand_this_modifies_data`, or any equivalent bypass argument
  anywhere in the tool surface.
- **Closed PII-sanitized projections.** Raw Ozon responses are never
  returned by the analytics tools. Projections exclude names, phones,
  email addresses, delivery/billing addresses, comments, credentials,
  authorization headers, arbitrary custom fields, and upstream error
  payloads. Errors expose only a local category, HTTP status, operation
  id, retryability, and a sanitized message.
- **stderr for logs, stdout for MCP framing.** Application and transport
  logging goes to stderr; stdout is reserved exclusively for the MCP
  stdio protocol.

Every network-capable tool is annotated `readOnlyHint: true` and
`destructiveHint: false`.

## Tools (21 registered)

### Offline discovery / reference / workflow tools (11)

These never touch the network:

- `ozon_list_sections` — API section overview with method counts
- `ozon_search_methods` — BM25 search (Russian + English) with filters
- `ozon_describe_method` — full method spec with resolved JSON Schema
- `ozon_get_section` — methods inside a section
- `ozon_get_related_methods` — method-graph neighbours
- `ozon_list_workflows` — curated workflow overview
- `ozon_get_workflow` — full step-by-step workflow plan
- `ozon_get_rate_limits` — per-method/per-section rate limits
- `ozon_get_error_catalog` — error codes and fixes
- `ozon_get_examples` — request payload examples
- `ozon_get_swagger_meta` — bundled snapshot metadata and hashes

### PetDog analytics tools (10)

All network analytics goes through these fixed workflows over the
12-operation allowlist, each returning a closed projection:

- `ozon_seller_status` — Seller identity/subscription status
  (`SellerAPI_SellerInfo`).
- `ozon_sales_analytics` — sales/postings and revenue over a bounded
  period (`PostingAPI_GetFboPostingList`, `PostingAPI_GetFbsPostingListV3`).
- `ozon_finance_analytics` — finance, commissions, logistics
  (`FinanceAPI_FinanceTransactionListV3`).
- `ozon_returns_analytics` — returns (`returnsList`).
- `ozon_inventory_analytics` — stocks and shortage risk
  (`ProductAPI_GetProductList`, `ProductAPI_GetProductInfoStocks`).
- `ozon_price_promotion_analytics` — prices and promotions
  (`ProductAPI_GetProductInfoPrices`).
- `ozon_advertising_analytics` — advertising **expense + daily stats**
  (`ListCampaigns`, `ListReports`, `GetCampaignExpense`,
  `GetCampaignDailyStats`).
- `ozon_sku_performance` — per-SKU performance across the above sources.
- `ozon_catalog_mapping` — SKU/offer catalog mapping
  (`ProductAPI_GetProductList`).
- `ozon_retailcrm_projection` — anonymized RetailCRM reconciliation
  projection with HMAC join keys (no RetailCRM calls).

## Advertising spend semantics

Aggregate advertising spend is authoritative from **`GetCampaignExpense`**
(expense rows). `GetCampaignDailyStats` daily spend is a **fallback only**,
used when expense rows are unavailable — never both, which prevents double
counting. Daily stats remain the source for impressions, clicks, orders,
and revenue. **No DRR is calculated in this server**; that belongs to the
upper SEO-SXO layer.

## Channel attribution

Ozon is a separate `source_channel: "ozon"`. Ozon orders that also enter
RetailCRM must be reconciled by their HMAC `join_key` in the upper
SEO-SXO layer — never summed twice. This MCP does not merge channels or
compute combined revenue.

Every commerce result declares `source_channel: "ozon"`, date basis,
requested period, timezone, pagination/completeness, and data provenance.
Ozon revenue is never labeled as petdog.ru website revenue.

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Pet-dog/ozon-mcp.git
cd ozon-mcp
uv sync --frozen
```

Run:

```bash
uv run ozon-mcp
```

The server speaks MCP over stdio.

## Environment variables

Set credentials via environment variables (names only — never commit
secret values to files):

- `OZON_CLIENT_ID` — Seller API client id
- `OZON_API_KEY` — Seller API key
- `OZON_PERFORMANCE_CLIENT_ID` — Performance API client id
- `OZON_PERFORMANCE_CLIENT_SECRET` — Performance API client secret
- `OZON_ANALYTICS_HMAC_SECRET` — secret for reconciliation join keys
- `OZON_LOG_LEVEL` — log level for stderr logging

## MCP client configuration (generic example)

Use placeholders; do not check credentials into config files under version
control:

```json
{
  "mcpServers": {
    "ozon": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/ozon-mcp", "run", "ozon-mcp"],
      "env": {
        "OZON_CLIENT_ID": "<your-seller-client-id>",
        "OZON_API_KEY": "<your-seller-api-key>",
        "OZON_PERFORMANCE_CLIENT_ID": "<your-performance-client-id>",
        "OZON_PERFORMANCE_CLIENT_SECRET": "<your-performance-client-secret>",
        "OZON_ANALYTICS_HMAC_SECRET": "<your-hmac-secret>",
        "OZON_LOG_LEVEL": "<log-level>"
      }
    }
  }
}
```

## Bundled spec evidence

The bundled Ozon OpenAPI snapshots carry content hashes and an
observation timestamp:

- Observed: `2026-04-16T21:47:53Z`
- Seller spec SHA-256:
  `c54962e9481ac776e14c0fe4f987e0ff74fde68a793e6b640f70db6cdaabdba5`
- Performance spec SHA-256:
  `27ad70709fdcc6323c4ba9d7d9af20e21a39397023e2776a6c171ab08aa05a06`

The server verifies the SHA-256 of both actual bundled files against this
metadata at runtime. Missing or malformed evidence, an unreadable file, or
a hash mismatch blocks execution before a provider request is constructed.

This April 2026 snapshot is honestly stale at the current date; it is not
treated as fresh and is not updated automatically. Verified-but-stale evidence
remains usable only for the exact 12 pinned read-only operations, and every
result discloses `spec_snapshot.stale: true`. A live response-shape canary is
required after deployment to confirm that those pinned contracts still match
the live API before relying on them in production.

## Tests

Bounded test commands:

```bash
uv run pytest tests/unit tests/golden tests/integration
uv run ruff check src tests
uv run mypy src
```

Live tests (`tests/live`, marker `live`) require real credentials and are
excluded from default discovery.

## Current limitations

- Read-only by design: no write, mutation, or report-generation surface.
- The analytics surface is scoped to PetDog workflows; it is not a
  general-purpose Ozon API client.
- Dynamic provider execution is blocked when snapshot evidence is missing,
  malformed, unreadable, or hash-mismatched. Verified-but-stale evidence
  continues only for the exact 12 pinned read-only operations, with
  `spec_snapshot.stale: true` in every result; there is no route beyond that
  pinned set.
- DRR and cross-channel reconciliation happen in the upper SEO-SXO layer,
  not here.
- Ozon runtime can be stricter than the swagger declares; projections
  treat upstream fields as nullable where known quirks exist.

## Attribution & license

Forked from [PCDCK/ozon-mcp](https://github.com/PCDCK/ozon-mcp)
(upstream v0.6.0). MIT license — see [LICENSE](LICENSE).
