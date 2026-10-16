"""Workspace-backed, one-step undo for file-manager mutations.

Snapshots are bounded and stored under the workspace. A mutation is recorded only
when its post-state is verifiable; undo refuses to overwrite later user edits.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from ..config import WORKSPACE_DIR

_LOCK = threading.RLock()
_MAX_HISTORY = 20
_MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def _undo_dir() -> Path:
    return Path(WORKSPACE_DIR) / ".jarvis_file_undo"


def _history_path() -> Path:
    return _undo_dir() / "history.json"


def _objects_dir() -> Path:
    return _undo_dir() / "objects"


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _measure(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for current, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            item = Path(current) / name
            if not item.is_symlink():
                total += item.stat().st_size
                if total > _MAX_SNAPSHOT_BYTES:
                    return total
    return total


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _fingerprint(path: Path) -> str:
    """Hash path names and stat metadata so changed post-operation state is detected."""
    if not _exists(path):
        return "missing"
    rows: list[tuple[str, str, int, int, str]] = []

    def add(item: Path, relative: str) -> None:
        st = item.lstat()
        if item.is_symlink():
            kind, size, target = "symlink", 0, os.readlink(item)
        elif item.is_dir():
            kind, size, target = "dir", 0, ""
        else:
            kind, size, target = "file", st.st_size, ""
        rows.append((relative, kind, size, st.st_mtime_ns, target))

    add(path, ".")
    if path.is_dir() and not path.is_symlink():
        for current, dirs, files in os.walk(path, followlinks=False):
            dirs.sort()
            files.sort()
            for name in (*dirs, *files):
                child = Path(current) / name
                add(child, child.relative_to(path).as_posix())
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_history() -> list[dict[str, Any]]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("undo history must be a JSON list")
        return data[-_MAX_HISTORY:]
    except Exception as exc:
        raise RuntimeError(f"file undo history is unreadable ({type(exc).__name__}); no file changed") from exc


def _save_history(history: list[dict[str, Any]]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(history[-_MAX_HISTORY:], ensure_ascii=False, indent=2)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".history-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def begin_file_change(path: Path, action: str, *, source: Path | None = None) -> dict[str, Any]:
    """Snapshot the previous target before a mutation; caller commits or rolls back."""
    target = Path(path).expanduser().absolute()
    original = Path(source).expanduser().absolute() if source is not None else None
    with _LOCK:
        _load_history()  # fail closed rather than mutate without a working undo journal
        ident = uuid.uuid4().hex
        object_dir = _objects_dir() / ident
        backup = object_dir / "previous"
        existed = _exists(target)
        if existed:
            size = _measure(target)
            if size > _MAX_SNAPSHOT_BYTES:
                raise ValueError(f"undo snapshot limit is {_MAX_SNAPSHOT_BYTES // (1024 * 1024)} MiB")
            object_dir.mkdir(parents=True, exist_ok=True)
            try:
                _copy_path(target, backup)
            except Exception:
                shutil.rmtree(object_dir, ignore_errors=True)
                raise
        return {
            "id": ident,
            "action": str(action)[:40],
            "path": str(target),
            "source": str(original) if original is not None else "",
            "previous_exists": existed,
            "backup": str(backup) if existed else "",
            "object_dir": str(object_dir),
            "created_at": time.time(),
        }


def commit_file_change(token: dict[str, Any]) -> None:
    """Record the operation's exact post-state after its filesystem change succeeded."""
    target = Path(str(token["path"]))
    entry = dict(token)
    entry["after"] = _fingerprint(target)
    with _LOCK:
        history = _load_history()
        history.append(entry)
        dropped = history[:-_MAX_HISTORY]
        _save_history(history)
        for old in dropped:
            old_dir = str(old.get("object_dir") or "")
            if old_dir:
                shutil.rmtree(old_dir, ignore_errors=True)


