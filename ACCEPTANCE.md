# PetDog read-only Ozon analytics acceptance

Status: binding for the `ozon-readonly-sxo-20260822` workstream.

Exact base: `7e1bea2dd7510064311f1d357d014bb012a18847`.

Existing-criteria preflight: the base repository contained no `AGENTS.md`,
`CLAUDE.md`, `AUTHORITY.md`, acceptance file, or decision record. The upstream
tests cover the general MCP but do not establish this PetDog read-only boundary.

## Required outcome

1. The MCP registers only offline documentation/discovery tools and Ozon API
   tools whose external effect is read-only. No tool argument, environment
   switch, or hidden generic route can enable create, update, activate,
   deactivate, cancel, delete, approve, reject, import, ship, or other mutation.
2. Every network-capable tool is annotated `readOnlyHint: true`,
   `destructiveHint: false`, and rejects an operation outside one source-owned
   exact allowlist before an HTTP request is constructed.
3. The generic execution surface, if retained, exposes neither
   `confirm_write` nor any equivalent bypass. Pagination may call only the same
   allowlisted operation and has bounded item/page/output limits plus an honest
   completeness/truncation result.
4. Raw Ozon responses are not returned from the public analytics tools. Their
   closed projections exclude names, phones, email addresses, delivery and
   billing addresses, comments, credentials, authorization headers, arbitrary
   custom fields, and upstream error payloads. Errors expose only a local
   category, HTTP status, operation id, retryability, and a sanitized message.
5. The public analytics surface provides bounded PetDog workflows for:
   sales/postings and revenue; finance, commissions, logistics and returns;
   stocks and shortage risk; prices and promotions; advertising spend and
   campaign performance; per-SKU performance; catalog mapping; and an
   anonymized RetailCRM reconciliation projection.
6. Every commerce result declares `source_channel: "ozon"`, date basis,
   requested period, timezone, pagination/completeness, and data provenance.
   Ozon revenue is never labeled as petdog.ru website revenue.
7. Reconciliation emits only a deterministic HMAC-SHA256 join key derived from
   a normalized Ozon posting/order identifier with an injected secret. It does
   not call RetailCRM and does not expose the source identifier. SKU/offer ids
   may remain visible because they are catalog identifiers, not customer data.
8. Because Ozon orders also enter RetailCRM, the contract gives the upper
   SEO-SXO layer enough evidence to classify `ozon-only`, `retailcrm-only`,
   `matched`, `amount-mismatch`, and `duplicate`, while counting a matched order
   once. The Ozon MCP itself does not merge channels or calculate PetDog DRR.
9. Server stdout is reserved for MCP framing. Application and transport logs go
   to stderr. A subprocess smoke test must fail if any non-protocol startup text
   appears on stdout.
10. The bundled API snapshot has content hashes and an observation timestamp.
    A stale or malformed snapshot blocks all dynamic spec-dependent execution.
    Contract-bound PetDog tools may continue only for exact allowlisted
    operations whose endpoint and response-shape canaries are carried by the
    release; their output must disclose the stale snapshot state.
11. Runtime dependency resolution is reproducible. Until an explicit MCP v2
    migration is accepted, the package pins the compatible MCP major version
    instead of allowing an unreviewed `mcp>=2` upgrade.
12. One live production canary uses read-only credentials and exercises one
    Seller identity/status call plus one bounded analytics call. It records no
    customer rows, makes no provider mutation, and proves the active runtime is
    built from the merged commit. A failed canary rolls registration back to the
    previous runtime.

## Required checks

- A mutation test replaces an allowlisted operation with a known write or
  destructive operation and proves rejection happens before the fake transport
  is called.
- A PII mutation test injects representative personal fields into an upstream
  fixture and proves no value or key reaches the public result or error.
- A duplicate/reload test gives the reconciliation projection the same Ozon
  order twice and proves one join key and an explicit duplicate count.
- A stale-metadata test advances the clock past the freshness bound and proves
  dynamic execution is blocked; an exact contract-bound tool remains usable
  only with `spec_stale: true` in its evidence.
- A stdout mutation test injects a startup `print()` and proves the protocol
  smoke check fails.
- Focused tests, changed-file lint/type checks, one final repository suite,
  PyCharm inspection with a bound interpreter, mandatory read-only Devin
  review, and native GitHub checks all pass on the exact candidate.

## Out of scope

- Changing products, prices, stocks, campaigns, bids, promotions, postings,
  returns, warehouse state, or any other Ozon data.
- Reading or returning customer identity, contact, address, comment, or payment
  details.
- Calling RetailCRM from this repository or treating RetailCRM as the Ozon data
  source.
- Mixing Ozon, website, phone, chat, or another marketplace into one revenue
  number inside this MCP.
- Changes to petdog.ru, Yandex Direct, Metrica, Plerdy, or the CRM.
- Automatic deployment triggered by importing upstream commits.

## Implementation divergence / status — 2026-08-22

The original criteria above are preserved verbatim. This section records
where the delivered implementation diverges in strength, not in intent.

- **Criterion 3 — resolved by deletion, not flag removal.** The wording
  anticipated a retained generic execution surface with pagination. The
  implementation deleted the generic executor (`ozon_call_method` /
  `ozon_fetch_all`) entirely, so there is no generic surface left to
  guard and no pagination walker reaching beyond fixed allowlisted
  operations. This closes the criterion rather than satisfying it
  partially.
- **Criterion 10 — implemented as written.** The runtime verifies the actual
  content hashes of both bundled specification files. Missing, malformed,
  unreadable, or hash-mismatched evidence blocks every dynamic provider call
  before request construction. Honestly stale evidence remains nonblocking
  only for the exact 12 internally pinned allowlisted operations, with
  explicit stale disclosure in their results.

Criterion 3 is resolved more strongly by deletion of the generic surface;
Criterion 10 required no divergence from the original acceptance contract.

## Finance completeness amendment — 2026-09-06

Measured RED on base `158ba79`:
`ozon_finance_analytics(page_size=1000)` accepted the request, the provider
returned 100 operations while `row_count=985` and `page_count` was absent,
and the tool still published `completeness=complete`. Two defects combined:
the tool's `page_size` bound (1..1000) exceeded the provider's silent
100-operations-per-page cap, and completeness was derived from
`page_count` defaulting to 1 when the provider omitted it.

Binding additions (criteria above preserved verbatim):

- AC-OZ-FIN-01: `page_size` outside 1..100 must return the existing closed
  `invalid_params` envelope before the executor is called. Values 1..100
  preserve the existing public request and response shape.
- AC-OZ-FIN-02: `completeness` is `complete` only when usable provider
  evidence proves the returned projection contains the whole requested
  result set: `row_count` equals fetched operations or `page_count` proves
  no later pages, and no reported total contradicts the fetched page or the
  requested page. Both totals absent or unintelligible (even on a short
  page), `row_count` above or below the fetched count, `page_count` later
  than or behind the requested page, `page > 1`, or an ambiguous full page
  without provider totals must produce `partial` plus truthful truncation
  evidence. Settlement rows must never be invented or duplicated.
- AC-OZ-FIN-03: the read-only operation, date validation, money
  aggregation, sanitization, retries, timeout behavior, and closed
  provider-error handling are unchanged.
- AC-OZ-FIN-04: finance knowledge entries advertising `page_size=1000`
  are corrected in place; no second workflow or pagination subsystem is
  introduced.
- AC-OZ-FIN-05: focused deterministic tests pin the measured 100/985
  regression, fail-before-request for `page_size=1000`, and an actually
  complete single page.

