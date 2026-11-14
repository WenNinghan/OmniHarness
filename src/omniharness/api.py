import asyncio
import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from . import services as runtime_services
from .db import make_engine, make_session_factory
from .models import (
    AgentRun,
    Conversation,
    LocalSession,
    Message,
    ModelConfig,
    Provider,
    Run,
    RunCommand,
    RunEvent,
    now,
)
from .plugins import discover_plugins
from .providers import ProviderError
from .schemas import (
    ActionRequirementsUpdate,
    ClarificationAnswerRequest,
    ConversationRequest,
    ImportRequest,
    ModelConfigRequest,
    PairRequest,
    ProbeRequest,
    RoleBindingsRequest,
    RunCommandRequest,
    SendMessageRequest,
    StageBindingsRequest,
)
from .security import SecretStore, digest, exchange_code, valid_session
from .services import (
    TERMINAL,
    ServiceError,
    agent_run_output,
    budget_limits,
    budget_output,
    cancel_run,
    command_output,
    conversation_output,
    enqueue,
    event_output,
    message_output,
    model_output,
    require,
    role_binding_output,
    run_output,
    runtime_config,
    save_model,
    save_role_bindings,
    save_stage_bindings,
    snapshot_model,
    stage_binding_output,
    submit_command,
    update_action_requirements,
)
from .settings import Settings

COOKIE = "omniharness_session"


def agent_available(settings, engine):
    """Readiness only; a wrong/missing fence schema must never admit a Run."""
    if not runtime_services.AGENT_RUNTIME_WIRED or not settings.enable_agent or engine.dialect.name != "postgresql":
        return False
    from .checkpoint_fencing import _connect, _schema, _verify
    from .m3_worker import _checkpoint_url

    async def verify():
        schema = _schema(settings.checkpoint_schema)
        async with await _connect(_checkpoint_url(settings.database_url)) as connection:
            await _verify(connection, schema)

    try:
        factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=factory) as runner:
            runner.run(asyncio.wait_for(verify(), timeout=3))
        return True
    except Exception:
        return False


