"""
jarvis.config — ek hi jagah se poora configuration (env + runtime overrides)
============================================================================

    from jarvis.config import settings, runtime
    settings.PROVIDER, settings.MODEL_ROUTES["brain"], settings.config_report()
    runtime.get("owner_name"), runtime.set_many(owner_name="Nikhil")
"""
from __future__ import annotations

from . import runtime, settings  # noqa: F401  (dono sub-modules accessible)
from .runtime import SCHEMA as SETTINGS_SCHEMA  # noqa: F401
from .runtime import describe as describe_settings  # noqa: F401
from .runtime import get as get_setting  # noqa: F401
from .runtime import reset as reset_setting  # noqa: F401
from .runtime import set_many as set_settings  # noqa: F401
from .runtime import snapshot as settings_snapshot  # noqa: F401
from .settings import *  # noqa: F401,F403  (backward-compatible flat namespace)

__all__ = [name for name in dir() if not name.startswith("_")]
