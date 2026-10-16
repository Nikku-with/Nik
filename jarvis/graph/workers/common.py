"""Shared graph-worker evidence helpers and tool-calling adapter."""
from __future__ import annotations

import asyncio
import re
import time
from posixpath import normpath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from langchain_core.messages import AIMessage, HumanMessage

from ...config import PROVIDER, is_offline
from ...llm import get_chat_model
from ...prompts import leaf_worker_prompt
from ...runtime.progress import emit
from ..state import JarvisState


def _event(state: JarvisState, node: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"node": node, "detail": detail, "ts": time.time(), **extra}

def _finding(worker: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"worker": worker, "summary": summary[:12000], "ts": time.time(), **extra}

def _permission_block(state: JarvisState, worker: str, tool_name: str) -> dict[str, Any] | None:
    from ...security.permissions import check as permission_check

    decision = permission_check(tool_name, str(state.get("surface") or "cli_owner"))
    if decision.allowed:
        return None
    message = f"Permission blocked: {tool_name} is {decision.level}; surface limit is {decision.max_level}."
    emit("error", message, tool=tool_name, status="fail")
    return {
        "findings": [_finding(worker, message)],
        "errors": [message],
        "execution_required": True,
        "blocked_tools": [tool_name],
        "status": "failed",
        "events": [_event(state, f"{worker}_worker", message)],
    }

def _extract_urls(text: str) -> list[str]:
    """Normalize explicit URLs and bare domains, avoiding duplicate browser targets."""
    source = text or ""
    url_matches = list(re.finditer(r"https?://[^\s,)\"']+", source, re.I))
    url_spans = [(match.start(), match.end()) for match in url_matches]
    candidates: list[tuple[int, str, bool]] = []

    for match in url_matches:
        candidates.append((match.start(), match.group(0).rstrip(".,);!?"), False))
    domain_rx = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|in|org|net|io|ai|co|dev|gov|edu)\b", re.I)
    for match in domain_rx.finditer(source):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        candidates.append((match.start(), match.group(0).rstrip(".,);!?"), True))

    normalized: list[tuple[int, str, str, str, bool]] = []
    for position, raw, bare in candidates:
        candidate = f"https://{raw}" if bare else raw
        try:
            parts = urlsplit(candidate)
            hostname = (parts.hostname or "").lower().rstrip(".")
            if not hostname:
                continue
            port = parts.port
            host = hostname if not port or (parts.scheme == "https" and port == 443) \
                or (parts.scheme == "http" and port == 80) else f"{hostname}:{port}"
            path = normpath(parts.path or "/")
            if not path.startswith("/"):
                path = f"/{path}"
            scheme = (parts.scheme or "https").lower()
            output_path = "" if path == "/" else path
            output = urlunsplit((scheme, host, output_path, parts.query, ""))
            normalized.append((position, output, host, path, bare))
        except ValueError:
            continue

    explicit_hosts = {host for _, _, host, _, bare in normalized if not bare}
    seen_paths: set[tuple[str, str]] = set()
    seen_bare_hosts: set[str] = set()
    selected: list[tuple[int, str]] = []
    for position, url, host, path, bare in sorted(normalized, key=lambda item: item[0]):
        if bare:
            if host in explicit_hosts or host in seen_bare_hosts:
                continue
            seen_bare_hosts.add(host)
        else:
            key = (host, path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
        selected.append((position, url))
    return [url for _, url in sorted(selected, key=lambda item: item[0])[:8]]

class SubAgentRun:
    """Sub-agent ka result — text ke saath *actual* tool calls ka record.

    Ye isliye zaroori hai: model "kar diya" bol deta hai chahe tool chala hi na ho.
    Yahan hum sirf wahi maante hain jo tool ne khud return kiya.
    """

    def __init__(self, text: str = "", calls: list[dict[str, Any]] | None = None, error: str = "",
                 retries: int = 0) -> None:
        self.text = text or ""
        self.calls = calls or []
        self.error = error
        self.retries = max(0, int(retries))

    @property
    def tool_count(self) -> int:
        return len(self.calls)

    @property
    def ok_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("ok")]

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c.get("ok")]

    def lines(self, limit: int = 6) -> list[str]:
        out = []
        for c in self.calls[-limit:]:
            mark = "✅" if c.get("ok") else "❌"
            out.append(f"{mark} {c.get('name')}({_short_args(c.get('args'))}) → {str(c.get('output'))[:220]}")
        return out

def _short_args(args: Any) -> str:
    try:
        if isinstance(args, dict):
            return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
        return str(args)[:80]
    except Exception:
        return "?"

