"""Worker registry for the compiled graph."""
from .browser import browser_worker
from .code import code_worker
from .desktop import desktop_worker
from .memory import memory_worker
from .research import research_worker

WORKER_NODES = {
    "browser": browser_worker,
    "research": research_worker,
    "code": code_worker,
    "desktop": desktop_worker,
    "memory": memory_worker,
}

__all__ = ["WORKER_NODES", "browser_worker", "code_worker", "desktop_worker", "memory_worker", "research_worker"]
