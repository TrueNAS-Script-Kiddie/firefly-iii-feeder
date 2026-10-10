"""
Generic runtime utilities used across the CSV pipeline.

This module contains only small, infrastructure-level helpers that:
- do NOT belong to CSV logic
- do NOT belong to row-state logic
- do NOT belong to normalization logic
- do NOT belong to completion logic

Functions included:
- log_event: append timestamped log entries
- load_env: minimal .env key=value loader
- alert: write failure notifications to stderr (cron emails them)
"""

import os
import sys
from datetime import datetime


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_event(logfile_path: str, message: str) -> None:
    """
    Append a timestamped log message to the logfile.

    Logging must never interrupt the pipeline. Any failure is silently ignored.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(logfile_path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} {message}\n")
    except Exception:
        # Logging failures must never break the pipeline
        pass


# ---------------------------------------------------------------------------
# Minimal .env loader
# ---------------------------------------------------------------------------
def load_env(path: str) -> dict[str, str]:
    """
    Load a minimal .env file containing simple KEY=VALUE pairs.

    - Lines starting with '#' are ignored.
    - Empty lines are ignored.
    - No quoting, no type conversion, no nesting.
    - Returns a dict with string keys and string values.

    This loader is intentionally minimalistic to avoid dependencies.
    """
    config: dict[str, str] = {}

    if not os.path.exists(path):
        return config

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()
    except Exception:
        # Config loading must never break the pipeline
        pass

    return config


# ---------------------------------------------------------------------------
# Alert via stderr
# ---------------------------------------------------------------------------
def alert(subject: str, body: str) -> None:
    """
    Write an alert to stderr. The TrueNAS cron job (Hide Standard Error off)
    emails anything on stderr, so success must stay silent.
    """
    print(f"{subject}\n{body}\n", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Global config (loaded once)
# ---------------------------------------------------------------------------
# Project root: engine/core/runtime.py -> three levels up
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = load_env(os.path.join(BASE_DIR, "config", "app.env"))