def _looks_like_failure(output: str) -> bool:
    """Detect explicit failure results without scanning arbitrary returned file/page content."""
    first = next((line.strip().lower() for line in str(output or "").splitlines() if line.strip()), "")
    if not first:
        return False
    if first.startswith("✅"):
        return False
    if first.startswith(("❌", "⚠️", "🤔", "🚫")):
        return True
    # Tool adapters often return a plain error message instead of raising. Anchor these checks
    # to the result's opening phrase so quoted file content mentioning "cannot" or "exception"
    # does not turn an otherwise successful read into a failed tool call.
    first = first.lstrip(" \t\"'([{*•-")
    bad_prefixes = (
        "error:", "traceback", "not found", "nahi mila", "fail:", "fail hua", "failed", "failure",
        "read fail:", "open fail:", "unavailable", "permission denied", "access is denied",
        "module not found", "no module named", "exception:", "launch nahi", "verify nahi",
        "unable to", "unable:", "could not", "cannot ", "can't ", "not able", "blocked", "denied",
        "install karo",
    )
    return first.startswith(bad_prefixes)

def _tool_call_record(name: str, args: Any, output: Any, *, ok: bool | None = None) -> dict[str, Any]:
    """Record the tool's returned status; explicit status beats any words in its content."""
    if ok is None and isinstance(output, dict) and isinstance(output.get("ok"), bool):
        ok = output["ok"]
    output_text = str(output)
    result_ok = not _looks_like_failure(output_text) if ok is None else bool(ok)
    return {"name": name, "args": args if isinstance(args, dict) else {"_raw": str(args)[:120]},
            "output": output_text[:800], "ok": result_ok}

def _wrap_tools_with_ledger(tools: list, ledger: dict[str, Any], worker: str) -> list:
    """Wrap LangChain tools so a successful call is replayed from the turn ledger, not rerun."""
    from ..idempotency import completed_action, remember_completed_action

    try:
        from langchain_core.tools import StructuredTool
    except Exception:
        return tools

    wrapped = []
    for original in tools:
        name = str(getattr(original, "name", "tool") or "tool")
        schema = getattr(original, "args_schema", None)
        if schema is None:
            wrapped.append(original)
            continue

        def _make_wrapper(tool_obj, tool_name, args_schema):
            async def _invoke(**kwargs):
                cached = completed_action(ledger, tool_name, kwargs)
                if cached:
                    if "result" in cached:
                        return cached["result"]
                    return cached.get("output", "(previously completed)")
                result = await tool_obj.ainvoke(kwargs)
                if _tool_call_record(tool_name, kwargs, result)["ok"]:
                    remember_completed_action(
                        ledger, tool_name, kwargs, result, output=str(result), worker=worker
                    )
                return result

            return StructuredTool.from_function(
                coroutine=_invoke,
                name=tool_name,
                description=str(getattr(tool_obj, "description", "") or tool_name),
                args_schema=args_schema,
                return_direct=bool(getattr(tool_obj, "return_direct", False)),
            )

        try:
            wrapped.append(_make_wrapper(original, name, schema))
        except Exception:
            wrapped.append(original)
    return wrapped

