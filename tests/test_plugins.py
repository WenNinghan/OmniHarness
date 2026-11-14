from types import SimpleNamespace

from omniharness import plugins
from omniharness.plugins import PluginManifest


class Point:
    def __init__(self, name, factory):
        self.name, self.factory = name, factory

    def load(self):
        return self.factory


def test_discovery_reports_compatible_and_incompatible_without_executing_tools(monkeypatch):
    executions = []

    class Tool:
        manifest = PluginManifest(id="example.reader", name="Reader", version="1.0", kind="tool")

        async def execute(self, arguments, context):
            executions.append(arguments)

    incompatible = SimpleNamespace(manifest=PluginManifest(
        id="example.future", name="Future", version="1.0", kind="tool", api_version="999",
    ))

    def entry_points(*, group):
        assert group == "omniharness.plugins"
        return [Point("reader", Tool), Point("future", lambda: incompatible)]

    monkeypatch.setattr(plugins, "entry_points", entry_points)
    results = plugins.discover_plugins()
    assert results[0]["id"] == "example.reader" and results[0]["status"] == "available"
    assert results[1]["status"] == "incompatible"
    assert executions == []


def test_bad_plugin_does_not_break_discovery_or_leak_import_exception(monkeypatch):
    secret = "test-only-plugin-secret"

    def broken_factory():
        raise RuntimeError(f"Could not connect with api_key={secret}")

    valid = SimpleNamespace(manifest=PluginManifest(
        id="example.verifier", name="Verifier", version="1.0", kind="verifier",
    ))
    monkeypatch.setattr(plugins, "entry_points", lambda **_: [
        Point("broken", broken_factory), Point("healthy", lambda: valid),
    ])
    results = plugins.discover_plugins()
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "available"
    assert secret not in repr(results)


def test_plugin_with_untyped_manifest_is_rejected(monkeypatch):
    untyped = SimpleNamespace(manifest={"api_version": "1", "id": "pretend-compatible"})
    monkeypatch.setattr(plugins, "entry_points", lambda **_: [Point("untyped", lambda: untyped)])
    result = plugins.discover_plugins()
    assert result[0]["status"] == "incompatible"