def rollback_file_change(token: dict[str, Any]) -> None:
    """Restore the pre-operation state if the mutation or journal commit failed."""
    target = Path(str(token["path"]))
    try:
        if token.get("action") == "rename" and token.get("source"):
            source = Path(str(token["source"]))
            if _exists(target) and not _exists(source):
                target.rename(source)
        elif token.get("previous_exists"):
            backup = Path(str(token["backup"]))
            if _exists(backup):
                staged = target.with_name(f".{target.name}.jarvis-restore-{token['id']}")
                _remove(staged)
                _copy_path(backup, staged)
                _remove(target)
                os.replace(staged, target)
        else:
            _remove(target)
    finally:
        object_dir = str(token.get("object_dir") or "")
        if object_dir:
            shutil.rmtree(object_dir, ignore_errors=True)


@tool
async def undo_file_operation() -> str:
    """Undo the last verified file-manager write, copy, rename, delete, or create."""
    from ..security.gate import authorize_command
    from ..security.permissions import check, current_surface

    surface = current_surface()
    decision = check("undo_file_operation", surface=surface)
    if not decision.allowed:
        return f"🚫 File undo permission blocked: {decision.reason}"

    with _LOCK:
        try:
            history = _load_history()
        except RuntimeError as exc:
            return f"❌ {exc}"
        if not history:
            return "⚠️ Koi recent file operation undo ke liye nahi hai; kuch nahi badla."
        entry = dict(history[-1])
        target = Path(str(entry.get("path") or ""))
        if not target.is_absolute():
            return "❌ Undo record me absolute path nahi hai; history safe rakhi hai."
        try:
            if _fingerprint(target) != str(entry.get("after") or ""):
                return f"⚠️ Undo nahi hua: {target.name} operation ke baad badal chuki hai; history safe rakhi hai."
        except Exception as exc:
            return f"❌ Undo verify nahi hua ({type(exc).__name__}); history safe rakhi hai."

    action = str(entry.get("action") or "file change")
    source = Path(str(entry.get("source") or "")) if entry.get("source") else None
    if action == "rename" and source is not None and _exists(source):
        return f"⚠️ Undo nahi hua: original path pehle se maujood hai: {source}"
    needs_gate = bool(entry.get("previous_exists") and _exists(target)) or not bool(entry.get("previous_exists"))
    if needs_gate:
        approval = await authorize_command(
            f"undo {action} for {target}", str(target.parent), force=True,
            surface=surface, reason="file-manager undo changes or removes the current path",
        )
        if not approval.allowed:
            return f"🚫 Undo nahi hua: {approval.reason}. File/history unchanged."

    with _LOCK:
        try:
            history = _load_history()
        except RuntimeError as exc:
            return f"❌ {exc}"
        if not history or str(history[-1].get("id")) != str(entry.get("id")):
            return "⚠️ Undo history badal gayi; koi file nahi badli. Dobara latest operation check karo."
        try:
            if _fingerprint(target) != str(entry.get("after") or ""):
                return f"⚠️ Undo nahi hua: {target.name} approval ke baad badal chuki hai; history safe rakhi hai."
            if action == "rename" and source is not None and _exists(source):
                return f"⚠️ Undo nahi hua: original path approval ke baad ban gayi: {source}"
            if entry.get("previous_exists"):
                backup = Path(str(entry.get("backup") or ""))
                if not _exists(backup):
                    return "❌ Undo snapshot missing hai; history safe rakhi hai."

            if action == "rename" and source is not None:
                target.rename(source)
            elif entry.get("previous_exists"):
                backup = Path(str(entry.get("backup") or ""))
                staged = target.with_name(f".{target.name}.jarvis-undo-{entry['id']}")
                _remove(staged)
                _copy_path(backup, staged)
                _remove(target)
                os.replace(staged, target)
            else:
                _remove(target)
            history.pop()
            _save_history(history)
            object_dir = str(entry.get("object_dir") or "")
            if object_dir:
                shutil.rmtree(object_dir, ignore_errors=True)
        except Exception as exc:
            return f"❌ Undo fail hua ({type(exc).__name__}); history safe rakhi hai."

    try:
        from .file_tools import invalidate_index

        invalidate_index()
    except Exception:
        pass
    return f"✅ Undo ho gaya: {action} — {target}"


__all__ = ["begin_file_change", "commit_file_change", "rollback_file_change", "undo_file_operation"]
