"""Demonstrate stdlib existing-logger policy from code or environment."""

from __future__ import annotations

import argparse
import logging

from structguru import configure, flush, shutdown
from structguru.integrations.stdlib import (
    install_stdlib_bridge,
    install_stdlib_bridge_from_env,
    uninstall_stdlib_bridge,
)


def main() -> None:
    """Run the existing-logger policy demonstration."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="Read stdlib bridge options from STRUCTGURU_STDLIB_* variables.",
    )
    args = parser.parse_args()

    configure(service="stdlib-policy-example", level="DEBUG")

    # Existing-logger policy is evaluated when the bridge is installed.
    existing = logging.getLogger("example.third_party.existing")
    existing.setLevel(logging.DEBUG)

    bridge = (
        install_stdlib_bridge_from_env()
        if args.from_env
        else install_stdlib_bridge(level="DEBUG", disable_existing_loggers=True)
    )
    try:
        existing.warning("EXISTING_LOGGER_RECORD")

        # Like dictConfig, the policy does not affect loggers created afterward.
        created_after = logging.getLogger("example.third_party.created_after")
        created_after.warning("CREATED_AFTER_RECORD")
        flush()
    finally:
        uninstall_stdlib_bridge(bridge)
        shutdown()


if __name__ == "__main__":
    main()
