import asyncio
import contextlib
import time

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .db import make_engine, make_session_factory
from .models import Message, ModelConfig, Provider, Run, RunCommand, now
from .security import SecretStore
from .services import (
    ACTIVE_STATES,
    TERMINAL,
    ServiceError,
    add_event,
    apply_commands,
    budget_limits,
    ensure_budget,
    finish_cancelled,
    reject_pending_commands,
    reserve_budget,
    runtime_config,
    settle_message_output,
)
from .settings import Settings


class WorkerOwnershipLost(RuntimeError):
    """The Worker can no longer prove exclusive ownership; do not persist results."""


class WorkerLock:
    """Only one worker per deployment; PostgreSQL session lock in production."""

    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.connection = self.file = None
        self.lost = False

    def __enter__(self):
        if self.engine.dialect.name == "postgresql":
            self.connection = self.engine.connect()
            if not self.connection.scalar(text("SELECT pg_try_advisory_lock(7243301201)")):
                self.connection.close()
                raise RuntimeError("Another OmniHarness worker already owns this database")
            # Session-level advisory locks survive commit. End SQLAlchemy's
            # implicit transaction so every Worker operation has a short, explicit
            # transaction on this same owner connection.
            self.connection.commit()
        else:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            self.file = open(self.settings.data_dir / "worker.lock", "a+b")
            self.file.write(b"0")
            self.file.flush()
            self.file.seek(0)
            try:
                if __import__("os").name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("Another local development worker is running") from None
        return self

    def check(self):
        if self.lost:
            raise WorkerOwnershipLost("Worker execution ownership was lost")
        if self.connection:
            try:
                if self.connection.invalidated or self.connection.closed:
                    raise WorkerOwnershipLost("Worker execution ownership was lost")
                with self.connection.begin():
                    self.connection.execute(text("SELECT 1"))
            except (DBAPIError, WorkerOwnershipLost) as exc:
                self.lost = True
                raise WorkerOwnershipLost("Worker execution ownership was lost") from exc
        elif self.file is not None and self.file.closed:
            self.lost = True
            raise WorkerOwnershipLost("Worker execution ownership was lost")

    @contextlib.contextmanager
    def session(self, factory):
        """Use the owner connection for the entire PostgreSQL transaction.

        A separate pooled connection would admit a stale writer after the owner
        backend dies, even if an earlier ownership check had succeeded.
        """
        if self.lost:
            raise WorkerOwnershipLost("Worker execution ownership was lost")
        if self.connection is None:
            self.check()
            with factory.begin() as db:
                yield db
            return
        try:
            if self.connection.invalidated or self.connection.closed:
                raise WorkerOwnershipLost("Worker execution ownership was lost")
            with self.connection.begin():
                # Force the owner backend to be checked before yielding. If it
                # dies while the transaction is active PostgreSQL rolls it back.
                self.connection.execute(text("SELECT 1"))
                with Session(bind=self.connection, expire_on_commit=False) as db:
                    with db.begin():
                        yield db
        except (DBAPIError, WorkerOwnershipLost) as exc:
            self.lost = True
            raise WorkerOwnershipLost("Worker execution ownership was lost") from exc

    def __exit__(self, *_):
        if self.connection:
            if not self.lost:
                with contextlib.suppress(Exception):
                    self.connection.execute(text("SELECT pg_advisory_unlock(7243301201)"))
            self.connection.close()
        if self.file:
            self.file.close()
        self.lost = True


