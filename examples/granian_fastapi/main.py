"""Launch the FastAPI app through Granian's Python API.

Install with ``pip install structguru fastapi granian``, then run with
``python examples/granian_fastapi/main.py``. ``HOST``, ``PORT``, and
``WEB_CONCURRENCY`` override the local defaults.
"""

from __future__ import annotations

import os

from app import LOGGING_CONFIG
from granian import Granian
from granian.constants import Interfaces
from granian.log import LogLevels


def main() -> None:
    # app is loaded by Granian in each worker; importing LOGGING_CONFIG above has
    # already configured structguru in this launcher process as well.
    Granian(
        "app:app",
        interface=Interfaces.ASGI,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        workers=int(os.environ.get("WEB_CONCURRENCY", "1")),
        log_level=LogLevels.info,
        log_dictconfig=LOGGING_CONFIG,
        log_access=False,  # StructguruMiddleware emits the structured summary.
    ).serve()


if __name__ == "__main__":
    main()
