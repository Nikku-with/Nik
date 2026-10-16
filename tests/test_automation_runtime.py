from __future__ import annotations

import json
from datetime import timedelta

from jarvis.automation import scheduler as sch
from jarvis.security.permissions import use_surface


def test_schedule_parser_is_kota_timezone_aware_and_validates_ranges(monkeypatch):
    monkeypatch.setattr(sch.S, "TIMEZONE", "Asia/Kolkata")
    cases = {
        "har subah 8 baje": ("daily", "daily 08:00"),
        "daily 18:30": ("daily", "daily 18:30"),
        "every 2h": ("interval", "every 2 h"),
        "har 30 minute": ("interval", "every 30 min"),
        "every 2 days": ("interval", "every 2 d"),
        "kal 6 PM": ("once", "once "),
    }
    for text, (kind, schedule) in cases.items():
        result = sch.parse_schedule(text)
        assert result["kind"] == kind
        assert result["schedule"].startswith(schedule)
    assert sch.parse_schedule("daily 25:99")["kind"] == ""
    assert sch._timezone().key == "Asia/Kolkata"
    monkeypatch.setattr(sch.S, "TIMEZONE", "not/a-timezone")
    assert sch._timezone().key == "Asia/Kolkata"

    tomorrow = sch.parse_schedule("kal 6 PM")
    assert sch._from_timestamp(tomorrow["at"]).hour == 18
    assert sch._from_timestamp(tomorrow["at"]).date() == (sch._now() + timedelta(days=1)).date()


def test_automation_store_atomic_backup_and_corruption_recovery(tmp_path):
    path = tmp_path / "automations.json"
    store = sch.AutomationStore(path)
    first = store.create("weather at breakfast", "har subah 8 baje", channel="telegram_owner")
    assert first["ok"]
    second = store.create("daily headlines", "daily 09:00", channel="api")
    assert second["ok"]
    backup = path.with_name(path.name + ".bak")
    assert backup.exists()
    backup_data = json.loads(backup.read_text(encoding="utf-8"))
    assert len(backup_data["automations"]) == 1

    path.write_text("{truncated", encoding="utf-8")
    recovered = sch.AutomationStore(path)
    assert len(recovered.list()) == 1
    assert "unreadable" in recovered.load_error and "backup" in recovered.load_error
    recovered.create("report", "every 2h", channel="voice_owner")
    assert len(json.loads(path.read_text(encoding="utf-8"))["automations"]) == 2
    # Recovery never copies a known-corrupt primary over the last good backup.
    assert len(json.loads(backup.read_text(encoding="utf-8"))["automations"]) == 1


