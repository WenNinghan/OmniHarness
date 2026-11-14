"""Isolated checkpoint-namespace mapping experiment, not a production saver.

This adapter supplies no authorization, owner check, durable phase ledger or
write fencing. It must not be attached to Worker or used for real side effects.
"""

from __future__ import annotations

import re
from copy import deepcopy

from langgraph.checkpoint.base import BaseCheckpointSaver

_PREFIX = re.compile(r"^v10corr-[a-f0-9]{64}$")


class CorrectionNamespacePrototype(BaseCheckpointSaver):
    def __init__(self, delegate, *, run_id, prefix):
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(prefix, str)
            or not _PREFIX.fullmatch(prefix)
        ):
            raise ValueError("Invalid prototype namespace")
        super().__init__(serde=delegate.serde)
        self.delegate, self.run_id, self.prefix = delegate, run_id, prefix

    def _in(self, config):
        if not isinstance(config, dict) or not isinstance(config.get("configurable"), dict):
            raise ValueError("Checkpoint config unavailable")
        conf = config["configurable"]
        namespace = conf.get("checkpoint_ns", "")
        if conf.get("thread_id") != self.run_id or not isinstance(namespace, str) or "\x00" in namespace:
            raise ValueError("Checkpoint scope differs")
        return {
            **config,
            "configurable": {**conf, "checkpoint_ns": self.prefix + ("|" + namespace if namespace else "")},
        }

    def _out(self, config):
        if config is None:
            return None
        conf = config.get("configurable", {})
        namespace = conf.get("checkpoint_ns")
        if conf.get("thread_id") != self.run_id or not isinstance(namespace, str):
            raise ValueError("Stored checkpoint scope differs")
        if namespace == self.prefix:
            virtual = ""
        elif namespace.startswith(self.prefix + "|"):
            virtual = namespace[len(self.prefix) + 1 :]
        else:
            raise ValueError("Stored checkpoint namespace differs")
        return {**config, "configurable": {**conf, "checkpoint_ns": virtual}}

    def _tuple(self, item):
        if item is None:
            return None
        return item._replace(config=self._out(item.config), parent_config=self._out(item.parent_config))

    async def aget_tuple(self, config):
        return self._tuple(await self.delegate.aget_tuple(self._in(config)))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if config is None:
            raise ValueError("Run-scoped listing required")
        before = self._in(before) if before is not None else None
        async for item in self.delegate.alist(self._in(config), filter=filter, before=before, limit=limit):
            yield self._tuple(item)

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await self.delegate.aput(
            self._in(config), deepcopy(checkpoint), deepcopy(metadata), new_versions
        )
        return self._out(result)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self.delegate.aput_writes(self._in(config), writes, task_id, task_path)

    def get_next_version(self, current, channel):
        return self.delegate.get_next_version(current, channel)
