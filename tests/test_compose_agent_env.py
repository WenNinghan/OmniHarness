"""Agent bootstrap contracts independent of production deployment data."""

from types import SimpleNamespace

import omniharness.checkpoint_fencing as fencing
import omniharness.cli as cli
import omniharness.services as services


def test_cli_bootstrap_initializes_checkpoint_only_with_explicit_agent_opt_in(monkeypatch):
    calls = []
    current = SimpleNamespace(
        database_url="postgresql+psycopg://test-user@127.0.0.1/test-db",
        checkpoint_schema="test_checkpoint_schema",
        enable_agent=False,
    )

    async def fake_initialize(url, *, schema):
        calls.append((url, schema))

    monkeypatch.setattr(cli, "Settings", lambda: current)
    monkeypatch.setattr(cli, "init_secrets", lambda _: calls.append("secrets"))
    monkeypatch.setattr(cli, "migrate", lambda _: calls.append("migrate"))
    monkeypatch.setattr(fencing, "initialize_checkpoint_schema", fake_initialize)
    monkeypatch.setattr("sys.argv", ["omniharness", "bootstrap"])
    for name in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true")

    cli.main()
    assert calls == ["secrets", "migrate"]
    current.enable_agent = True
    cli.main()
    assert calls[-3:-1] == ["secrets", "migrate"]
    assert calls[-1] == ("postgresql+psycopg://test-user@127.0.0.1/test-db", "test_checkpoint_schema")


def test_bootstrap_opt_in_does_not_bypass_runtime_gate_or_sqlite_boundary(environment, monkeypatch):
    settings, _, _, client = environment
    settings.enable_agent = True
    monkeypatch.setattr(services, "AGENT_RUNTIME_WIRED", False)
    assert client.get("/api/v1/bootstrap").json()["agent_available"] is False
    # Even if the source-level runtime gate is enabled for a future version,
    # SQLite remains unsupported for persistent Agent execution.
    monkeypatch.setattr(services, "AGENT_RUNTIME_WIRED", True)
    assert client.get("/api/v1/bootstrap").json()["agent_available"] is False
