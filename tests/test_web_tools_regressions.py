from __future__ import annotations

from datetime import timedelta

import pytest


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = chunks
        self.encoding = "utf-8"
        self.text = "example page"
        self.stream_opened = False
        self.received_bytes = 0

    async def __aenter__(self):
        self.stream_opened = True
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.received_bytes += len(chunk)
            yield chunk


class FakeClient:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return self.response

    def stream(self, method, url):
        self.calls.append((method, url))
        return self.response


@pytest.mark.asyncio
async def test_httpbin_bare_domain_gets_a_real_http_scheme(monkeypatch):
    from jarvis.tools import web_tools

    response = FakeResponse([b"example page"])
    calls = []
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", lambda **_kwargs: FakeClient(response, calls))

    await web_tools.fetch_url_text("httpbin.org")

    assert calls[0][1] == "https://httpbin.org"


@pytest.mark.asyncio
async def test_page_fetch_streams_and_stops_after_the_response_byte_cap(monkeypatch):
    from jarvis.tools import web_tools

    chunk = b"x" * (1024 * 1024)
    response = FakeResponse([chunk, chunk, chunk])
    calls = []
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", lambda **_kwargs: FakeClient(response, calls))

    text = await web_tools.fetch_url_text("https://example.test", max_chars=100)

    assert response.stream_opened is True
    assert response.received_bytes == 2 * 1024 * 1024
    assert len(text) <= 100


def test_timezone_falls_back_to_fixed_ist_without_tzdata(monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError

    from jarvis import timezone as timezone_module

    def unavailable(_name):
        raise ZoneInfoNotFoundError("tzdata is not installed")

    monkeypatch.setattr(timezone_module, "ZoneInfo", unavailable)

    zone = timezone_module.configured_timezone()

    assert zone.utcoffset(None) == timedelta(hours=5, minutes=30)
