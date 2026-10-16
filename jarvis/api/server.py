"""
jarvis/api/server.py — FastAPI server + built-in web console
===========================================================

Endpoints:
  GET  /                → web console (single-file HTML, inline CSS/JS — preview-safe)
  POST /chat            → {message, session_id} → answers (LangGraph supervisor)
  POST /task            → raw graph run + poora state JSON
  GET  /stream?message= → Server-Sent Events: live node-by-node progress
  GET  /health          → provider / tools / browser / memory status
  GET  /notes           → saved notes list
  GET/POST /apps        → verified app-launch mapping list/manage
  GET  /graph           → mermaid + ASCII architecture
  GET  /tools           → tool catalogue (kaun kaun se tool wired hain)

Run:
  python -m jarvis serve                 # local-only by default
  python -m jarvis serve 0.0.0.0 8000   # startup warns if API_AUTH_TOKEN is not configured
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import threading
import time
from collections import deque
from typing import Any

from ..config import (
    API_AUTH_TOKEN,
    API_HOST,
    API_PORT,
    ASSISTANT_NAME,
)
from ..graph.build import graph_mermaid, graph_summary
from ..memory.store import get_memory
from ..runtime.agent import run_agent

try:
    from fastapi import FastAPI, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    fastapi_available = True
except Exception:  # pragma: no cover
    fastapi_available = False
    FastAPI = None  # type: ignore
    HTMLResponse = JSONResponse = StreamingResponse = None  # type: ignore


def _status() -> dict[str, Any]:
    from ..runtime.status import agent_status

    current = agent_status(compact=True, surface="API/server")
    return {
        "assistant": current["assistant"],
        "owner": current["owner"],
        "status": current["status"],
        "interface": "API/server",
        "provider": current["provider"],
        "configured_provider": current["configured_provider"],
        "provider_status": current.get("provider_status", "unknown"),
        "model": current["model"],
        "fast_model": current["fast_model"],
        "model_route_reason": current.get("model_route_reason", ""),
        "offline_demo": current["offline"],
        "connectivity_tested": current["connection_tested"],
        "browser": current["browser"],
        "browser_use": current["browser_cli"] or "browser-use" in current["browser"],
        "browser_use_cli": current["browser_cli"],
        "livekit": current["livekit"],
        "livekit_status": current["livekit_status"],
        "telegram": current["telegram"],
        "telegram_status": current["telegram_status"],
        "workspace": current["workspace"],
        "memory": current.get("memory_stats", {}),
        "tools": current.get("tool_count", 0),
        "tools_status": current.get("tools_status", "unknown"),
        "automations": current["automations"],
    }


if fastapi_available:
    app = FastAPI(title=f"{ASSISTANT_NAME} — NikhilAI ", version="2.0")

    _AUTOMATION_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)
    _AUTOMATION_EVENTS_LOCK = threading.RLock()

    def _deliver_api_automation(event: dict[str, Any]) -> None:
        with _AUTOMATION_EVENTS_LOCK:
            _AUTOMATION_EVENTS.append(dict(event))

    @app.on_event("startup")
    async def startup_warnings() -> None:
        from ..automation import scheduler as sch
        from ..security.gate import warn_startup

        warn_startup()
        sch.configure_agent_runner()
        sch.register_delivery_handler("api", _deliver_api_automation)
        sch.start_background()

    @app.on_event("shutdown")
    async def shutdown_automation_scheduler() -> None:
        from ..automation import scheduler as sch

        sch.stop_background()
        sch.register_delivery_handler("api", None)

    def _host_is_network_exposed(raw_host: str) -> bool:
        host = str(raw_host or "127.0.0.1").strip().strip("[]").rstrip(".").lower()
        if host in {"localhost", "ip6-localhost", "localhost.localdomain"}:
            return False
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            # Unknown hostnames are treated as network-facing for the warning only.
            return True
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return not address.is_loopback

    def _network_exposed(request: Request | None = None) -> bool:
        """Detect the actual ASGI bind host for a warning; this does not block a request."""
        server = request.scope.get("server") if request is not None else None
        if isinstance(server, (tuple, list)) and server:
            runtime_host = str(server[0] or "").strip()
            if runtime_host and runtime_host.lower() not in {"testserver", "unknown"}:
                return _host_is_network_exposed(runtime_host)
        return _host_is_network_exposed(API_HOST or "127.0.0.1")

    def _token_valid(request: Request) -> bool:
        if not API_AUTH_TOKEN:
            return True
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        supplied = (request.headers.get("x-api-token") or bearer
                    or request.cookies.get("jarvis_api_session") or "")
        expected_bytes = str(API_AUTH_TOKEN).encode("utf-8")
        supplied_bytes = str(supplied).encode("utf-8")
        return bool(supplied_bytes) and hmac.compare_digest(supplied_bytes, expected_bytes)

    def _auth_failure_response() -> JSONResponse:
        return JSONResponse(
            {"error": "Unauthorized. Supply an API bearer token or unlock the web console."},
            status_code=401, headers={"WWW-Authenticate": "Bearer"},
        )

    @app.middleware("http")
    async def api_access_guard(request: Request, call_next):
        if _network_exposed(request) and not API_AUTH_TOKEN:
            from ..security.gate import warn_startup

            runtime_server = request.scope.get("server")
            warn_startup(api_host=str(runtime_server[0]) if runtime_server else API_HOST)
        path = request.url.path
        if path in {"/", "/auth/status", "/auth", "/auth/logout"}:
            return await call_next(request)
        if API_AUTH_TOKEN and not _token_valid(request):
            return _auth_failure_response()
        return await call_next(request)

    @app.get("/auth/status")
    async def auth_status(request: Request) -> JSONResponse:
        return JSONResponse({"required": bool(API_AUTH_TOKEN),
                             "authorized": _token_valid(request),
                             "auth_possible": True})

    @app.post("/auth")
    async def auth_unlock(request: Request) -> JSONResponse:
        if not API_AUTH_TOKEN:
            return JSONResponse({"ok": True, "required": False})
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        supplied = str(payload.get("token", "")) if isinstance(payload, dict) else ""
        expected_bytes = str(API_AUTH_TOKEN).encode("utf-8")
        supplied_bytes = supplied.encode("utf-8")
        if not supplied_bytes or not hmac.compare_digest(supplied_bytes, expected_bytes):
            return JSONResponse({"ok": False, "error": "Invalid API token."}, status_code=401)
        response = JSONResponse({"ok": True, "required": True})
        response.set_cookie("jarvis_api_session", API_AUTH_TOKEN, httponly=True,
                            secure=request.url.scheme.lower() == "https",
                            samesite="strict", max_age=8 * 60 * 60, path="/")
        return response

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie("jarvis_api_session", httponly=True,
                               secure=request.url.scheme.lower() == "https",
                               samesite="strict", path="/")
        return response

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(_status())

    @app.get("/approvals")
    async def pending_approvals() -> JSONResponse:
        from ..security.gate import BROKER

        return JSONResponse({"approvals": [
            {"id": req.id, "command": req.command, "why_flagged": req.why_flagged,
             "cwd": req.cwd, "expires_at": req.expires_at}
            for req in BROKER.pending_requests("api")
        ]})

    @app.post("/approvals/{request_id}")
    async def resolve_approval(request_id: str, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        allow = bool(payload.get("allow")) if isinstance(payload, dict) else False
        from ..config import settings as S
        from ..security.gate import BROKER

        owner = str(S.OWNER_ID or S.TELEGRAM_OWNER_ID or "api_owner")
        resolved = await BROKER.resolve(request_id, allow, owner, surface="api")
        if not resolved:
            return JSONResponse({"ok": False, "error": "approval expired, already used, or no longer pending"},
                                status_code=409)
        return JSONResponse({"ok": True, "allow": allow})

    @app.post("/chat")
    async def chat(payload: dict) -> JSONResponse:
        message = (payload or {}).get("message", "").strip()
        session_id = (payload or {}).get("session_id", "web")
        if not message:
            return JSONResponse({"error": "message khali hai"}, status_code=400)
        started = time.time()
        result = (await run_agent(message, session_id=session_id, surface="api")).as_dict()
        return JSONResponse(
            {
                "answer": result.get("answer", ""),
                "spoken": result.get("spoken", ""),
                "workers": result.get("workers", []),
                "plan": result.get("plan", []),
                "findings": [
                    {"worker": f.get("worker"), "summary": str(f.get("summary", ""))[:1200]}
                    for f in result.get("findings", [])
                ],
                "memory_updates": result.get("memory_updates", []),
                "artifacts": result.get("artifacts", []),
                "critique": result.get("critique", {}),
                "iterations": result.get("iterations", 0),
                "errors": result.get("errors", []),
                "status": result.get("status", "unknown"),
                "execution_required": result.get("execution_required", False),
                "verified_execution": result.get("verified_execution", False),
                "memory_persisted": result.get("memory_persisted", False),
                "task_id": result.get("task_id", ""),
                "seconds": round(time.time() - started, 2),
            }
        )

    @app.post("/task")
    async def task(payload: dict) -> JSONResponse:
        message = (payload or {}).get("task", "").strip()
        if not message:
            return JSONResponse({"error": "task khali hai"}, status_code=400)
        result = (await run_agent(message, session_id=(payload or {}).get("session_id", "api"),
                                  surface="api")).as_dict()
        safe = {k: v for k, v in result.items() if k not in ("messages",)}
        return JSONResponse(json.loads(json.dumps(safe, default=str)))

    @app.get("/stream")
    async def stream(message: str = Query(...), session_id: str = "web") -> StreamingResponse:
        async def gen():
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            started = time.monotonic()

            def on_progress(event) -> None:
                payload = {
                    "kind": event.kind, "text": event.text, "detail": event.detail,
                    "tool": event.tool, "status": event.status, "ms": event.ms, "ts": event.ts,
                }
                loop.call_soon_threadsafe(queue.put_nowait, payload)

            yield f"event: start\ndata: {json.dumps({'message': message, 'interface': 'API/server'})}\n\n"
            task = asyncio.create_task(run_agent(message, session_id=session_id,
                                                 surface="api", on_progress=on_progress))
            result_sent = False
            try:
                while not task.done() or not queue.empty():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"event: progress\ndata: {json.dumps(payload, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        if task.done():
                            break
                        yield ("event: heartbeat\ndata: "
                               + json.dumps({"elapsed_seconds": round(time.monotonic() - started),
                                             "message": "still waiting; no new runtime event"})
                               + "\n\n")
                result = await task
                final = result.as_dict()
                payload = {
                    "answer": final.get("answer", ""), "spoken": final.get("spoken", ""),
                    "workers": final.get("workers", []), "plan": final.get("plan", []),
                    "memory_updates": final.get("memory_updates", []),
                    "artifacts": final.get("artifacts", []), "errors": final.get("errors", []),
                    "status": final.get("status", "unknown"),
                    "execution_required": final.get("execution_required", False),
                    "verified_execution": final.get("verified_execution", False),
                    "memory_persisted": final.get("memory_persisted", False),
                    "seconds": round(time.monotonic() - started, 2),
                }
                yield f"event: result\ndata: {json.dumps(payload, default=str)}\n\n"
                result_sent = True
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                raise
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'error': f'{type(exc).__name__}: {str(exc)[:400]}'})}\n\n"
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
            yield f"event: end\ndata: {json.dumps({'result_sent': result_sent})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/notes")
    async def notes() -> JSONResponse:
        return JSONResponse({"notes": get_memory().notes(limit=50)})

    @app.get("/tools")
    async def tools() -> JSONResponse:
        from ..tools import describe_tools, tool_names

        return JSONResponse({"count": len(tool_names()), "names": tool_names(), "catalogue": describe_tools()})

    @app.get("/voice")
    async def voice() -> JSONResponse:
        from ..voice.livekit_agent import voice_status

        return JSONResponse(voice_status())

    @app.get("/memory")
    async def memory(limit: int = 30) -> JSONResponse:
        from ..memory.manager import get_manager

        mgr = get_manager()
        return JSONResponse({"stats": mgr.stats(), "memories": mgr.list_memories(limit=limit),
                             "pending": mgr.pending()})

    @app.post("/memory")
    async def memory_write(payload: dict) -> JSONResponse:
        """{action: remember|confirm|cancel|forget|update, text: str, id: pending/fact ID}"""
        from ..memory.manager import get_manager

        payload = payload if isinstance(payload, dict) else {}
        mgr = get_manager()
        action = str(payload.get("action", "remember")).strip().lower()
        text = str(payload.get("text", ""))
        if action == "remember":
            return JSONResponse(mgr.remember(text))
        if action in {"confirm", "cancel"}:
            pending_id = str(payload.get("id", "")).strip()
            if not pending_id:
                return JSONResponse({"ok": False, "message": "exact pending memory id is required"},
                                    status_code=400)
            result = mgr.confirm_pending(confirmed=action == "confirm", pending_id=pending_id)
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)
        if action == "forget":
            return JSONResponse(mgr.forget(text, all_matches=bool(payload.get("all"))))
        if action == "update":
            return JSONResponse(mgr.update(str(payload.get("id", "")), text))
        return JSONResponse({"ok": False, "message": f"unknown action '{action}'"}, status_code=400)

    @app.get("/apps")
    async def app_mappings() -> JSONResponse:
        from ..tools.app_finder import _APP_MEMORY

        return JSONResponse({"apps": _APP_MEMORY.list(), "load_warning": _APP_MEMORY.load_error})

    @app.post("/apps")
    async def manage_app_mappings(payload: dict) -> JSONResponse:
        from ..tools.app_finder import manage_apps

        action = str((payload or {}).get("action", "list")).lower()
        query = str((payload or {}).get("query", ""))
        if action not in {"list", "forget", "clear"} or (action == "forget" and not query.strip()):
            return JSONResponse({"ok": False, "error": "Use action=list|forget|clear; forget needs a query."},
                                status_code=400)
        try:
            result = await manage_apps.ainvoke({"action": action, "query": query})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"app mapping operation failed ({type(exc).__name__})"},
                                status_code=500)
        return JSONResponse({"ok": not str(result).startswith("❌"), "result": str(result)})

    @app.get("/automations")
    async def automations() -> JSONResponse:
        from ..automation import scheduler as sch

        store = sch.get_store()
        return JSONResponse({"status": sch.status(), "items": store.list(),
                             "load_warning": store.load_error, "runs": sch.recent_runs("api")})

    @app.get("/automations/events")
    async def automation_events() -> JSONResponse:
        with _AUTOMATION_EVENTS_LOCK:
            return JSONResponse({"events": [dict(item) for item in _AUTOMATION_EVENTS]})

    @app.get("/graph")
    async def graph() -> JSONResponse:
        return JSONResponse({"mermaid": graph_mermaid(), "summary": graph_summary()})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return CONSOLE_HTML


# ---------------------------------------------------------------------------
# Web console (inline everything — sandboxed preview me bhi chalta hai)
# ---------------------------------------------------------------------------
CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NikhilAI Jarvis — Console</title>
<style>
  :root{
    --bg:#070b14; --panel:#0d1424; --panel2:#121b30; --line:#1e2b47;
    --text:#e8eefc; --muted:#8fa3c8; --accent:#4da3ff; --accent2:#7ef0c0;
    --warn:#ffcc66; --err:#ff6b81;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif;background:
      radial-gradient(1200px 600px at 10% -10%, #14213d 0%, transparent 60%), var(--bg);color:var(--text);min-height:100vh}
  header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
  .brand{display:flex;gap:12px;align-items:center}
  .logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#4da3ff,#7ef0c0);display:grid;place-items:center;font-weight:800;color:#06101f}
  h1{font-size:17px;margin:0;letter-spacing:.3px}
  .sub{color:var(--muted);font-size:12px}
  .badges{display:flex;gap:8px;flex-wrap:wrap}
  .badge{font-size:11px;padding:4px 9px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--muted)}
  .badge.on{color:#06101f;background:var(--accent2);border-color:transparent;font-weight:700}
  .badge.off{color:#ffd9de;background:#3a1220;border-color:#5d1e30}
  .badge.info{color:#cfe0ff;background:#111c31;border-color:#28466f}
  main{display:grid;grid-template-columns:1.35fr .95fr;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
  @media (max-width:980px){main{grid-template-columns:1fr}}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:14px}
  .card h2{font-size:13px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.12em}
  #log{height:430px;overflow:auto;display:flex;flex-direction:column;gap:10px;padding-right:4px}
  .msg{border:1px solid var(--line);border-radius:12px;padding:10px 12px;white-space:pre-wrap;line-height:1.5;font-size:13.5px}
  .msg.user{background:#0f1a2f;border-color:#22406b}
  .msg.bot{background:#0b1524}
  .msg.sys{background:#0e1526;color:var(--muted);font-size:12px;font-family:ui-monospace,Menlo,monospace}
  .who{font-size:11px;color:var(--muted);margin-bottom:5px;letter-spacing:.08em;text-transform:uppercase}
  .row{display:flex;gap:8px;margin-top:12px}
  input,button,textarea{font:inherit}
  input[type=text]{flex:1;background:#0a1120;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:12px}
  button{background:linear-gradient(135deg,#4da3ff,#7ef0c0);border:none;color:#06101f;font-weight:700;border-radius:10px;padding:12px 16px;cursor:pointer}
  button.ghost{background:#111c31;color:var(--text);border:1px solid var(--line);font-weight:600}
  button:disabled{opacity:.55;cursor:progress}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .chip{font-size:11.5px;padding:6px 10px;border-radius:999px;background:#101b2f;border:1px solid var(--line);color:#bcd0f0;cursor:pointer}
  .chip:hover{border-color:var(--accent)}
  pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;color:#cfe0ff}
  .kv{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;padding:5px 0;border-bottom:1px dashed #1b2740}
  .kv span:last-child{color:var(--muted)}
  .node{display:flex;gap:8px;align-items:center;font-size:12px;padding:5px 0;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
  .dot.done{background:var(--accent2)}
  .dot.err{background:var(--err)}
  .muted{color:var(--muted);font-size:12px}
  .err{color:var(--err)}
  .approval{position:fixed;inset:0;background:#030712cc;display:grid;place-items:center;padding:16px;z-index:20}
  .approval-card{width:min(680px,100%);background:var(--panel);border:1px solid var(--warn);border-radius:16px;padding:18px;box-shadow:0 18px 80px #0009}
  .approval-card pre{max-height:180px;overflow:auto;background:#070d19;padding:10px;border-radius:8px;margin:10px 0}
  .approval-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:14px}
  a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">NJ</div>
    <div>
      <h1>NikhilAI Jarvis — Console</h1>
      <div class="sub">LangGraph supervisor • DSPy reasoning • browser-use agents • self-healing code engine</div>
    </div>
  </div>
  <div class="badges" id="badges"></div>
</header>

<section id="auth-panel" class="card" hidden style="max-width:760px;margin:16px auto">
  <h2>API access</h2>
  <div class="muted" id="auth-message">This server requires an API token.</div>
  <div class="row">
    <input id="api-token" type="password" autocomplete="current-password" placeholder="NIKHILAI_API_TOKEN"/>
    <button id="unlock">Unlock</button>
  </div>
</section>

<section id="approval-modal" class="approval" hidden role="dialog" aria-modal="true" aria-labelledby="approval-title">
  <div class="approval-card">
    <h2 id="approval-title">Owner approval required</h2>
    <div class="muted" id="approval-why"></div>
    <pre id="approval-command"></pre>
    <div class="muted" id="approval-cwd"></div>
    <div class="approval-actions">
      <button id="approval-cancel" class="ghost">Cancel</button>
      <button id="approval-run">Run once</button>
    </div>
  </div>
</section>

<main>
  <section class="card">
    <h2>Chat / Command</h2>
    <div id="log"></div>
    <div class="row">
      <input id="q" type="text" placeholder="jarvis, alag alag website se laptop prices laao aur laptops naam se notes me save kardo"/>
      <button id="send" disabled>Run</button>
      <button id="cancel" class="ghost" disabled>Cancel</button>
    </div>
    <div class="chips">
      <div class="chip">notepad me meeting_notes naam se save kardo: client call 5 baje</div>
      <div class="chip">ek script likho jo csv report bana de</div>
      <div class="chip">browser se laptop ke top 5 price laao aur laptops naam se save kardo</div>
      <div class="chip">yaad rakhna mera client Ramesh hai</div>
      <div class="chip">tum kaun ho?</div>
    </div>
  </section>

  <section class="card">
    <h2>Runtime</h2>
    <div id="status"></div>
    <h2 style="margin-top:14px">Live runtime events</h2>
    <div id="trace"><div class="muted">Koi run nahi hua abhi.</div></div>
    <h2 style="margin-top:14px">Automation deliveries</h2>
    <div id="automation-runs"><div class="muted">Abhi tak koi scheduled result nahi.</div></div>
    <h2 style="margin-top:14px">Notes</h2>
    <div id="notes"><div class="muted">—</div></div>
    <h2 style="margin-top:14px">API</h2>
    <div class="muted">
      POST <code>/chat</code> · POST <code>/task</code> · GET <code>/stream</code> ·
      GET <code>/health</code> · GET <code>/tools</code> · GET <code>/graph</code>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let busy = false;
let activeStream = null;
let apiAuthReady = false;
let activeApproval = null;

function bubble(kind, who, text){
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  const label = document.createElement('div'); label.className = 'who'; label.textContent = who;
  const body = document.createElement('div'); body.textContent = String(text ?? '');
  div.append(label, body); $('log').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
  return div;
}
function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function setBusy(on){
  busy = on; $('send').disabled = on || !apiAuthReady; $('cancel').disabled = !on;
}
async function initAuth(){
  const panel = $('auth-panel');
  try{
    const r = await fetch('./auth/status');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    apiAuthReady = !!d.authorized;
    if (!d.required || apiAuthReady){
      panel.hidden = true; setBusy(false); loadStatus(); loadNotes(); loadAutomations(); return;
    }
    panel.hidden = false;
    $('auth-message').textContent = d.auth_possible
      ? 'Enter NIKHILAI_API_TOKEN. The browser keeps it only for this login session; it is not placed in the URL.'
      : 'This server is bound beyond loopback but NIKHILAI_API_TOKEN is not configured. Requests are fail-closed.';
    setBusy(false);
  }catch(e){
    panel.hidden = false; $('auth-message').textContent = 'Could not check API access: ' + String(e);
    apiAuthReady = false; setBusy(false);
  }
}
async function unlockApi(){
  const button = $('unlock'); button.disabled = true;
  try{
    const r = await fetch('./auth', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({token: $('api-token').value})});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    apiAuthReady = true; $('api-token').value = ''; $('auth-panel').hidden = true;
    setBusy(false); loadStatus(); loadNotes(); loadAutomations();
  }catch(e){ $('auth-message').textContent = 'Unlock failed: ' + String(e); }
  finally{ button.disabled = false; }
}
async function loadStatus(){
  try{
    const r = await fetch('./health'); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const b = $('badges'); b.innerHTML = '';
    const addBadge = (label, tone='info') => {
      const el = document.createElement('div'); el.className = 'badge ' + tone;
      el.textContent = label; b.appendChild(el);
    };
    addBadge('Interface: ' + (d.interface || 'API/server'));
    addBadge('Readiness: ' + (d.status || 'unknown'), d.offline_demo ? 'off' : 'info');
    addBadge('Provider: ' + d.provider + ' • ' + (d.provider_status || 'connectivity not tested'));
    addBadge('Browser: ' + (d.browser || 'unknown'), d.browser_use ? 'info' : 'off');
    addBadge('LiveKit: ' + (d.livekit_status || 'unknown'), d.livekit ? 'info' : 'off');
    addBadge('Telegram: ' + (d.telegram_status || 'unknown'), d.telegram ? 'info' : 'off');
    const s = $('status'); s.innerHTML = '';
    const rows = [
      ['Operator / Owner', d.owner], ['Assistant', d.assistant], ['Interface', d.interface],
      ['Readiness', d.status], ['Provider', d.provider], ['Configured provider', d.configured_provider],
      ['Brain model', d.model], ['Fast model', d.fast_model], ['Provider status', d.provider_status],
      ['Tools', d.tools + ' schemas registered; per-interface permissions apply'],
      ['Browser', d.browser], ['Voice (LiveKit)', d.livekit_status], ['Telegram', d.telegram_status],
      ['Memory facts / episode records', (d.memory?.facts ?? 0) + ' / ' + (d.memory?.episodes ?? 0)],
      ['Notes indexed', d.memory?.notes ?? 0], ['Automations', d.automations], ['Workspace', d.workspace],
      ['Connectivity', d.connectivity_tested ? 'tested' : 'not tested by status endpoint']
    ];
    for (const [k, v] of rows){
      const div = document.createElement('div'); div.className = 'kv';
      const key = document.createElement('span'); key.textContent = k;
      const value = document.createElement('span'); value.textContent = String(v ?? '—');
      div.append(key, value); s.appendChild(div);
    }
  }catch(e){ bubble('sys', 'status error', String(e)); }
}
async function loadNotes(){
  try{
    const r = await fetch('./notes'); const d = await r.json();
    const el = $('notes'); el.innerHTML = '';
    if (!d.notes || !d.notes.length){ el.innerHTML = '<div class="muted">Abhi koi note nahi.</div>'; return; }
    for (const n of d.notes.slice(0, 8)){
      const div = document.createElement('div'); div.className = 'node';
      const dot = document.createElement('span'); dot.className = 'dot';
      const label = document.createElement('span'); label.textContent = `${n.name} ${n.size || ''}B`;
      div.append(dot, label); el.appendChild(div);
    }
  }catch(e){}
}
async function loadAutomations(){
  if (!apiAuthReady) return;
  try{
    const r = await fetch('./automations'); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); const root = $('automation-runs'); root.replaceChildren();
    const runs = [...(d.runs || [])].reverse().slice(0, 8);
    if (!runs.length){
      const empty = document.createElement('div'); empty.className = 'muted';
      empty.textContent = d.load_warning || 'Abhi tak koi scheduled result nahi.'; root.appendChild(empty); return;
    }
    for (const item of runs){
      const row = document.createElement('div'); row.className = 'node';
      const dot = document.createElement('span'); dot.className = 'dot';
      const body = document.createElement('span');
      const progress = (item.progress || []).slice(-3).map(p => p.text || p.kind || 'progress').join(' → ');
      body.textContent = `#${item.automation_id || ''} • ${item.surface || 'unknown'} • ${item.status || 'unknown'} • ${progress} • ${item.result || item.error || ''}`;
      row.append(dot, body); root.appendChild(row);
    }
  }catch(e){
    const root = $('automation-runs'); root.textContent = 'Automation status unavailable: ' + String(e);
  }
}
async function pollApprovals(){
  if (!apiAuthReady || activeApproval) return;
  try{
    const r = await fetch('./approvals'); if (!r.ok) return;
    const d = await r.json(); const item = (d.approvals || [])[0];
    if (!item) return;
    activeApproval = item;
    $('approval-why').textContent = item.why_flagged || 'This action needs owner approval.';
    $('approval-command').textContent = item.command || '';
    $('approval-cwd').textContent = 'Working directory: ' + (item.cwd || 'unknown');
    $('approval-modal').hidden = false;
  }catch(e){}
}
async function answerApproval(allow){
  if (!activeApproval) return;
  const item = activeApproval;
  $('approval-run').disabled = true; $('approval-cancel').disabled = true;
  try{
    const r = await fetch('./approvals/' + encodeURIComponent(item.id), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({allow:!!allow})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    $('approval-modal').hidden = true;
    bubble('sys', 'approval', allow ? 'Owner approved this exact action once.' : 'Owner cancelled; no command ran.');
    activeApproval = null;
  }catch(e){
    $('approval-why').textContent = 'Approval failed or expired: ' + String(e);
    activeApproval = null; $('approval-modal').hidden = true;
  }finally{
    $('approval-run').disabled = false; $('approval-cancel').disabled = false;
    pollApprovals();
  }
}
function traceStep(kind, detail, status=''){
  const el = $('trace');
  if (el.querySelector('.muted')) el.innerHTML = '';
  const div = document.createElement('div'); div.className = 'node';
  const dot = document.createElement('span'); dot.className = 'dot';
  if (status === 'ok' || kind === 'done') dot.classList.add('done');
  if (status === 'fail' || kind === 'error') dot.classList.add('err');
  const title = document.createElement('b'); title.textContent = kind;
  const text = document.createElement('span'); text.className = 'muted'; text.textContent = detail || '';
  div.append(dot, title, text); el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}
function stopStream(message='Cancelled by user.'){
  if (activeStream){ activeStream.close(); activeStream = null; }
  if (busy){ bubble('sys', 'cancelled', message); setBusy(false); }
}
function run(){
  const message = $('q').value.trim();
  if (!message || busy) return;
  setBusy(true);
  bubble('user', 'Owner (Nikhil Mehra)', message);
  $('q').value = '';
  const waiting = bubble('sys', 'runtime', 'Request accepted; waiting for actual runtime events…');
  $('trace').innerHTML = '<div class="muted">Waiting for server events…</div>';
  const params = new URLSearchParams({message, session_id:'web'});
  const stream = new EventSource('./stream?' + params.toString());
  activeStream = stream;
  let resultReceived = false;
  const parse = ev => { try { return JSON.parse(ev.data || '{}'); } catch { return {}; } };
  const close = () => {
    stream.close(); if (activeStream === stream) activeStream = null;
    setBusy(false);
  };
  stream.addEventListener('start', ev => {
    const d = parse(ev); waiting.querySelector('div:last-child').textContent =
      'Request started on ' + (d.interface || 'server') + '; progress will appear only when emitted.';
    traceStep('request_start', d.interface || 'API/server', 'ok');
  });
  stream.addEventListener('progress', ev => {
    const d = parse(ev); const label = d.text || d.tool || 'runtime event';
    const plan = d.kind === 'plan' && d.detail
      ? ' — ' + d.detail.split(' → ').map((step, index) => `${index + 1}. ${step.trim()}`).join(' · ')
      : '';
    waiting.querySelector('div:last-child').textContent = label + plan;
    traceStep(d.kind || 'progress', label + plan + (d.ms ? ` • ${d.ms}ms` : ''), d.status || '');
  });
  stream.addEventListener('heartbeat', ev => {
    const d = parse(ev);
    waiting.querySelector('div:last-child').textContent =
      `Still processing • ${d.elapsed_seconds || 0}s • no new runtime event`;
  });
  stream.addEventListener('result', ev => {
    const d = parse(ev); resultReceived = true; waiting.remove();
    const meta = [`status: ${d.status || 'unknown'}`];
    if (d.workers?.length) meta.push('workers: ' + d.workers.join(', '));
    if (d.memory_updates?.length) meta.push('memory updates: ' + d.memory_updates.join(', '));
    if (d.artifacts?.length) meta.push('artifacts: ' + d.artifacts.length);
    if (d.memory_persisted) meta.push('turn memory persisted');
    else meta.push('memory persistence not confirmed');
    if (d.execution_required) meta.push(d.verified_execution ? 'tool evidence verified' : 'tool completion not verified');
    if (d.seconds != null) meta.push(d.seconds + 's');
    bubble('bot', 'Jarvis', (d.answer || '(no answer returned)') + '\n\n— ' + meta.join(' • '));
    if (d.errors?.length) bubble('sys', 'errors', d.errors.join('\n'));
    const terminal = (d.status || 'unknown').toLowerCase();
    const failedStates = new Set(['failed','error','blocked','cancelled','partial_failure','done_with_errors']);
    const successfulStates = new Set(['done','chat','memory','settings','time']);
    const terminalTone = failedStates.has(terminal) ? 'fail'
      : terminal === 'needs_clarification' ? 'skipped'
      : successfulStates.has(terminal) ? 'ok' : '';
    traceStep('result', 'terminal status: ' + terminal, terminalTone);
    loadNotes(); loadStatus(); close();
  });
  stream.addEventListener('error', ev => {
    if (ev.data){
      const d = parse(ev); bubble('sys', 'server error', d.error || 'Server reported an error.');
      traceStep('error', d.error || 'server error', 'fail'); waiting.remove();
    }else if (!resultReceived && stream.readyState === EventSource.CLOSED){
      waiting.remove(); bubble('sys', 'connection error', 'Stream closed before a final result arrived.');
      traceStep('connection_error', 'no final result received', 'fail'); close();
    }
  });
  stream.addEventListener('end', ev => {
    const d = parse(ev);
    if (!resultReceived){
      waiting.remove(); bubble('sys', 'incomplete', 'Stream ended without a final result.');
      traceStep('incomplete', JSON.stringify(d), 'fail');
    }
    close();
  });
}
$('send').onclick = run;
$('cancel').onclick = () => stopStream('Cancel requested; live stream closed. Server cancellation is best-effort.');
$('unlock').onclick = unlockApi;
$('approval-run').onclick = () => answerApproval(true);
$('approval-cancel').onclick = () => answerApproval(false);
$('q').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
$('api-token').addEventListener('keydown', e => { if (e.key === 'Enter') unlockApi(); });
document.querySelectorAll('.chip').forEach(c => c.onclick = () => { $('q').value = c.textContent; run(); });
setInterval(pollApprovals, 1000);
setInterval(loadAutomations, 10000);
initAuth();
</script>
</body>
</html>
"""


def main() -> int:
    if not fastapi_available:
        print("⚠️ FastAPI/uvicorn install karo: pip install fastapi uvicorn")
        return 1
    import uvicorn

    print(f"\n🤖 {ASSISTANT_NAME} console → http://{API_HOST}:{API_PORT}\n")
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
