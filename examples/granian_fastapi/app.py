"""FastAPI application with structured request and Granian server logging."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from structguru import configure, logger
from structguru.integrations.asgi import StructguruMiddleware

SERVICE_NAME = "granian-fastapi-example"

# Importing this module configures both the launcher and every spawned worker.
configure(service=SERVICE_NAME, level="INFO", format="json")

LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "structguru": {
            "class": "structguru.integrations.stdlib.StructguruHandler",
        },
    },
    "root": {"handlers": ["structguru"], "level": "INFO"},
    "loggers": {
        "_granian": {"handlers": [], "level": "INFO", "propagate": True},
        "granian.access": {"handlers": [], "level": "INFO", "propagate": True},
    },
}

app = FastAPI()
app.add_middleware(StructguruMiddleware, request_id_header="X-Request-ID")


@app.get("/health")
async def health() -> dict[str, bool]:
    logger.info("Health checked")
    return {"ok": True}
