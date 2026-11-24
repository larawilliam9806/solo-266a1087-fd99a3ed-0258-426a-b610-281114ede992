"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import router
from .config import SEED_DEMO_TREE
from .db import init_db
from .errors import ApiProblem
from .seed import seed_fixed_tree

logger = logging.getLogger("reconciliation")

# Initialize at import time as well as lifespan so TestClient(app) used without
# a ``with`` block still has the schema and fixed tree available.
init_db()
if SEED_DEMO_TREE:
    seed_fixed_tree()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if SEED_DEMO_TREE:
        seed_fixed_tree()
    yield


app = FastAPI(
    title="Hourly Park Electricity Reconciliation",
    version="1.0.0",
    description=(
        "SQLite-backed hourly Wh reconciliation over a fixed meter tree. "
        "Readings and affected conclusions commit atomically."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiProblem)
async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
    body = {
        "detail": exc.message,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    }
    if exc.details:
        body.update(exc.details)
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed.",
            "error": {"code": "request_validation_error", "message": "Request validation failed."},
            "errors": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error; the last committed ledger was preserved.",
            "error": {"code": "internal_error", "message": str(exc)},
        },
    )


# Primary unprefixed REST API plus common versioned/domain prefixes.
app.include_router(router)
for prefix in (
    "/api",
    "/api/v1",
    "/v1",
    "/electricity",
    "/energy",
    "/hourly",
    "/api/electricity",
    "/api/v1/electricity",
):
    app.include_router(router, prefix=prefix, include_in_schema=False)
