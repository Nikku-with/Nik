from __future__ import annotations


def test_browser_alias_resolves_to_a_browser_candidate(monkeypatch):
    from jarvis.tools import app_finder

    candidates = [
        {
            "name": "Google Chrome",
            "norm": "google chrome",
            "path": "desktop:google-chrome",
            "kind": "desktop",
            "extra": "",
        },
        {
            "name": "ImageMagick",
            "norm": "imagemagick",
            "path": "desktop:imagemagick",
            "kind": "desktop",
            "extra": "image viewer",
        },
    ]
    monkeypatch.setattr(app_finder, "build_app_index", lambda force=False: candidates)

    matches = app_finder.find_apps("browser")

    assert matches
    assert matches[0]["name"] == "Google Chrome"
    assert "imagemagick" not in {match["norm"] for match in matches}


def test_browser_query_never_returns_non_browser_fuzzy_hits(monkeypatch):
    from jarvis.tools import app_finder

    candidates = [
        {"name": "ImageMagick", "norm": "imagemagick", "path": "desktop:imagemagick", "kind": "desktop",
         "extra": "browser compatible image viewer"},
        {"name": "Chromium Updater", "norm": "chromium updater", "path": "desktop:chromium-updater",
         "kind": "desktop", "extra": "update service"},
        {"name": "Google Chrome", "norm": "google chrome", "path": "desktop:google-chrome", "kind": "desktop",
         "extra": ""},
        {"name": "Browser Test Harness", "norm": "browser test harness", "path": "desktop:browser-test",
         "kind": "desktop", "extra": ""},
        {"name": "Mozilla Firefox", "norm": "mozilla firefox", "path": "desktop:firefox", "kind": "desktop",
         "extra": ""},
    ]
    monkeypatch.setattr(app_finder, "build_app_index", lambda force=False: candidates)
    matches = app_finder.find_apps("browser")
    assert {match["norm"] for match in matches} == {"google chrome", "mozilla firefox"}