async def _run_react_subagent(
    role: str, task: str, tools: list, raw_request: str = "",
    done_actions: dict[str, Any] | None = None,
) -> SubAgentRun:
    """Ek specialist ReAct sub-agent chalao (LangChain 1.x create_agent) + tool calls track karo."""
    if not tools:
        return SubAgentRun(text="(koi tool available nahi)", error="no tools")
    ledger = done_actions if done_actions is not None else {}
    if is_offline() or PROVIDER == "demo":
        # offline: tools ko deterministic queue me chalao (demo brain).
        # NOTE: instructions wala wrapper text use nahi karte, warna uske words
        # (jaise "launch"/"band karna") galat tool trigger kar dete hain.
        from .offline import _offline_tool_run

        text, calls = await _offline_tool_run(raw_request or task, tools, done_actions=ledger)
        return SubAgentRun(text=text, calls=calls)

    tools = _wrap_tools_with_ledger(tools, ledger, worker=role)
    from ...llm.core import ToolBindingError

    try:
        try:
            from langchain.agents import create_agent  # LangChain 1.x
        except Exception:  # import hi fail hua (1.x nahi hai) → legacy ReAct
            create_agent = None  # type: ignore[assignment]

        # tool-required path: model tool-capable hona hi chahiye. `require_tool_bound` unbound
        # / un-flagged chains par ToolBindingError deta hai (silent plain-text generation nahi).
        from ...runtime.evidence import require_tool_bound

        model = require_tool_bound(get_chat_model("brain"), role="brain")
        tools_bound_already = bool(getattr(model, "tools_bound", True))

        system_prompt = leaf_worker_prompt(role)
        if create_agent is not None:
            agent = create_agent(model, tools=tools, system_prompt=system_prompt)
        else:
            from langgraph.prebuilt import create_react_agent  # fallback

            agent = create_react_agent(model, tools=tools, prompt=system_prompt)
        _ = tools_bound_already  # (create_agent khud bind karta hai; flag sirf precondition proof)

        async def invoke_once(prompt: str):
            return await asyncio.wait_for(
                agent.ainvoke({"messages": [HumanMessage(content=prompt)]}, config={"recursion_limit": 25}),
                timeout=300,
            )

        def parse_messages(result: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
            msgs = result.get("messages", []) if isinstance(result, dict) else []
            pending: dict[str, dict[str, Any]] = {}
            calls: list[dict[str, Any]] = []
            texts: list[str] = []
            for message in msgs:
                if isinstance(message, AIMessage):
                    content = message.content
                    if isinstance(content, str) and content.strip():
                        texts.append(content.strip())
                    elif isinstance(content, list):
                        readable = " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                                            for part in content).strip()
                        if readable:
                            texts.append(readable)
                    for tool_call in (getattr(message, "tool_calls", None) or []):
                        call_id = tool_call.get("id") or tool_call.get("name") or f"call{len(pending)}"
                        pending[call_id] = {"name": tool_call.get("name") or "tool",
                                            "args": tool_call.get("args") or {}}
                elif message.__class__.__name__ == "ToolMessage":
                    call_id = getattr(message, "tool_call_id", "") or ""
                    meta = pending.pop(call_id, {}) or {}
                    status = getattr(message, "status", None)
                    ok = status == "success" if status in {"success", "error"} else None
                    calls.append(_tool_call_record(meta.get("name", getattr(message, "name", "tool") or "tool"),
                                                   meta.get("args", {}), str(message.content), ok=ok))
            for meta in pending.values():
                calls.append({"name": meta.get("name", "tool"), "args": meta.get("args", {}),
                              "output": "(tool result nahi aaya — call aborted)", "ok": False})
            return (texts[-1] if texts else ""), calls, texts

        result = await invoke_once(task)
        body, calls, texts = parse_messages(result)
        retries = 0
        refusal = re.search(r"\b(?:cannot|can't|won't|unable|not able|refuse|can't help|nahi kar sakta|"
                            r"nahi kar paunga|as an ai|i am unable)\b", body, re.I)
        user_gate = re.search(r"\b(?:login required|sign in required|captcha|manual confirmation|"
                              r"owner action|required user|human approval)\b", body, re.I)
        if not calls and tools and (not body.strip() or (refusal and not user_gate)):
            # A blank/refusal response is retried once only when this worker has registered,
            # permission-filtered tools. Never repeat after any tool attempt or user-gated block.
            retries = 1
            emit("warn", f"{role} returned empty/refusal with usable tools; one targeted retry", status="retry")
            retry_prompt = (
                f"{task}\n\nYour previous reply was empty or refused, but this worker has registered tools. "
                "Make one concrete attempt with the appropriate tool(s). Do not claim success without tool evidence. "
                "If a human login/approval is genuinely required, ask for that explicitly instead."
            )
            retry_result = await invoke_once(retry_prompt)
            retry_body, retry_calls, _retry_texts = parse_messages(retry_result)
            if retry_calls or retry_body.strip():
                body, calls = retry_body, retry_calls
            else:
                body = body or "(empty response after one tool-enabled retry)"

        if not body:
            body = "(subagent ne kuch nahi kaha)"
        if calls:
            body += "\n\ntool calls:\n" + "\n".join(f"- {line}" for line in SubAgentRun(calls=calls).lines())
        return SubAgentRun(text=body, calls=calls, retries=retries)
    except ToolBindingError as exc:
        # tools bind hi nahi huye → bina tools ke "confident" jawab dena galat hoga.
        # Isliye loud fail: user ko saaf reason, aur graph ise error ke roop me record karta hai.
        emit("error", f"tools model ke saath bind nahi hue — {str(exc)[:160]}", status="fail")
        return SubAgentRun(
            text=("⚠️ is kaam ke liye tools ko model ke saath bind nahi kiya ja saka, aur bina "
                  "tools ke jawab dena galat hota — isliye ruk gaya. Reason: " + str(exc)),
            error=f"tool_binding_failed: {str(exc)[:200]}",
        )
    except Exception as exc:
        return SubAgentRun(text=f"(subagent error: {exc})", error=str(exc))