def create_app(settings: Settings | None = None, engine=None):
    settings = settings or Settings()
    engine = engine or make_engine(settings)
    factory = make_session_factory(engine)
    secrets = SecretStore(settings)
    app = FastAPI(title="OmniHarness", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.engine, app.state.sessions = settings, engine, factory
    app.state.secret_store = secrets
    app.state.plugins = discover_plugins()
    attempts = defaultdict(deque)
    hosts = {urlsplit(origin).hostname for origin in settings.origins} | {"localhost", "127.0.0.1"}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(h for h in hosts if h))

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Browsers scope host-only cookies by host, not port. Keep the preview
        # on localhost so pairing via the 127.0.0.1 alias cannot replace the
        # production instance's cookie. Service-authenticated internal calls
        # and a cookie-free health probe retain their existing access paths.
        path = request.url.path
        internal = path == "/api/internal/workspaces" or path.startswith("/api/internal/workspaces/")
        if settings.v6_preview and path != "/health" and not internal:
            host = request.headers.get("host", "").lower()
            port = host[len("localhost:"):] if host.startswith("localhost:") else ""
            localhost = host == "localhost" or (len(port) <= 5 and port.isascii()
                                                  and port.isdigit() and 0 < int(port) <= 65535)
            if not localhost:
                return JSONResponse({"detail": {"code": "PREVIEW_HOST_REQUIRED",
                                    "message": "V6 预览请通过 localhost 地址访问。"}}, 403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in settings.origins:
                return JSONResponse({"detail": {"code": "ORIGIN_DENIED", "message": "请求来源不允许。"}}, 403)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("x-omni-request") != "1":
                return JSONResponse({"detail": {"code": "REQUEST_HEADER_REQUIRED", "message": "缺少本地请求标识。"}}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.exception_handler(ProviderError)
    async def provider_error(_, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": exc.public_message}}, 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        fields = [".".join(str(part) for part in e["loc"]) for e in exc.errors()]
        return JSONResponse({"detail": {"code": "VALIDATION_ERROR", "message": "输入格式或参数不符合要求。", "fields": fields}}, 422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(_, __):
        return JSONResponse({"detail": {"code": "CONFLICT", "message": "请求与已有记录冲突，请刷新后重试。"}}, 409)

    def authenticated(request: Request):
        if not valid_session(factory, request.cookies.get(COOKIE)):
            raise HTTPException(401, detail={"code": "PAIRING_REQUIRED", "message": "请使用本机配对码连接控制台。"})

    auth = [Depends(authenticated)]

    @app.get("/health")
    def health():
        try:
            with engine.connect() as db:
                db.execute(text("SELECT 1"))
            return {"status": "ok", "version": __version__, "database": "ok", "stage": "M4-preview"}
        except Exception:
            return JSONResponse({"status": "unavailable", "database": "unavailable", "version": __version__}, 503)

    @app.get("/api/v1/bootstrap")
    def bootstrap(request: Request):
        valid = valid_session(factory, request.cookies.get(COOKIE))
        result = {"version": __version__, "authenticated": valid, "pairing_required": not valid,
                  "demo_enabled": settings.enable_demo, "stage": "M4-preview",
                  "agent_available": bool(valid and agent_available(settings, engine))}
        if valid:
            result.update(v6_preview=settings.v6_preview,
                          stage_workflow_version=settings.stage_workflow_version,
                          workspace_workflow_version=settings.workspace_workflow_version)
        return result

    @app.post("/api/v1/auth/pair")
    def pair(body: PairRequest, request: Request, response: Response):
        address = request.client.host if request.client else "local"
        queue = attempts[address]
        stamp = time.monotonic()
        while queue and queue[0] < stamp - 60:
            queue.popleft()
        if len(queue) >= 5:
            raise ServiceError("PAIRING_RATE_LIMIT", "尝试过于频繁，请一分钟后重试。", 429)
        queue.append(stamp)
        token = exchange_code(factory, body.code.get_secret_value(), settings.session_days)
        if token is None:
            raise ServiceError("INVALID_PAIRING_CODE", "配对码无效、已使用或已过期，请在本机重新生成。", 401)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            max_age=settings.session_days * 86400, path="/")
        return {"authenticated": True}

    @app.post("/api/v1/auth/logout", dependencies=auth, status_code=204)
    def logout(request: Request):
        with factory.begin() as db:
            db.execute(delete(LocalSession).where(LocalSession.token_hash == digest(request.cookies[COOKIE])))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/v1/openapi.json", dependencies=auth)
    def openapi():
        return app.openapi()

    @app.get("/api/v1/model-configs", dependencies=auth)
    def models():
        with factory() as db:
            return [model_output(db, row) for row in db.scalars(select(ModelConfig).order_by(ModelConfig.created_at))]

    @app.post("/api/v1/model-configs", dependencies=auth, status_code=201)
    def create_model(body: ModelConfigRequest):
        with factory.begin() as db:
            return save_model(db, body, secrets, settings)

    @app.put("/api/v1/model-configs/{identity}", dependencies=auth)
    def update_model(identity: str, body: ModelConfigRequest):
        with factory.begin() as db:
            return save_model(db, body, secrets, settings, identity)

    @app.delete("/api/v1/model-configs/{identity}", dependencies=auth, status_code=204)
    def disable_model(identity: str):
        with factory.begin() as db:
            model = require(db, ModelConfig, identity)
            require(db, Provider, model.provider_id).enabled = False
        return Response(status_code=204)

    @app.post("/api/v1/model-configs/{identity}/test", dependencies=auth)
    async def test_model(identity: str, body: ProbeRequest):
        from .providers import probe_model

        with factory() as db:
            snapshot = snapshot_model(db, identity)
        results = await probe_model(runtime_config(snapshot, secrets), list(dict.fromkeys(body.probes)))
        with factory.begin() as db:
            model = db.scalar(select(ModelConfig).where(ModelConfig.id == identity).with_for_update())
            if model is None:
                raise ServiceError("NOT_FOUND", "模型配置不存在。", 404)
            if model.version != snapshot["version"]:
                raise ServiceError("CONFIG_CHANGED", "测试期间配置已改变，请重新测试。", 409)
            model.capabilities = {**model.capabilities, **results}
            model.tested_at = now()
            return {"model_config_id": identity, "results": results, "tested_at": model.tested_at}

    @app.get("/api/v1/configuration/export", dependencies=auth)
    def export_configuration():
        fields = set(ModelConfigRequest.model_fields) - {"api_key"}
        with factory() as db:
            configs = [{k: v for k, v in model_output(db, model).items() if k in fields}
                       for model in db.scalars(select(ModelConfig))]
        return {"schema_version": 1, "model_configs": configs}

    @app.post("/api/v1/configuration/import", dependencies=auth)
    def import_configuration(body: ImportRequest):
        with factory.begin() as db:
            for model in body.model_configs:
                save_model(db, model, secrets, settings)
        return {"imported": len(body.model_configs)}

    @app.get("/api/v1/conversations", dependencies=auth)
    def conversations():
        with factory() as db:
            return [conversation_output(row) for row in db.scalars(select(Conversation).order_by(Conversation.updated_at.desc()))]

    @app.post("/api/v1/conversations", dependencies=auth, status_code=201)
    def create_conversation(body: ConversationRequest):
        with factory.begin() as db:
            row = Conversation(title=body.title)
            db.add(row)
            db.flush()
            return conversation_output(row)

    @app.get("/api/v1/conversations/{identity}/messages", dependencies=auth)
    def messages(identity: str):
        with factory() as db:
            require(db, Conversation, identity)
            rows = db.execute(select(Message, Run).join(Run).where(Message.conversation_id == identity)
                              .order_by(Run.ordinal, Message.role.desc()))
            return [message_output(row, run) for row, run in rows]

    @app.post("/api/v1/conversations/{identity}/messages", dependencies=auth, status_code=202)
    def send_message(identity: str, body: SendMessageRequest):
        if body.workflow_type == "agent" and not agent_available(settings, engine):
            raise ServiceError("AGENT_RUNTIME_UNAVAILABLE", "Agent 执行尚未就绪，请使用普通聊天。", 503)
        owner_challenge = None
        if (settings.v10_preview and body.workflow_type == "agent" and body.workspace_id
                and body.auto_correction_enabled is not False):
            from .v10_owner_runtime import request_owner_challenge
            owner_challenge = request_owner_challenge(settings)
        with factory.begin() as db:
            return enqueue(db, identity, body, settings, owner_challenge=owner_challenge)

    @app.get("/api/v1/runs", dependencies=auth)
    def runs(conversation_id: str | None = None):
        query = select(Run).order_by(Run.created_at.desc()).limit(100)
        if conversation_id:
            query = query.where(Run.conversation_id == conversation_id)
        with factory() as db:
            return [run_output(row) for row in db.scalars(query)]

    @app.get("/api/v1/runs/{identity}", dependencies=auth)
    def get_run(identity: str):
        with factory() as db:
            return run_output(require(db, Run, identity))

    @app.get("/api/v1/runs/{identity}/observation", dependencies=auth)
    def get_run_observation(identity: str):
        from .run_observation import observe_run
        with factory() as db:
            if engine.dialect.name == "postgresql":
                db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                db.execute(text("SET TRANSACTION READ ONLY"))
            else:
                # sqlite3's legacy transaction mode does not BEGIN on SELECT.
                db.execute(text("BEGIN"))
            return observe_run(db, identity)

    @app.put("/api/v1/runs/{identity}/action-requirements", dependencies=auth)
    def revise_action_requirements(identity: str, body: ActionRequirementsUpdate):
        with factory.begin() as db:
            return update_action_requirements(db, identity, body)

    @app.get("/api/v1/runs/{identity}/clarifications", dependencies=auth)
    def get_clarifications(identity: str):
        from .clarification_service import list_clarifications
        with factory() as db:
            return list_clarifications(db, identity)

    @app.post("/api/v1/runs/{identity}/clarifications/{question_id}/answer", dependencies=auth)
    def answer_question(identity: str, question_id: str, body: ClarificationAnswerRequest):
        from .clarification_service import answer_clarification
        with factory.begin() as db:
            return answer_clarification(db, identity, question_id, body.client_request_id, body.answer)

    @app.get("/api/v1/runs/{identity}/agent-runs", dependencies=auth)
    def agent_runs(identity: str):
        with factory() as db:
            run = require(db, Run, identity)
            rows = db.scalars(select(AgentRun).where(AgentRun.run_id == identity)
                              .order_by(AgentRun.created_at, AgentRun.id))
            return [agent_run_output(row, run) for row in rows]

    @app.post("/api/v1/runs/{identity}/cancel", dependencies=auth)
    def cancel(identity: str):
        with factory.begin() as db:
            return cancel_run(db, identity)

    @app.post("/api/v1/runs/{identity}/commands", dependencies=auth, status_code=202)
    def command(identity: str, body: RunCommandRequest):
        with factory.begin() as db:
            return submit_command(db, identity, body)

    @app.get("/api/v1/runs/{identity}/commands", dependencies=auth)
    def commands(identity: str):
        with factory() as db:
            run = require(db, Run, identity)
            return [command_output(row, run.status) for row in db.scalars(
                select(RunCommand).where(RunCommand.run_id == identity)
                .order_by(RunCommand.sequence_number)
            )]

    @app.get("/api/v1/orchestration/bindings", dependencies=auth)
    def get_role_bindings():
        with factory() as db:
            return role_binding_output(db)

    @app.put("/api/v1/orchestration/bindings", dependencies=auth)
    def put_role_bindings(body: RoleBindingsRequest):
        with factory.begin() as db:
            return save_role_bindings(db, body)

    @app.get("/api/v1/stage-bindings", dependencies=auth)
    def get_stage_bindings():
        with factory() as db:
            return stage_binding_output(db)

    @app.put("/api/v1/stage-bindings", dependencies=auth)
    def put_stage_bindings(body: StageBindingsRequest):
        with factory.begin() as db:
            return save_stage_bindings(db, body)

    @app.get("/api/v1/runs/{identity}/budget", dependencies=auth)
    def budget(identity: str):
        with factory() as db:
            require(db, Run, identity)
            return budget_output(db, identity, budget_limits(settings))

    @app.get("/api/v1/runs/{identity}/events", dependencies=auth)
    async def events(identity: str, request: Request, after: int = 0):
        try:
            header_cursor = int(request.headers.get("last-event-id", "0"))
            if after < 0 or header_cursor < 0:
                raise ServiceError("INVALID_CURSOR", "事件游标必须为非负整数。", 409)
            cursor = max(after, header_cursor)
        except ValueError:
            raise ServiceError("INVALID_CURSOR", "事件游标必须为非负整数。") from None
        with factory() as db:
            run = require(db, Run, identity)
            if cursor < 0 or cursor >= run.next_event_sequence:
                raise ServiceError("INVALID_CURSOR", "事件游标无效，请重新获取任务状态。", 409)

        async def stream():
            nonlocal cursor
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                if not valid_session(factory, request.cookies.get(COOKIE)):
                    break
                with factory() as db:
                    current = require(db, Run, identity)
                    batch = [event_output(row) for row in db.scalars(
                        select(RunEvent).where(RunEvent.run_id == identity, RunEvent.sequence_number > cursor)
                        .order_by(RunEvent.sequence_number).limit(200)
                    )]
                    done = current.status in TERMINAL
                for item in batch:
                    cursor = item["sequence_number"]
                    yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if done and len(batch) < 200:
                    break
                if time.monotonic() - heartbeat > 10:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(settings.poll_seconds)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/v1/plugins", dependencies=auth)
    def plugins():
        return app.state.plugins

    from .ai_routing import create_ai_routing_router
    from .m4_api import create_broker_grants_router, create_tools_router
    from .model_catalog import create_catalog_router

    app.include_router(create_catalog_router(factory, secrets), dependencies=auth)
    app.include_router(create_ai_routing_router(factory, secrets), dependencies=auth)
    app.include_router(create_tools_router(factory, settings), dependencies=auth)
    app.include_router(create_broker_grants_router(factory, settings))

    @app.get("/api/{path:path}")
    def unknown_api(path: str):
        raise ServiceError("NOT_FOUND", "接口不存在。", 404)

    if Path(settings.static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="web")
    else:
        @app.get("/")
        def no_frontend():
            return {"name": "OmniHarness", "stage": "M4-preview", "message": "Build the Web console with npm run build in web/."}
    return app
