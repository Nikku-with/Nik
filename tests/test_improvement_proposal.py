from __future__ import annotations

import json

from jarvis.tools import improvement


def test_self_improvement_is_a_reviewable_proposal_and_never_edits_source(tmp_path, monkeypatch):
    monkeypatch.setattr(improvement, "WORKSPACE_DIR", tmp_path / "workspace")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    diff = """--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""

    result = improvement.propose_self_improvement.invoke({
        "title": "Improve the sample",
        "rationale": "A human-reviewed behavior change.",
        "unified_diff": diff,
    })

    assert "Source improvement proposal saved" in result
    assert "source unchanged" in result
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    proposal_dir = tmp_path / "workspace" / "proposals"
    patch_path = next(proposal_dir.glob("*.patch"))
    metadata_path = next(proposal_dir.glob("*.json"))
    assert patch_path.read_text(encoding="utf-8").strip() == diff.strip()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["applied"] is False
    assert "review and test manually" in metadata["verification"]


def test_self_improvement_rejects_non_diff_text_and_is_registered_as_write(tmp_path, monkeypatch):
    monkeypatch.setattr(improvement, "WORKSPACE_DIR", tmp_path)
    result = improvement.propose_self_improvement.invoke({
        "title": "bad", "rationale": "not a patch", "unified_diff": "run this shell command",
    })
    assert "Unified diff headers" in result
    assert not list(tmp_path.rglob("*"))

    from jarvis.security.permissions import check
    from jarvis.tools import tool_names, tools_for

    assert "propose_self_improvement" in tool_names()
    assert "propose_self_improvement" in tool_names(tools_for("code"))
    assert check("propose_self_improvement", "api").allowed
    assert not check("propose_self_improvement", "telegram_guest").allowed
