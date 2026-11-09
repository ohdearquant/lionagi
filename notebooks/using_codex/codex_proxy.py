#!/usr/bin/env python3
# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Codex CLI Proxy API Server - OpenAI Compatible

A FastAPI server that proxies requests to OpenAI Codex CLI using OpenAI API format.
Run this, then use iModel(endpoint=...) from notebooks to call Codex.

Usage:
    uv run python playground/proxies/codex_proxy.py
    uv run python playground/proxies/codex_proxy.py --port 8001
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from loguru import logger

from lionagi.service.connections.providers.codex_cli import CodexCLIEndpoint
from lionagi.service.third_party.codex_models import (
    CODEX_CLI,
    HAS_CODEX_CLI,
    CodexCodeRequest,
)

logger.remove()
logger.add(sys.stderr, level="INFO")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not HAS_CODEX_CLI:
        raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex")
    logger.info(f"Codex CLI found: {CODEX_CLI}")
    logger.info("Codex CLI Proxy API started successfully")
    yield


app = FastAPI(
    title="Codex CLI Proxy API - OpenAI Compatible",
    description="Proxy server for OpenAI Codex CLI - Personal Development Use",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "codex-cli-proxy"}


@app.post("/v1/query")
async def query(request: CodexCodeRequest):
    endpoint = CodexCLIEndpoint()
    try:
        return await endpoint._call(payload={"request": request}, headers={})
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": str(exc),
                    "type": "internal_error",
                    "param": None,
                    "code": None,
                }
            }
        ),
        status_code=500,
        media_type="application/json",
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Codex CLI Proxy API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    uvicorn.run(
        "codex_proxy:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
