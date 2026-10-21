#!/usr/bin/env python3
"""Backward-compatibility shim. All code lives in estat_api.py — import from there.

Note: check_status() now raises estat_api.EStatError (was SystemExit).
"""
try:
    from .estat_api import *  # noqa: F401,F403
    from .estat_api import APP_ID, BASE, DEFAULT_SLEEP, RETRY_SLEEP, MAX_RETRIES  # noqa: F401
except ImportError:
    from estat_api import *  # noqa: F401,F403
    from estat_api import APP_ID, BASE, DEFAULT_SLEEP, RETRY_SLEEP, MAX_RETRIES  # noqa: F401
