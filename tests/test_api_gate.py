from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.security import gate


class FakeRequest:
    async def json(self):
        return {"allow": True}


@pytest.mark.asyncio
async def test_api_pending_approval_is_owner_resolvable_once(monkeypatch):
    from jarvis.api import server
    from jarvis.config import settings as settings_module

    broker = gate.ApprovalBroker(owner_id="owner-7", ttl_s=1)
    monkeypatch.setattr(gate, "BROKER", broker)
    monkeypatch.setattr(settings_module, "OWNER_ID", "owner-7")
    monkeypatch.setattr(settings_module, "TELEGRAM_OWNER_ID", "")
    emitted = asyncio.Event()
    request_holder = []

    async def notify(req):
        request_holder.append(req)
        emitted.set()
        return None

    gate.register_approval_handler("api", notify)
    try:
        pending_task = asyncio.create_task(
            broker.ask("rm -rf /tmp/x", "/tmp", "recursive delete", "api")
        )
        await asyncio.wait_for(emitted.wait(), 0.5)
        req = request_holder[0]

        pending_endpoint = next(r.endpoint for r in server.app.routes
                                if getattr(r, "path", "") == "/approvals")
        pending_response = await pending_endpoint()
        listed = json.loads(pending_response.body)
        assert listed["approvals"][0]["id"] == req.id
        assert listed["approvals"][0]["command"] == req.command

        resolve_endpoint = next(r.endpoint for r in server.app.routes
                                if getattr(r, "path", "") == "/approvals/{request_id}")
        response = await resolve_endpoint(req.id, FakeRequest())
        assert json.loads(response.body)["ok"] is True
        assert (await pending_task).allowed
        denied_replay = await broker.resolve(req.id, True, "owner-7", surface="api")
        assert denied_replay is False
    finally:
        gate.register_approval_handler("api", None)


@pytest.mark.asyncio
async def test_api_automation_result_is_exposed_to_origin_surface():
    from jarvis.api import server

    event = {"automation_id": "auto-api", "run_id": "run-api-1", "surface": "api",
             "task": "nightly report", "result": "report ready", "status": "done"}
    server._deliver_api_automation(event)
    endpoint = next(route.endpoint for route in server.app.routes
                    if getattr(route, "path", "") == "/automations/events")
    response = await endpoint()
    payload = json.loads(response.body)
    assert payload["events"][-1]["run_id"] == "run-api-1"
    assert payload["events"][-1]["result"] == "report ready"


@pytest.mark.asyncio
async def test_api_gate_wrong_user_cannot_resolve(monkeypatch):
    broker = gate.ApprovalBroker(owner_id="actual-owner", ttl_s=0.5)
    event = asyncio.Event()
    requests = []

    async def notify(req):
        requests.append(req)
        event.set()
        return None

    gate.register_approval_handler("api", notify)
    try:
        task = asyncio.create_task(broker.ask("rm -rf x", "/tmp", "delete", "api"))
        await asyncio.wait_for(event.wait(), 0.3)
        req = requests[0]
        assert not await broker.resolve(req.id, True, "attacker", surface="api")
        assert await broker.resolve(req.id, False, "actual-owner", surface="api")
        assert not (await task).allowed
    finally:
        gate.register_approval_handler("api", None)
