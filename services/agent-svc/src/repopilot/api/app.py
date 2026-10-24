"""The RepoPilot HTTP API. It records runs and queues jobs; workers run the agent."""

import hmac
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from repopilot.api.repo import RunsRepo
from repopilot.api.sse import run_events
from repopilot.bench.tasks import Task
from repopilot.eval.score import EvalResult, summarize

log = logging.getLogger(__name__)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_TOKEN = "dev-api-token-change-me"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class Queue(Protocol):
    async def enqueue(self, job: dict) -> str: ...


class Claims(Protocol):
    async def claim_for_approval(self, run_id: str) -> dict | None: ...

    async def status(self, run_id: str, status: str, **fields: Any) -> None: ...


def bind_problem(host: str, token: str) -> str | None:
    """Why serving on `host` with `token` is unsafe, or None. Off loopback the API is reachable
    by others, so the shipped default token, or a guessable one, is refused."""
    if host in _LOOPBACK:
        return None
    if token == DEFAULT_TOKEN:
        return f"REPOPILOT_API_TOKEN is the default; set a random one before binding {host}"
    if len(token) < 24:
        return "REPOPILOT_API_TOKEN is too short to expose; use at least 24 random characters"
    return None


class ResultStore(Protocol):
    async def load(self, name: str) -> list[EvalResult]: ...


@dataclass
class ApiServices:
    repo: RunsRepo
    claims: Claims
    queue: Queue
    results: ResultStore
    load_task: Callable[[str], Task | None]
    token: str
    ui_origin: str
    default_model: str
    poll_s: float = 1.0
    settle_s: float = 10.0  # how long an event stream stays open after the run ends


class CreateRun(BaseModel):
    task_id: str | None = Field(default=None, description="A benchmark task id.")
    repo: str | None = Field(default=None, description="owner/name of a GitHub repository.")
    issue_number: int | None = Field(default=None, ge=1)
    auto_approve: bool = False
    model: str | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> "CreateRun":
        bench, github = self.task_id is not None, self.repo is not None
        if bench == github:
            raise ValueError("give either task_id, or repo and issue_number")
        if github and (self.issue_number is None or not _REPO.match(self.repo)):
            raise ValueError("repo must be owner/name and issue_number is required")
        if github and self.auto_approve:
            raise ValueError("auto_approve is for benchmark runs; a pull request needs a reviewer")
        return self


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=4000)
    # Who decided, as recorded with the approval and named in the pull request. One line: no
    # control characters, so it cannot add lines to the PR body.
    by: str = Field(default="web", min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("by", mode="before")
    @classmethod
    def _trimmed(cls, v: Any) -> Any:
        if v is None:
            return "web"
        return v.strip() if isinstance(v, str) else v


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(services: ApiServices) -> FastAPI:
    # No public schema or docs UI: every route but /healthz needs the token.
    app = FastAPI(
        title="RepoPilot", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[services.ui_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )

    @app.middleware("http")
    async def trace_and_headers(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        # Runs outside the middleware stack, so the headers are set here; details stay in logs.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = _error(500, "internal", "internal error")
        response.headers.update(SECURITY_HEADERS)
        return response

    async def enqueue(job: dict, on_failure: Callable[[], Awaitable[None]]) -> None:
        try:
            await services.queue.enqueue(job)
        except Exception as exc:
            log.exception("could not enqueue %s job for run %s", job["type"], job["run_id"])
            await on_failure()
            raise ApiError(
                503, "queue_unavailable", "the job queue is unavailable; try again"
            ) from exc

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return _error(422, "invalid_request", message)

    def authorized(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization.encode(), f"Bearer {services.token}".encode()):
            raise ApiError(401, "unauthorized", "missing or invalid bearer token")

    auth = [Depends(authorized)]

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/runs", status_code=202, dependencies=auth)
    async def create_run(body: CreateRun) -> dict:
        run_id = str(uuid.uuid4())
        model = body.model or services.default_model
        if body.task_id is not None:
            task = services.load_task(body.task_id)
            if task is None:
                raise ApiError(422, "unknown_task", f"no benchmark task {body.task_id!r}")
            await services.repo.create_queued_run(
                run_id,
                repo=task.repo,
                issue_text=task.issue,
                task_id=task.id,
                issue_ref=None,
                task_mode="bench",
                model=model,
            )
        else:
            await services.repo.create_queued_run(
                run_id,
                repo=body.repo,
                issue_text="",
                task_id=None,
                issue_ref=f"{body.repo}#{body.issue_number}",
                task_mode="github",
                model=model,
            )
        await enqueue(
            {"type": "start", "run_id": run_id, "auto_approve": body.auto_approve},
            lambda: services.claims.status(run_id, "failed", failure_reason="enqueue_failed"),
        )
        return {"run_id": run_id, "status": "queued"}

    @app.get("/v1/runs", dependencies=auth)
    async def list_runs(limit: int = Query(50, ge=1, le=500)) -> dict:
        return {"runs": await services.repo.list_runs(limit)}

    async def _run_or_404(run_id: str) -> dict:
        run = await services.repo.get_run(run_id)
        if run is None:
            raise ApiError(404, "not_found", f"no run {run_id}")
        return run

    @app.get("/v1/runs/{run_id}", dependencies=auth)
    async def get_run(run_id: str) -> dict:
        return await _run_or_404(run_id)

    @app.get("/v1/runs/{run_id}/tool-calls", dependencies=auth)
    async def tool_calls(run_id: str) -> dict:
        await _run_or_404(run_id)
        return {"tool_calls": await services.repo.tool_calls(run_id)}

    @app.get("/v1/runs/{run_id}/events", dependencies=auth)
    async def events(
        run_id: str, last_event_id: str | None = Header(default=None), after: int = Query(0, ge=0)
    ) -> StreamingResponse:
        await _run_or_404(run_id)
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def stream():
            async with services.repo.event_session() as session:
                async for chunk in run_events(
                    session, run_id, start, poll_s=services.poll_s, settle_s=services.settle_s
                ):
                    yield chunk

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/v1/runs/{run_id}/approval", status_code=202, dependencies=auth)
    async def approval(run_id: str, body: Decision) -> dict:
        await _run_or_404(run_id)
        # Claim first: of two concurrent decisions only one may resume the run.
        if await services.claims.claim_for_approval(run_id) is None:
            raise ApiError(409, "not_awaiting_approval", f"run {run_id} is not awaiting approval")
        # If the job cannot be queued, hand the decision back to the reviewer.
        await enqueue(
            {
                "type": "resume",
                "run_id": run_id,
                "decision": {"decision": body.decision, "comment": body.comment, "by": body.by},
            },
            lambda: services.claims.status(run_id, "awaiting_approval"),
        )
        return {"run_id": run_id, "status": "running"}

    @app.get("/v1/evals", dependencies=auth)
    async def evals() -> dict:
        return {"evals": await services.repo.eval_names()}

    @app.get("/v1/evals/{name}", dependencies=auth)
    async def eval_detail(name: str) -> dict:
        results = await services.results.load(name)
        if not results:
            raise ApiError(404, "not_found", f"no eval {name}")
        return {
            "name": name,
            "summary": summarize(results).model_dump(),
            "results": [r.model_dump() for r in results],
        }

    return app
