import pytest


@pytest.fixture(autouse=True)
def _reset_mcp_shared_client():
    """The MCP server memoises one client per PROCESS, which is right in
    production and wrong across tests: a client built under one test's
    monkeypatched CTGovClient would survive into the next. Cleared both sides
    so each test builds its own."""
    from app import mcp_server

    mcp_server._SHARED.clear()
    yield
    mcp_server._SHARED.clear()
