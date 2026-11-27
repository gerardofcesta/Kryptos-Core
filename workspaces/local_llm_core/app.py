"""FastAPI application factory & Uvicorn loopback runner for Local LLM Core."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from .config import HEAVY_BASE, HOST, OLLAMA_BASE, PORT, STATIC_DIR
from .routes import (
    auth,
    batch,
    benchmark,
    control,
    go_mode,
    legacy_proxy,
    mcp,
    models,
    openai_v1,
    orchestrator,
    prompts,
    rag,
    telemetry,
    tuning,
    ui,
)
from .security import GatewaySecurityMiddleware, rotate_gateway_token


def _shutdown_orchestrator_safe() -> None:
    try:
        from workspaces.orchestrator.api_router import shutdown_orchestrator

        shutdown_orchestrator()
    except Exception:
        pass
    try:
        from .rag.store import get_rag_manager

        get_rag_manager().ghost.clear()
    except Exception:
        pass
    try:
        from .routes.mcp import get_mcp_audit_logger

        get_mcp_audit_logger().clear()
    except Exception:
        pass


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    rotate_gateway_token()
    try:
        yield
    finally:
        _shutdown_orchestrator_safe()


def create_app() -> FastAPI:
    """Create and configure the Local LLM FastAPI application."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    application = FastAPI(
        title="Kryptos Core — Offline AI Workstation & Unified Gateway",
        version="3.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    application.include_router(ui.router)
    application.include_router(auth.router)
    application.include_router(prompts.router)
    application.include_router(telemetry.router)
    application.include_router(tuning.router)
    application.include_router(control.router)
    application.include_router(orchestrator.router)
    application.include_router(rag.router)
    application.include_router(rag.chat_ephemeral_router)
    application.include_router(models.router)
    application.include_router(benchmark.router)
    application.include_router(go_mode.router)
    application.include_router(batch.router)
    application.include_router(mcp.router)
    application.include_router(legacy_proxy.router)
    application.include_router(openai_v1.router)

    application.add_middleware(GatewaySecurityMiddleware)
    return application


app = create_app()


def run_server(host: str = HOST, port: int = PORT) -> None:
    """Launch Uvicorn strictly bound to loopback `127.0.0.1` with zero access/chat logs."""
    safe_host = "127.0.0.1" if host not in ("127.0.0.1", "localhost", "::1") else host
    rotate_gateway_token()
    print(f"Kryptos Core Offline Workstation + Unified Gateway attiva su: http://{safe_host}:{port}")
    print(f"Unified OpenAI API:   http://{safe_host}:{port}/v1")
    print(f"Fast Tier (Ollama):   {OLLAMA_BASE}")
    print(f"Heavy MoE (Colibri):  {HEAVY_BASE}")
    print("Modalita: 100% loopback, zero telemetria, zero log chat su disco.")
    print("Premi CTRL+C per terminare.")
    try:
        uvicorn.run(
            app,
            host=safe_host,
            port=port,
            log_level="warning",
            access_log=False,
        )
    except KeyboardInterrupt:
        print("\nArresto server locale...")
    finally:
        _shutdown_orchestrator_safe()
