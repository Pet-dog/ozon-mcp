# Decisions

## 2026-08-22 — Fork and narrow instead of replacing the server

Decision: retain the upstream schema/catalog and transport foundations, but
replace the public execution boundary with an exact read-only allowlist and
PetDog-specific projections.

Basis: the base already loads both Ozon OpenAPI snapshots, authenticates Seller
and Performance APIs, retries bounded transient failures, and implements MCP
stdio. The failing behavior is the public authority surface: mutation remains
reachable with confirmation flags and raw provider responses can escape.

Refutable by: a measured implementation showing that removing the mutation
surface requires more code or leaves more unreachable behavior than a fresh
read-only server with the same accepted workflows.

## 2026-08-22 — Keep Ozon as a separate SEO-SXO channel

Decision: every result is labeled `source_channel: ozon`; RetailCRM comparison
is performed by the upper SEO-SXO layer using anonymized join keys.

Basis: the owner confirmed that Ozon orders also enter RetailCRM. Summing both
sources would double-count matched sales, while merging Ozon into petdog.ru
would corrupt website attribution.

Refutable by: current primary data proving Ozon orders no longer enter
RetailCRM or that a canonical cross-source transaction id safely identifies a
different deduplication rule.

## 2026-08-22 — Preserve MCP v1 until a deliberate v2 migration

Decision: bind the dependency to a compatible MCP v1 range for this delivery.

Basis: upstream imports `mcp.server.fastmcp` through the v1 API, while the
official Python SDK now publishes v2 as the current stable major. The base
constraint `mcp>=1.2.0` permits an unreviewed major-version change.

Refutable by: a separate accepted migration proving all tools, annotations,
stdio framing, clients, and deployment wiring against MCP v2.

## 2026-08-22 — Stale general specs fail closed; accepted tools are contract-bound

Decision: dynamic spec-dependent execution is unavailable when metadata is
missing, malformed, hash-mismatched, or older than the freshness bound.
PetDog workflow tools may use only release-pinned allowlisted contracts with
response-shape canaries and must disclose stale snapshot status.

Basis: the base snapshot was observed as refreshed on 2026-04-16, the parser
named by the README is not present in the public repository, and the existing
CI job is non-blocking and reads a metadata shape that does not match the file.
Changing the timestamp without new source bytes would fabricate freshness.

Refutable by: current official Ozon OpenAPI bytes with reproducible provenance,
matching hashes, and a refresh procedure that runs in this repository.

## 2026-08-22 — Performance reports are read, not generated, in the first release

Decision: use existing GET/list/download statistics endpoints and exclude
report-generation, campaign, product, and bid mutations from the public tools.

Basis: generating a report creates external provider state even if it does not
alter campaign delivery. The required first production canary must be strictly
read-only.

Refutable by: an official Ozon contract and owner decision classifying a
specific report-generation operation as an admitted read-only provider action.

## 2026-08-22 — All dynamic Ozon calls fail closed on unusable evidence

Decision: supersedes the earlier stale-spec exception for contract-bound
tools. When the bundled snapshot evidence is missing, malformed,
hash-mismatched, or older than the freshness bound, every dynamic Ozon
network call fails closed — including PetDog workflow tools that were
previously permitted to continue with release-pinned contracts and stale
disclosure.

Basis: one uniform preflight over shared evidence is simpler and safer
than maintaining a per-tool stale bypass; a stale contract cannot
guarantee the endpoint still exists or returns the pinned shape, so
proceeding would encode unsupported endpoint assumptions.

Refutable by: an accepted release-pinned canary contract plus a fresh
official snapshot process that together restore verifiable evidence.

## 2026-08-22 — Advertising spend from expense rows; daily spend only as fallback

Decision: aggregate advertising spend is taken from `GetCampaignExpense`
expense rows. `GetCampaignDailyStats` daily spend is used only as a
fallback when expense rows are unavailable, never summed together.
Daily stats remain the source for impressions, clicks, orders, and
revenue.

Basis: expense rows and daily spend overlap in what they measure;
combining both would double-count advertising spend.

Refutable by: official or live response evidence showing the two
endpoints have non-overlapping spend semantics that both must be
included.

## 2026-08-22 — Pinned stale contracts continue with explicit evidence

Decision: supersedes only the stale-age clause of “All dynamic Ozon calls
fail closed on unusable evidence.” A stale observation timestamp remains
usable only for the exact 12 pinned read-only operations with closed
projections and response-shape tests. Missing or malformed evidence, an
unreadable bundled file, or any hash mismatch still blocks before provider
request construction.

Basis: runtime content-hash binding proves that the bundled bytes are exactly
the pinned contract bytes, while age alone does not alter those bytes. This is
the exception already defined by acceptance criterion 10; every result must
disclose the honest stale state.

Refutable by: live divergence of any pinned endpoint or response shape, or any
content-hash mismatch.
