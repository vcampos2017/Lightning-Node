# error_handler.py
"""
Centralized logging & error handling for LightningSensorBluesky.
"""

import logging
import sys
import traceback
from datetime import datetime


_LOGGER_INITIALIZED = False


def init_logging(log_file: str = "lightning_bluesky.log") -> None:
    """Configure logging once for the whole app."""
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return

    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    _LOGGER_INITIALIZED = True


def _timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def warn(message: str, context: str = "") -> None:
    """Print and log a non-fatal warning."""
    init_logging()
    full = f"[{_timestamp()}] WARNING{f' ({context})' if context else ''}: {message}"
    logging.warning(full)
    print(full)


def handle_error(
    err: Exception,
    context: str = "",
    fatal: bool = False,
) -> None:
    """
    Centralized error handler.

    fatal=True will exit with code 1 after logging a full traceback.
    """
    init_logging()

    base = f"[{_timestamp()}] ERROR"
    if context:
        base += f" while {context}"
    base += f": {repr(err)}"

    # Log error + traceback
    logging.error(base)
    logging.error("Traceback:\n" + "".join(traceback.format_exception(err)))

    # Print a shorter message for journalctl / console
    print(base)

    if fatal:
        print("[LightningSensorBluesky] Fatal error – exiting. See log file for details.")
        sys.exit(1)