class Worker:
    def __init__(self, settings: Settings, engine=None, stream_fn=None):
        from .providers import stream_chat

        self.settings = settings
        self.engine = engine or make_engine(settings)
        self.sessions = make_session_factory(self.engine)
        self.secrets = SecretStore(settings)
        self.stream_fn = stream_fn or stream_chat
        self.lock = None
        self._v10_owner_listener = None

    @contextlib.contextmanager
    def transaction(self):
        if self.lock:
            with self.lock.session(self.sessions) as db:
                yield db
        else:
            # Explicit direct single-shot execution is used by local tests.
            # The production serve() entry always holds WorkerLock.
            with self.sessions.begin() as db:
                yield db

    def finish(self, identity, status, code=None, message=None, usage=None):
        with self.transaction() as db:
            run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
            if run.status in TERMINAL:
                return
            if run.config_snapshot.get("workflow_type") != "agent":
                from .context_usage import include_summary_usage
                usage = include_summary_usage(db, identity, usage)
            if run.status == "cancel_requested" or status == "cancelled":
                finish_cancelled(db, run, usage)
                return
            run.status, run.finished_at = status, now()
            run.error_code, run.error_message = code, message
            run.usage = usage
            reject_pending_commands(db, run)
            assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
            assistant.status = status
            settle_message_output(assistant, status)
            add_event(db, run, f"run.{status}", {"message_id": assistant.id, "code": code, "message": message,
                                                  "usage": usage, "output_metadata": assistant.output_metadata})

    def recover(self):
        with self.transaction() as db:
            rows = list(db.scalars(select(Run).where(Run.status.in_(
                ["running", "cancel_requested", "pause_requested"]))))
            chats = [run for run in rows if run.config_snapshot.get("workflow_type") != "agent"]
            identities = [run.id for run in chats if run.status in {"running", "cancel_requested"}]
            pausing = [run.id for run in chats if run.status == "pause_requested"]
        for identity in identities:
            self.finish(identity, "interrupted", "WORKER_RESTARTED", "执行进程中断，已保留收到的文本；请重新发送以继续。")
        for identity in pausing:
            status = self.mark_paused(identity, reason="worker_restarted")
            # A resume may race the recovery scan. The old model stream is gone,
            # so mark it interrupted rather than leaving a phantom running Run.
            if status == "running":
                self.finish(identity, "interrupted", "WORKER_RESTARTED", "执行进程中断，已保留收到的文本；请重新发送以继续。")

    def consume_commands(self):
        with self.transaction() as db:
            run_ids = list(db.scalars(select(RunCommand.run_id).where(
                RunCommand.status == "pending").distinct()))
        for identity in run_ids:
            with self.transaction() as db:
                run = db.get(Run, identity)
                agent = bool(run and run.config_snapshot.get("workflow_type") == "agent")
                apply_commands(db, identity, defer_requirements=agent)
        from .m4_runtime import reconcile_approval_waits
        reconcile_approval_waits(self)

    async def prepare_chat_history(self, identity, history, snapshot):
        """Use the shared summarizer while keeping chat stop/pause responsive."""
        from .context_host import prepare_chat_context
        from .context_policy import ContextPolicyError, public_context_error
        from .m3_agents import AgentBudgetExceeded
        from .m3_host import AgentCancellationRequested, AgentControlPending, AgentOperationNeedsReview
        from .providers import ProviderError

        task = asyncio.create_task(prepare_chat_context(self, identity, history, snapshot,
                                  model_factory=getattr(self, "summary_model_factory", None)))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=self.settings.poll_seconds)
                self.consume_commands()
                if self.cancelled(identity) or self.pause_requested(identity):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    if self.cancelled(identity):
                        self.finish(identity, "cancelled")
                    else:
                        status = self.mark_paused(identity)
                        if status == "running":
                            raise ProviderError("SUMMARY_INTERRUPTED", "上下文整理已被中断，原始记录保留；未重复调用模型。")
                    return None
            prepared = task.result()
            roles = {"human": "user", "ai": "assistant", "system": "system"}
            return [{"role": roles[message.type], "content": message.content} for message in prepared.messages]
        except AgentCancellationRequested:
            self.finish(identity, "cancelled")
            return None
        except AgentControlPending:
            if self.pause_requested(identity):
                self.mark_paused(identity)
                return None
            raise ProviderError("SUMMARY_CONTROL_STOP", "上下文整理停在控制边界，原始记录已保留。") from None
        except AgentBudgetExceeded:
            raise ProviderError("BUDGET_EXCEEDED", "摘要与回答共享的模型调用预算已用尽。") from None
        except AgentOperationNeedsReview:
            raise ProviderError("SUMMARY_RESULT_UNKNOWN", "上次摘要结果未确认，未重复付费调用；原始记录保留。") from None
        except ContextPolicyError as exc:
            from .context_host import record_context_rejection
            record_context_rejection(self, identity, exc)
            raise ProviderError(exc.code, public_context_error(exc.code)) from None
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
    def claim(self):
        selected = None
        qualifier = self._v10_owner_listener
        try:
            with self.transaction() as db:
                # A paused or active run in a conversation keeps later tasks of that
                # conversation queued, while other conversations remain runnable.
                active = select(Run.conversation_id).where(Run.status.in_(ACTIVE_STATES))
                candidates = db.scalars(select(Run).where(Run.status == "queued",
                                Run.conversation_id.not_in(active))
                                .order_by(Run.created_at, Run.ordinal)
                                .with_for_update(skip_locked=True))
                from .m4_services import acquire_workspace_lease
                run = next((item for item in candidates if acquire_workspace_lease(db, item.id)), None)
                if run is None:
                    return None
                selected = run.id
                if qualifier is not None:
                    try:
                        # A failed qualification must not poison the ordinary
                        # claim transaction or create authorization.
                        with db.begin_nested():
                            qualifier.before_claim(db, run)
                    except Exception:
                        qualifier.claim_failed(run.id)
                run.status = "running"
                if run.started_at is None:
                    run.started_at = now()
                assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
                assistant.status = "running"
                add_event(db, run, "run.started", {"message_id": assistant.id, "model": run.model_label})
        except BaseException:
            if qualifier is not None:
                qualifier.claim_failed(selected)
            raise
        return selected

    def cancelled(self, identity):
        if self.lock:
            self.lock.check()
        with self.transaction() as db:
            return db.get(Run, identity).status == "cancel_requested"

    def pause_requested(self, identity):
        if self.lock:
            self.lock.check()
        with self.transaction() as db:
            return db.get(Run, identity).status == "pause_requested"

    def mark_paused(self, identity, reason="safe_boundary", usage=None):
        with self.transaction() as db:
            run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
            if run is None:
                return None
            # Cancellation can arrive after pause_requested() returned but
            # before this lock was acquired; finish it instead of abandoning it.
            if run.status == "cancel_requested":
                finish_cancelled(db, run, usage)
                return "cancelled"
            if run.status != "pause_requested":
                return run.status
            run.status = "paused"
            assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
            if assistant:
                assistant.status = "paused"
                settle_message_output(assistant, "paused")
            if usage is not None:
                run.usage = usage
            add_event(db, run, "run.paused", {"reason": reason, "message_id": assistant.id if assistant else None,
                                               "output_metadata": assistant.output_metadata if assistant else {}})
            return "paused"

    def append(self, identity, content="", *, reasoning="", metadata=None):
        with self.transaction() as db:
            run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
            if run.status not in {"running", "cancel_requested", "pause_requested"}:
                return
            message = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
            if content:
                message.content += content
                add_event(db, run, "message.delta", {"message_id": message.id, "delta": content})
            if reasoning:
                message.reasoning_content += reasoning
                add_event(db, run, "message.reasoning_delta", {"message_id": message.id, "delta": reasoning})
            if metadata:
                message.output_metadata = {**(message.output_metadata or {}), **metadata}
                add_event(db, run, "message.output_state", {"message_id": message.id,
                                                            "output_metadata": message.output_metadata})

    def reserve_model_call(self, identity):
        limits = budget_limits(self.settings)
        with self.transaction() as db:
            from .history_scope import require_current_scope_authorized
            run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
            if run is not None:
                require_current_scope_authorized(db, run)
            return reserve_budget(db, identity, "model_calls", limits)

    async def process(self, identity):
        from .providers import ProviderError

        with self.transaction() as db:
            current = db.get(Run, identity)
            agent = bool(current and current.config_snapshot.get("workflow_type") == "agent")
        if agent:
            from .m3_worker import run_agent

            await run_agent(self, identity)
            return

        usage, pending, iterator = None, None, None
        buffer, reasoning_buffer = "", ""
        output_metadata = {}
        saw_answer, saw_reasoning = False, False

        def flush():
            nonlocal buffer, reasoning_buffer
            if buffer or reasoning_buffer:
                self.append(identity, buffer, reasoning=reasoning_buffer)
                buffer, reasoning_buffer = "", ""

        try:
            with self.transaction() as db:
                run = db.get(Run, identity)
                model = db.get(ModelConfig, run.model_config_id)
                provider = db.get(Provider, model.provider_id)
                if not provider.enabled:
                    raise ProviderError("MODEL_DISABLED", "此模型已停用，排队任务未发送。")
                config = runtime_config(run.config_snapshot, self.secrets)
                from .history_scope import select_history
                history, history_decisions = select_history(db, run)
                from .context_visibility import record_history_selection
                record_history_selection(db, run, history_decisions)
                snapshot = run.config_snapshot
                ensure_budget(db, identity, budget_limits(self.settings))
            history = await self.prepare_chat_history(identity, history, snapshot)
            if history is None:
                return
            if len(history) > self.settings.max_chat_messages or sum(len(m["content"]) for m in history) > self.settings.max_chat_characters:
                raise ProviderError("CONTEXT_LIMIT", "此对话已达到本地历史上限，请创建新对话。")
            if not self.reserve_model_call(identity):
                raise ProviderError("BUDGET_EXCEEDED", "任务已达到模型调用预算上限，已停止派发。")
            iterator = self.stream_fn(config, history).__aiter__()
            started, flush_at, buffer = time.monotonic(), time.monotonic(), ""
            while True:
                self.consume_commands()
                if self.cancelled(identity):
                    flush()
                    self.finish(identity, "cancelled", usage=usage)
                    return
                if self.pause_requested(identity):
                    flush()
                    status = self.mark_paused(identity, usage=usage)
                    if status == "running":
                        # A resume won the race before the pause checkpoint.
                        continue
                    return
                if time.monotonic() - started > 1800:
                    raise ProviderError("RUN_TIMEOUT", "任务达到 30 分钟执行上限。")
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                ready, _ = await asyncio.wait({pending}, timeout=self.settings.poll_seconds)
                if ready:
                    try:
                        delta = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        break
                    pending = None
                    if delta.usage is not None:
                        usage = delta.usage
                    buffer += delta.text
                    reasoning_buffer += getattr(delta, "reasoning", "")
                    saw_answer = saw_answer or bool(delta.text.strip())
                    saw_reasoning = saw_reasoning or bool(getattr(delta, "reasoning", ""))
                    metadata = {key: value for key in ("finish_reason", "output_status")
                                if (value := getattr(delta, key, None)) is not None}
                    if metadata:
                        # Preserve received channels before consuming a terminal
                        # diagnosis that may be followed by ProviderError.
                        flush()
                        output_metadata.update(metadata)
                        self.append(identity, metadata=metadata)
                if (buffer or reasoning_buffer) and (
                        time.monotonic() - flush_at >= 0.1 or len(buffer) + len(reasoning_buffer) >= 200):
                    flush()
                    flush_at = time.monotonic()
            flush()
            if output_metadata.get("output_status") not in (None, "complete"):
                raise ProviderError("INCOMPLETE_RESPONSE", "模型未完成正式回答，已保留收到的内容。")
            if not saw_answer:
                self.append(identity, metadata={"output_status": "reasoning_only" if saw_reasoning else "empty"})
                raise ProviderError("REASONING_ONLY" if saw_reasoning else "EMPTY_RESPONSE",
                                    "模型只返回了思考过程，未提供最终回答。" if saw_reasoning else "模型未返回正式回答。")
            self.finish(identity, "completed", usage=usage)
        except WorkerOwnershipLost:
            # A replacement Worker may already own the Run. Do not save the
            # buffer or write an error through another database connection.
            raise
        except ProviderError as exc:
            flush()
            self.finish(identity, "failed", exc.code, exc.public_message, usage)
        except ServiceError as exc:
            flush()
            if exc.code in {"HISTORY_SCOPE_INVALID", "HISTORY_SOURCE_INVALID"}:
                self.finish(identity, "failed", exc.code, exc.message, usage)
            else:
                self.finish(identity, "failed", "EXECUTION_ERROR", "执行失败；请检查模型设置与本地运行环境。", usage)
        except asyncio.CancelledError:
            flush()
            self.finish(identity, "interrupted", "WORKER_STOPPED", "执行进程停止，已保留收到的文本。", usage)
            raise
        except Exception:
            flush()
            self.finish(identity, "failed", "EXECUTION_ERROR", "执行失败；请检查模型设置与本地运行环境。", usage)
        finally:
            if pending:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            if iterator and hasattr(iterator, "aclose"):
                with contextlib.suppress(Exception):
                    await iterator.aclose()

    async def run_once(self):
        self.consume_commands()
        qualifier = self._v10_owner_listener
        if qualifier is not None:
            await qualifier.wait_before_claim()
        identity = self.claim()
        if identity:
            if qualifier is not None and identity in qualifier.committing:
                try:
                    await qualifier.confirm_claim(identity)
                except Exception:
                    qualifier.claim_failed(identity)
            try:
                await self.process(identity)
            finally:
                if qualifier is not None:
                    qualifier.discard(identity)
        return identity

    async def serve(self):
        with WorkerLock(self.engine, self.settings) as lock:
            self.lock = lock
            if self.settings.v10_preview and self.engine.dialect.name == "postgresql":
                from .v10_owner_runtime import V10OwnerListener
                self._v10_owner_listener = await V10OwnerListener.start(self)
            try:
                self.recover()
                from .m3_worker import recover_agents

                await recover_agents(self)
                while True:
                    lock.check()
                    if await self.run_once() is None:
                        await asyncio.sleep(self.settings.poll_seconds)
            finally:
                if self._v10_owner_listener is not None:
                    await self._v10_owner_listener.close()
                    self._v10_owner_listener = None
