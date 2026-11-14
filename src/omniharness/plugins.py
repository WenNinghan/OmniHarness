"""Trusted Python extension contracts; discovery never installs remote code."""

from dataclasses import asdict, dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol

API_VERSION = "1"


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    kind: str
    api_version: str = API_VERSION
    permissions: tuple[str, ...] = ()
    side_effect: str = "none"
    config_schema: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    run_id: str
    workspace_id: str | None = None
    permissions: tuple[str, ...] = ()


class ToolPlugin(Protocol):
    manifest: PluginManifest

    async def execute(self, arguments: dict, context: ToolContext) -> dict: ...


class VerifierPlugin(Protocol):
    manifest: PluginManifest

    async def verify(self, artifact: dict, requirements: dict) -> dict: ...


class ProviderAdapter(Protocol):
    manifest: PluginManifest

    def stream_chat(self, config: Any, messages: list[dict]): ...


class WorkflowFactory(Protocol):
    manifest: PluginManifest

    def build(self, config: dict, context: Any): ...


class SearchProvider(Protocol):
    manifest: PluginManifest

    async def search(self, query: str, options: dict) -> list[dict]: ...


def discover_plugins() -> list[dict]:
    result = []
    for point in entry_points(group="omniharness.plugins"):
        try:
            plugin = point.load()()
            manifest = plugin.manifest
            if not isinstance(manifest, PluginManifest) or manifest.api_version != API_VERSION:
                result.append({"id": point.name, "status": "incompatible", "message": "Plugin API version mismatch"})
                continue
            result.append({**asdict(manifest), "status": "available", "trusted": True})
        except Exception:
            result.append({"id": point.name, "status": "error", "message": "Trusted plugin could not be loaded"})
    return result
