from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_docs_layout_and_readme_links():
    expected = {
        "docs/ARCHITECTURE.md",
        "docs/SETUP_APIS.md",
        "docs/STATUS.md",
        "docs/FEATURES_REQUIREMENTS_ENVIRONMENT.md",
        "CHANGELOG.md",
    }
    assert all((ROOT / path).is_file() for path in expected)

    obsolete_root_docs = (
        "ARCHITECTURE.md",
        "CAPABILITIES.md",
        "FINAL_STATUS.md",
        "SETUP_APIS.md",
        "TOOL_AUDIT.md",
    )
    assert all(not (ROOT / path).exists() for path in obsolete_root_docs)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for path in expected:
        assert f"]({path})" in readme


def test_local_markdown_links_resolve_in_canonical_docs():
    markdown_files = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")

    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(text):
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            local_path = source.parent / unquote(parsed.path)
            assert local_path.exists(), f"broken local link in {source.relative_to(ROOT)}: {target}"


def test_tool_audit_no_longer_advertises_removed_root_report():
    source = (ROOT / "scripts" / "audit_tools.py").read_text(encoding="utf-8")
    assert "TOOL_AUDIT.md" not in source
    assert "--report <path>" in source
