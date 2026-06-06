#!/usr/bin/env python3
"""
Claude Code Proxy API Server - OpenAI Compatible
A FastAPI server that proxies requests to Claude Code CLI using OpenAI API format.
Compatible with Roo Code, Cline, and other OpenAI API clients.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from loguru import logger

from lionagi.service.connections.providers.claude_code_cli import (
    ClaudeCodeCLIEndpoint,
    ClaudeCodeRequest,
)

# Configure logging
logger.remove()
logger.add(sys.stderr, level="INFO")


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_claude_code()
    logger.info("Claude Code CLI Proxy API started successfully")
    yield
    pass


# FastAPI app
app = FastAPI(
    title="Claude Code Proxy API - OpenAI Compatible",
    description="Proxy server for Claude Code CLI - Personal Development Use",
    version="1.0.0",
    lifespan=lifespan,
)


# Check if Claude Code is available
def check_claude_code():
    """Verify Claude Code CLI is installed and accessible"""
    try:
        from lionagi.service.third_party.claude_code import CLAUDE_CLI

        result = subprocess.run([CLAUDE_CLI, "--version"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("Claude Code not found, please install it first:\n")
        logger.info(f"Claude Code found: {result.stdout.strip()}")
    except Exception as e:
        logger.error(f"Claude Code check failed: {e}")
        raise RuntimeError(
            "Claude Code CLI not found. Please install it first:\n"
            "npm install -g @anthropic-ai/claude-code"
        )


# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "claude-code-cli-proxy"}


@app.post("/v1/query")
async def query(request: ClaudeCodeRequest):
    endpoint = ClaudeCodeCLIEndpoint()
    try:
        return await endpoint._call(payload={"request": request}, headers={})
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# Error handling
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
    # Parse command line arguments
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code Proxy API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    # Run server
    uvicorn.run(
        "claude_code_proxy:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
