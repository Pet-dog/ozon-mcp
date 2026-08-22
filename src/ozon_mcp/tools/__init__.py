"""MCP tool registration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ozon_mcp.knowledge import KnowledgeBase
from ozon_mcp.schema import Catalog, MethodGraph, SearchIndex
from ozon_mcp.tools import analytics, discovery, reference, workflow
from ozon_mcp.tools import graph as graph_tool
from ozon_mcp.transport.performance import PerformanceClient
from ozon_mcp.transport.seller import SellerClient


def register_all(
    mcp: FastMCP,
    catalog: Catalog,
    search: SearchIndex,
    graph: MethodGraph,
    knowledge: KnowledgeBase,
    *,
    seller_client: SellerClient | None = None,
    performance_client: PerformanceClient | None = None,
    hmac_secret: str | None = None,
) -> None:
    discovery.register(mcp, catalog, search, graph=graph, knowledge=knowledge)
    graph_tool.register(mcp, catalog, graph)
    workflow.register(mcp, knowledge)
    reference.register(mcp, catalog, knowledge)
    # Always register the read-only analytics surface: the tools are visible
    # even without credentials and return their own closed
    # missing-credential/config errors instead of hiding from discovery.
    analytics.register(
        mcp,
        catalog,
        seller_client,
        performance_client,
        hmac_secret=hmac_secret,
    )