def test_automation_save_failure_keeps_primary_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "automations.json"
    store = sch.AutomationStore(path)
    first = store.create("one", "every 2h")
    assert first["ok"]
    original = path.read_bytes()
    real_replace = sch.os.replace

    def fail_primary_replace(src, dst):
        if str(dst) == str(path):
            raise OSError("simulated atomic replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(sch.os, "replace", fail_primary_replace)
    failed = store.create("two", "every 3h")
    assert not failed["ok"]
    assert "persistence failed" in store.load_error
    assert path.read_bytes() == original
    assert len(store.list()) == 1


def test_automation_origin_and_delivery_are_preserved_for_each_surface(tmp_path, monkeypatch):
    store = sch.AutomationStore(tmp_path / "automations.json")
    monkeypatch.setattr(sch, "_STORE", store)
    deliveries = {name: [] for name in ("cli_owner", "api", "telegram_owner", "voice_owner")}
    for surface in deliveries:
        sch.register_delivery_handler(surface, deliveries[surface].append)

    def runner(task, automation_id, run_id, origin_surface):
        return {"answer": f"verified: {task}", "status": "done", "errors": [],
                "execution_required": True, "verified_execution": True,
                "automation_id": automation_id, "run_id": run_id, "surface": origin_surface}

    monkeypatch.setattr(sch, "RUNNER", runner)
    try:
        for surface in deliveries:
            with use_surface(surface):
                created = store.create(f"task for {surface}", "every 2h")
            auto = created["automation"]
            assert auto["channel"] == surface
            result = sch.run_now(auto["id"])
            assert result["ok"] is True
            assert result["run_id"]
            assert len(deliveries[surface]) == 1
            event = deliveries[surface][0]
            assert event["surface"] == surface
            assert event["automation_id"] == auto["id"]
            assert event["run_id"] and event["run_id"] != auto["id"]
            stored = store.get(auto["id"])
            assert stored.last_run_id == event["run_id"]
            assert stored.last_surface == surface and stored.last_status == "done"
    finally:
        for surface in deliveries:
            sch.register_delivery_handler(surface, None)


def test_scheduler_captures_live_progress_with_run_id_and_origin_surface(tmp_path, monkeypatch):
    from jarvis.runtime import agent
    from jarvis.runtime.progress import ProgressEvent

    store = sch.AutomationStore(tmp_path / "automations.json")
    monkeypatch.setattr(sch, "_STORE", store)
    monkeypatch.setattr(sch, "_RUN_HISTORY", sch.deque(maxlen=200))
    monkeypatch.setattr(sch, "_RUN_PROGRESS", {})

    async def fake_run_agent(task, *, session_id, surface, run_id, on_progress=None):
        assert session_id.startswith("automation:")
        assert surface == "api"
        assert run_id
        on_progress(ProgressEvent(kind="plan", text="Plan created"))
        on_progress(ProgressEvent(kind="tool_end", text="Search verified", tool="web_search", status="ok"))
        return {"answer": "verified result", "status": "done", "errors": [],
                "execution_required": False, "verified_execution": True}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    runner = sch.configure_agent_runner()
    monkeypatch.setattr(sch, "RUNNER", runner)
    created = store.create("research this", "every 2h", channel="api")
    outcome = sch.run_now(created["automation"]["id"])

    assert outcome["ok"] and outcome["run_id"]
    assert [item["text"] for item in outcome["progress"]] == ["Plan created", "Search verified"]
    event = next(item for item in reversed(sch.recent_runs("api"))
                 if item.get("run_id") == outcome["run_id"])
    assert event["status"] == "done" and event["surface"] == "api"
    assert event["progress"] == outcome["progress"]
    assert outcome["run_id"] != created["automation"]["id"]


async def test_cli_automation_run_displays_captured_progress(monkeypatch):
    from jarvis.cli import app as cli

    displayed = []
    monkeypatch.setattr(
        sch,
        "run_now",
        lambda _automation_id: {
            "ok": True,
            "message": "Automation completed",
            "progress": [{"kind": "plan", "text": "Plan created"},
                         {"kind": "tool_end", "text": "Search verified"}],
        },
    )
    monkeypatch.setattr(cli.ui, "cprint_safe", lambda text, *_args: displayed.append(text))

    assert await cli.handle_command("/automation run auto-1", "cli-test") is True
    assert any("Plan created" in line for line in displayed)
    assert any("Search verified" in line for line in displayed)
    assert any("Automation completed" in line for line in displayed)


def test_scheduled_agent_runs_gate_on_stored_origin_surface(monkeypatch):
    from types import SimpleNamespace

    from jarvis.runtime import agent
    from jarvis.security import gate

    broker = gate.ApprovalBroker(owner_id="owner-9", ttl_s=0.3)
    monkeypatch.setattr(gate, "BROKER", broker)
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    approvals = []

    async def human_approval(request):
        approvals.append(request)
        return True

    gate.register_approval_handler("telegram_owner", human_approval)

    async def fake_run_agent(task, *, session_id, surface, run_id, on_progress=None):
        result = await gate.authorize_command("rm -rf /tmp/owner-data", "/tmp", surface=surface)
        if on_progress:
            from jarvis.runtime.progress import ProgressEvent

            on_progress(ProgressEvent(kind="tool_end", text="dangerous command approved", tool="run_shell_command",
                                      status="ok"))
        return SimpleNamespace(
            answer="ran" if result.allowed else result.reason,
            status="done" if result.allowed else "blocked",
            errors=[] if result.allowed else [result.reason],
            execution_required=True, verified_execution=result.allowed,
            run_id=run_id, as_dict=lambda: {
                "answer": "ran" if result.allowed else result.reason,
                "status": "done" if result.allowed else "blocked",
                "errors": [] if result.allowed else [result.reason],
                "execution_required": True, "verified_execution": result.allowed,
                "run_id": run_id, "task": task, "session_id": session_id,
            },
        )

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    previous_runner = sch.RUNNER
    try:
        runner = sch.configure_agent_runner()
        outcome = runner("scheduled dangerous action", "auto-9", "run-9", "telegram_owner")
        assert outcome["verified_execution"] is True
        assert approvals and approvals[0].surface == "telegram_owner"
        assert approvals[0].command == "rm -rf /tmp/owner-data"
    finally:
        sch.RUNNER = previous_runner
        gate.register_approval_handler("telegram_owner", None)
