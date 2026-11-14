import argparse
import asyncio
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from .db import make_engine, make_session_factory
from .security import create_pairing_code, init_secrets
from .settings import Settings


def migrate(settings: Settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def run_async(coro):
    # psycopg async requires a Selector event loop on Windows. The choice is
    # made before creating the loop, including during checkpoint bootstrap.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro)


def main():
    parser = argparse.ArgumentParser(prog="omniharness")
    parser.add_argument("command", choices=["serve", "worker", "bootstrap", "migrate", "init-secrets", "auth-code", "plugins"])
    args = parser.parse_args()
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGSMITH_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    settings = Settings()
    if args.command in {"init-secrets", "bootstrap"}:
        init_secrets(settings)
    if args.command in {"migrate", "bootstrap"}:
        migrate(settings)
        if args.command == "bootstrap" and settings.enable_agent:
            from .checkpoint_fencing import initialize_checkpoint_schema
            from .m3_worker import _checkpoint_url

            if not settings.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Agent checkpoint initialization requires PostgreSQL")
            run_async(initialize_checkpoint_schema(_checkpoint_url(settings.database_url),
                                                   schema=settings.checkpoint_schema))
        print("Database migration complete. No credentials were printed.")
    elif args.command == "auth-code":
        engine = make_engine(settings)
        try:
            code = create_pairing_code(make_session_factory(engine))
            print("One-time local pairing code (expires in 15 minutes):")
            print(code)
        finally:
            engine.dispose()
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)
    elif args.command == "worker":
        from .worker import Worker

        try:
            run_async(Worker(settings).serve())
        except KeyboardInterrupt:
            pass
    elif args.command == "plugins":
        import json

        from .plugins import discover_plugins

        print(json.dumps(discover_plugins(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("OmniHarness could not start. Check database availability, migration and configured paths.", file=sys.stderr)
        sys.exit(1)
