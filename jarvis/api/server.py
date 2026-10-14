"""
jarvis/api/server.py — FastAPI server + built-in web console
===========================================================

Endpoints:
  GET  /                → web console (single-file HTML, inline CSS/JS — preview-safe)
  POST /chat            → {message, session_id} → answers (LangGraph supervisor)
  POST /task            → raw graph run + poora state JSON
  GET  /stream?message= → Server-Sent Events: live node-by-node progress + thinking feed
  GET  /health          → provider / tools / browser / memory status
  GET  /notes           → saved notes list
  GET  /graph           → mermaid + ASCII architecture
  GET  /tools           → tool catalogue (kaun kaun se tool wired hain)

Run:
  python -m jarvis.api.server            # 0.0.0.0:8000
  uvicorn jarvis.api.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..config import API_HOST, API_PORT, ASSISTANT_NAME, OFFLINE_MODE, OWNER_NAME, PROVIDER, WORKSPACE_DIR
from ..graph.build import graph_mermaid, graph_summary, jarvis_stream
from ..memory.store import get_memory
from ..runtime.agent import run_agent

try:
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    fastapi_available = True
except Exception:  # pragma: no cover
    fastapi_available = False
    FastAPI = None  # type: ignore
    HTMLResponse = JSONResponse = StreamingResponse = None  # type: ignore


def _web_asker(req) -> str:
    """Web UI asker — approval modal se resolve hota hai (event), warna timeout par no."""
    from ..security.gate import get_broker

    broker = get_broker()
    ev = broker._events.get(req.id)
    if ev is not None:
        ev.wait(timeout=req.timeout_s)
        if req.status == "approved":
            return "yes"
    return "no"


def _status() -> dict[str, Any]:
    from ..config import has_browser_use, has_browser_use_cli, has_livekit

    return {
        "assistant": ASSISTANT_NAME,
        "owner": OWNER_NAME,
        "provider": PROVIDER,
        "offline_demo": bool(OFFLINE_MODE or PROVIDER == "demo"),
        "browser_use": has_browser_use(),
        "browser_use_cli": has_browser_use_cli(),
        "livekit": has_livekit(),
        "workspace": str(WORKSPACE_DIR),
        "memory": get_memory().stats(),
    }


if fastapi_available:
    app = FastAPI(title=f"{ASSISTANT_NAME} — NikhilAI ", version="2.0")

    from ..security.gate import gate_context as _gate_ctx
    from ..security.gate import get_broker as _get_broker
    from ..security.gate import register_asker as _register_asker

    _register_asker("api", _web_asker)
    _register_asker("web", _web_asker)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(_status())

    @app.post("/chat")
    async def chat(payload: dict) -> JSONResponse:
        message = (payload or {}).get("message", "").strip()
        session_id = (payload or {}).get("session_id", "web")
        if not message:
            return JSONResponse({"error": "message khali hai"}, status_code=400)
        started = time.time()
        with _gate_ctx("api"):
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
                "notes_saved": result.get("notes_saved", []),
                "artifacts": result.get("artifacts", []),
                "critique": result.get("critique", {}),
                "iterations": result.get("iterations", 0),
                "errors": result.get("errors", []),
                "seconds": round(time.time() - started, 2),
            }
        )

    @app.post("/task")
    async def task(payload: dict) -> JSONResponse:
        message = (payload or {}).get("task", "").strip()
        if not message:
            return JSONResponse({"error": "task khali hai"}, status_code=400)
        with _gate_ctx("api"):
            result = (await run_agent(message, session_id=(payload or {}).get("session_id", "api"),
                                      surface="api")).as_dict()
        safe = {k: v for k, v in result.items() if k not in ("messages",)}
        return JSONResponse(json.loads(json.dumps(safe, default=str)))

    @app.get("/stream")
    async def stream(message: str = Query(...), session_id: str = "web") -> StreamingResponse:
        async def gen():
            yield f"event: start\ndata: {json.dumps({'message': message})}\n\n"
            try:
                async for ev in jarvis_stream(message, session_id=session_id):
                    yield f"event: node\ndata: {json.dumps(ev, default=str)}\n\n"
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

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
        return JSONResponse({"stats": mgr.stats(), "memories": mgr.list_memories(limit=limit)})

    @app.post("/memory")
    async def memory_write(payload: dict) -> JSONResponse:
        """{action: remember|forget|update, text: str, id: str}"""
        from ..memory.manager import get_manager

        mgr = get_manager()
        action = (payload or {}).get("action", "remember")
        text = (payload or {}).get("text", "")
        if action == "remember":
            return JSONResponse(mgr.remember(text))
        if action == "forget":
            return JSONResponse(mgr.forget(text, all_matches=bool((payload or {}).get("all"))))
        if action == "update":
            return JSONResponse(mgr.update((payload or {}).get("id", ""), text))
        return JSONResponse({"ok": False, "message": f"unknown action '{action}'"})

    @app.get("/automations")
    async def automations() -> JSONResponse:
        from ..automation import scheduler as sch

        return JSONResponse({"status": sch.status(), "items": sch.get_store().list()})

    @app.get("/graph")
    async def graph() -> JSONResponse:
        return JSONResponse({"mermaid": graph_mermaid(), "summary": graph_summary()})

    @app.get("/approvals")
    async def approvals() -> JSONResponse:
        """Pending dangerous-command approvals (web modal inhe dikhata hai)."""
        broker = _get_broker()
        return JSONResponse({"pending": [r.to_dict() for r in broker.pending()],
                             "recent": broker.recent_decisions(10)})

    @app.post("/approvals/resolve")
    async def approvals_resolve(payload: dict) -> JSONResponse:
        """{id: str, approved: bool} — web modal se human decision."""
        broker = _get_broker()
        approval_id = str((payload or {}).get("id", ""))
        approved = bool((payload or {}).get("approved", False))
        ok = broker.resolve(approval_id, approved, by="web",
                            note="web modal se " + ("approved" if approved else "denied"))
        req = broker.get(approval_id)
        return JSONResponse({"ok": ok, "status": req.status if req else "unknown"})

    @app.get("/gate")
    async def gate_status() -> JSONResponse:
        from ..security.gate import effective_mode

        return JSONResponse({"mode": effective_mode(), "pending": len(_get_broker().pending())})

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
  .muted{color:var(--muted);font-size:12px}
  .err{color:var(--err)}
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

<main>
  <section class="card">
    <h2>Chat / Command</h2>
    <div id="log"></div>
    <div class="row">
      <input id="q" type="text" placeholder="jarvis, alag alag website se laptop prices laao aur laptops naam se notes me save kardo"/>
      <button id="send">Run</button>
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
    <h2 style="margin-top:14px">Live graph trace</h2>
    <div id="trace"><div class="muted">Koi run nahi hua abhi.</div></div>
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

function bubble(kind, who, text){
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  div.innerHTML = '<div class="who">' + who + '</div>' + escapeHtml(text);
  $('log').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
  return div;
}
function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function loadStatus(){
  try{
    const r = await fetch('./health'); const d = await r.json();
    const b = $('badges'); b.innerHTML = '';
    const items = [
      ['LLM: ' + d.provider, d.provider !== 'demo'],
      ['offline demo', !!d.offline_demo],
      ['browser-use', !!d.browser_use],
      ['browser-use CLI', !!d.browser_use_cli],
      ['livekit voice', !!d.livekit],
    ];
    for (const [label, on] of items){
      const el = document.createElement('div');
      el.className = 'badge ' + (on ? 'on' : 'off');
      el.textContent = label;
      b.appendChild(el);
    }
    const s = $('status'); s.innerHTML = '';
    const rows = [
      ['Operator / Owner', d.owner],
      ['Assistant', d.assistant],
      ['Provider', d.provider + (d.offline_demo ? ' (demo brain)' : '')],
      ['Browser-use API', d.browser_use ? '✅' : '❌'],
      ['Browser-use CLI', d.browser_use_cli ? '✅' : '❌'],
      ['Voice (LiveKit)', d.livekit ? '✅' : '—'],
      ['Memory facts/turns', (d.memory?.facts ?? 0) + ' / ' + (d.memory?.episodes ?? 0)],
      ['Notes indexed', d.memory?.notes ?? 0],
      ['Workspace', d.workspace],
    ];
    for (const [k, v] of rows){
      const div = document.createElement('div'); div.className = 'kv';
      div.innerHTML = '<span>' + escapeHtml(k) + '</span><span>' + escapeHtml(v) + '</span>';
      s.appendChild(div);
    }
  }catch(e){ console.error(e); }
}
async function loadNotes(){
  try{
    const r = await fetch('./notes'); const d = await r.json();
    const el = $('notes'); el.innerHTML = '';
    if (!d.notes || !d.notes.length){ el.innerHTML = '<div class="muted">Abhi koi note nahi.</div>'; return; }
    for (const n of d.notes.slice(0, 8)){
      const div = document.createElement('div'); div.className = 'node';
      div.innerHTML = '<span class="dot done"></span>' + escapeHtml(n.name) + ' <span class="muted">' + escapeHtml(n.size || '') + 'B</span>';
      el.appendChild(div);
    }
  }catch(e){}
}
function traceStep(node, detail){
  const el = $('trace');
  if (el.querySelector('.muted')) el.innerHTML = '';
  const div = document.createElement('div'); div.className = 'node';
  div.innerHTML = '<span class="dot"></span><b>' + escapeHtml(node) + '</b> <span class="muted">' + escapeHtml(detail || '') + '</span>';
  el.appendChild(div);
}
async function run(){
  const message = $('q').value.trim();
  if (!message || busy) return;
  busy = true; $('send').disabled = true;
  bubble('user', 'Owner (Nikhil Mehra)', message);
  $('q').value = '';
  const thinking = bubble('sys', 'supervisor', 'graph chal raha hai…');
  $('trace').innerHTML = '<div class="muted">running…</div>';

  try{
    const r = await fetch('./chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message, session_id:'web'})
    });
    const d = await r.json();
    thinking.remove();
    let out = d.answer || '(koi answer nahi)';
    const meta = [];
    if (d.workers?.length) meta.push('workers: ' + d.workers.join(', '));
    if (d.notes_saved?.length) meta.push('notes: ' + d.notes_saved.join(', '));
    if (d.artifacts?.length) meta.push('artifacts: ' + d.artifacts.length);
    if (d.iterations) meta.push('refine passes: ' + d.iterations);
    meta.push(d.seconds + 's');
    bubble('bot', 'Jarvis', out + '\\n\\n— ' + meta.join(' • '));

    if (d.findings?.length){
      for (const f of d.findings){
        bubble('sys', 'worker: ' + f.worker, String(f.summary).slice(0, 900));
      }
    }
    if (d.errors?.length) bubble('sys', 'errors', d.errors.join('\\n'));
    loadNotes(); loadStatus();

    // trace panel
    const el = $('trace'); el.innerHTML = '';
    for (const node of ['recall','route','plan', ...(d.workers||[]).map(w=>w+'_worker'),'synthesize','critic','persist','respond']){
      const div = document.createElement('div'); div.className='node';
      div.innerHTML = '<span class="dot done"></span>' + escapeHtml(node);
      el.appendChild(div);
    }
  }catch(e){
    thinking.remove();
    bubble('sys', 'error', 'Request failed: ' + e);
  }finally{
    busy = false; $('send').disabled = false;
  }
}
$('send').onclick = run;
$('q').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
document.querySelectorAll('.chip').forEach(c => c.onclick = () => { $('q').value = c.textContent; run(); });
loadStatus(); loadNotes();
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
