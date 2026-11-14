"""Disposable PG test child: wait after checkpoint SQL, before seal commit."""
import asyncio
import os
import sys
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint

from omniharness.checkpoint_fencing import open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.settings import Settings
from omniharness.v10_sealed_saver import V10SealedSaver
from omniharness.worker import Worker, WorkerLock


async def main():
    settings = Settings(_env_file=None, database_url=os.environ["V10_TEST_DATABASE_URL"],
                        data_dir=Path(os.environ["V10_TEST_DATA_DIR"]),
                        secret_dir=Path(os.environ["V10_TEST_SECRET_DIR"]))
    run_id = os.environ["V10_TEST_RUN_ID"]
    worker = Worker(settings)
    with WorkerLock(worker.engine, settings) as owner:
        ticket = register_fence(owner, run_id, "stage-agent-v10", schema=os.environ["V10_TEST_CHECKPOINT_SCHEMA"])
        async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
            saver = V10SealedSaver(base.conn, ticket, business_schema=os.environ["V10_TEST_BUSINESS_SCHEMA"],
                                   settings=settings)
            inventory = saver._inventory
            calls = 0

            async def pause_before_seal(cur):
                nonlocal calls
                calls += 1
                result = await inventory(cur)
                if calls == 2:
                    Path(os.environ["V10_TEST_READY_FILE"]).write_text("ready", encoding="utf-8")
                    await asyncio.Event().wait()
                return result

            saver._inventory = pause_before_seal
            await saver.aput({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}},
                             empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
