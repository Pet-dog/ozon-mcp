"""FastMCP server factory."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import TextIOBase
from typing import TextIO, cast

import structlog
from mcp.server.fastmcp import FastMCP

from ozon_mcp import __version__
from ozon_mcp.config import Config
from ozon_mcp.knowledge import KnowledgeBase, load_knowledge
from ozon_mcp.schema import MethodGraph, SearchIndex, load_catalog
from ozon_mcp.tools import register_all
from ozon_mcp.transport.oauth import PerformanceTokenManager
from ozon_mcp.transport.performance import PerformanceClient
from ozon_mcp.transport.ratelimit import RateLimitRegistry
from ozon_mcp.transport.seller import SellerClient

log = structlog.get_logger()


class _DynamicStderr(TextIOBase):
    """File-like proxy that resolves `sys.stderr` at call time.

    Binding handlers to the current `sys.stderr` object captures that
    specific stream (e.g. pytest's temporary capsys stream), which is
    closed when the test ends — later writes then fail with
    "ValueError: I/O operation on closed file". Delegating on every call
    keeps logging pointed at whatever stderr is current while never
    touching stdout.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        try:
            return sys.stderr.isatty()
        except ValueError:  # stream already closed
            return False


_stderr_proxy = _DynamicStderr()


def create_server(config: Config | None = None) -> FastMCP:
    config = config or Config()
    _configure_logging(config.log_level)

    log.info("loading_catalog")
    catalog = load_catalog()
    log.info("catalog_loaded", total_methods=catalog.total_methods)

    log.info("loading_knowledge")
    knowledge = load_knowledge()

    # Apply curated description overrides BEFORE building the search index,
    # so BM25 has the new text to rank on.
    #
    # Policy: overwrite the swagger description whenever our curated text is
    # strictly longer (or swagger shipped an empty description). The original
    # "only when empty" rule skipped three hot-path methods whose swagger
    # description is present but a single-sentence stub — our curated
    # descriptions give search and describe_method significantly more signal.
    description_overrides_applied = 0
    for desc_override in knowledge.descriptions_overrides:
        method = catalog.get_by_operation_id(desc_override.operation_id)
        if method is None:
            continue
        current = (method.description or "").strip()
        override_text = desc_override.description.strip()
        if not current or len(override_text) > len(current):
            method.description = override_text
            description_overrides_applied += 1
    if description_overrides_applied:
        log.info(
            "description_overrides_applied",
            applied=description_overrides_applied,
        )

    log.info("building_search_index")
    search = SearchIndex(catalog)
    log.info("search_index_ready")
    log.info(
        "knowledge_loaded",
        workflows=len(knowledge.workflows),
        rate_limits=len(knowledge.rate_limits),
        errors=len(knowledge.errors),
        quirks=len(knowledge.quirks),
        examples=len(knowledge.examples),
        descriptions_overrides=len(knowledge.descriptions_overrides),
    )

    # Apply curated safety overrides from knowledge YAML over heuristic results.
    overrides_applied = 0
    for override in knowledge.safety_overrides:
        method = catalog.get_by_operation_id(override.operation_id)
        if method is not None and method.safety != override.safety:
            log.info(
                "safety_override_applied",
                operation_id=override.operation_id,
                was=method.safety,
                now=override.safety,
                reason=override.reason,
            )
            method.safety = override.safety
            method.safety_reason = f"curated override: {override.reason}"
            overrides_applied += 1
    if overrides_applied:
        log.info("safety_overrides_total", applied=overrides_applied)

    log.info("building_method_graph")
    graph = MethodGraph(catalog)
    workflow_chains = [
        [step.operation_id for step in wf.steps] for wf in knowledge.workflows
    ]
    graph.add_workflow_edges(workflow_chains)
    log.info("method_graph_ready", nodes=graph.node_count, edges=graph.edge_count)

    seller_client, performance_client = _maybe_build_clients(config, knowledge)
    # Unwrap the optional HMAC secret once, here, and pass it only to
    # register_all/analytics. Never log or embed it anywhere else.
    hmac_secret = config.analytics_hmac_secret_value()
    log.info(
        "analytics_layer",
        seller=seller_client is not None,
        performance=performance_client is not None,
        hmac_configured=hmac_secret is not None,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if seller_client is not None:
                try:
                    await seller_client.aclose()
                except Exception as e:
                    log.warning("seller_client_close_failed", error=str(e))
            if performance_client is not None:
                try:
                    await performance_client.aclose()
                except Exception as e:
                    log.warning("performance_client_close_failed", error=str(e))

    mcp = FastMCP(
        name="ozon-mcp",
        instructions=(
            f"PetDog/Ozon strict read-only analytics server v{__version__}. "
            f"Indexes {catalog.total_methods} methods across Seller and Performance APIs, "
            f"with {len(knowledge.workflows)} curated workflows and {len(knowledge.quirks)} method quirks. "
            "This is a strict read-only Ozon source: it never creates, updates, "
            "cancels, deletes, or otherwise mutates anything on Ozon, and offers "
            "no generic method execution, no raw provider responses, and no "
            "write-confirmation bypass. Offline catalog tools (discovery, graph, "
            "workflow, reference) describe API methods without touching the "
            "network. Only the ten contract-bound PetDog analytics tools may "
            "call Ozon, each restricted to an exact read-only operation "
            "allowlist and returning closed, sanitized projections. Start with "
            "ozon_list_sections or ozon_list_workflows to explore the offline "
            "catalog, or call an analytics tool directly — it reports its own "
            "missing-credential/configuration error when credentials are absent."
        ),
        lifespan=lifespan,
    )
    register_all(
        mcp,
        catalog,
        search,
        graph,
        knowledge,
        seller_client=seller_client,
        performance_client=performance_client,
        hmac_secret=hmac_secret,
    )
    log.info("server_ready", version=__version__)
    return mcp


def _maybe_build_clients(
    config: Config, knowledge: KnowledgeBase
) -> tuple[SellerClient | None, PerformanceClient | None]:
    rate_limits = RateLimitRegistry(knowledge)

    seller: SellerClient | None = None
    if config.has_seller_credentials():
        seller = SellerClient(
            config.seller_client_id(),
            config.seller_api_key(),
            rate_limits=rate_limits,
        )

    performance: PerformanceClient | None = None
    if config.has_performance_credentials():
        token_manager = PerformanceTokenManager(
            config.perf_client_id(),
            config.perf_client_secret(),
        )
        performance = PerformanceClient(
            token_manager,
            rate_limits=rate_limits,
        )

    return seller, performance


def _configure_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    # MCP stdio protocol owns stdout — all logs MUST go to stderr. Pin both
    # stdlib logging and structlog to an explicit stderr stream so no handler
    # or logger factory can ever fall back to stdout.
    stderr_handler = logging.StreamHandler(_stderr_proxy)
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [stderr_handler]
    root.setLevel(log_level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.WriteLoggerFactory(file=cast(TextIO, _stderr_proxy)),
    )
