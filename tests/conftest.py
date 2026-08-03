import pytest


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Tests never read the developer's .env.

    `app.main`, `app.cli` and `app.mcp_server` all call `load_dotenv()` at
    import time. A test that clears an env var and *then* imports one of them
    gets the variable put straight back, because dotenv only skips keys already
    present and a deleted key is not present. The test then runs against
    whatever happens to be in the developer's .env.

    That is not theoretical. `test_sessions_are_isolated_end_to_end_over_api_chat`
    deleted OPENAI_API_KEY, annotated it "deterministic planner", and then
    silently ran the LLM planner and made a real network call -- on a machine
    with a .env. On a fresh clone, with no .env, the same test failed. A test
    whose result depends on an untracked file is not a test.

    Neutralised at the source module so every importer is covered at once.
    """

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for module in ("app.main", "app.cli", "app.mcp_server"):
        mod = __import__(module, fromlist=["load_dotenv"])
        if hasattr(mod, "load_dotenv"):
            monkeypatch.setattr(mod, "load_dotenv", lambda *a, **k: False)


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
