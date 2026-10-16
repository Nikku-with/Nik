from __future__ import annotations

import asyncio
from pathlib import Path


def test_catalog_skills_reference_only_registered_tools():
    from jarvis.skills.loader import load_skills
    from jarvis.tools import tool_names

    skills = load_skills(force=True)
    assert len(skills) == 18
    registered = set(tool_names())
    unknown = {
        skill.name: sorted(set(skill.tools) - registered)
        for skill in skills
        if set(skill.tools) - registered
    }
    assert not unknown
    assert len({skill.name for skill in skills}) == 18
    assert all(Path(skill.path).name == "SKILL.md" for skill in skills)
    assert all(skill.body.strip() for skill in skills)


def test_requested_real_tool_skills_are_registered_and_evidence_oriented():
    from jarvis.skills.loader import load_skills
    from jarvis.tools import tool_names

    by_name = {skill.name: skill for skill in load_skills(force=True)}
    required = {
        "task_verification", "deep_research", "file_management", "terminal_ops", "project_builder",
        "desktop_browser_ops", "data_extraction_and_reports", "automation_and_scheduling",
        "memory_management", "self_review",
    }
    assert required <= set(by_name)
    registered = set(tool_names())
    for name in required:
        skill = by_name[name]
        assert skill.tools
        assert set(skill.tools) <= registered
        assert "verify" in skill.body.lower() or "evidence" in skill.body.lower()
    assert "run_shell_command" in by_name["terminal_ops"].tools
    assert "create_project" in by_name["project_builder"].tools
    assert "propose_self_improvement" in by_name["self_review"].tools


def test_skill_loader_reads_configured_catalog_and_refreshes(monkeypatch, tmp_path):
    from jarvis.skills import loader

    monkeypatch.setattr(loader, "SKILLS_DIR", tmp_path)
    skill_file = tmp_path / "probe_skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: probe_skill\ndescription: " + '"probe workflow"' +
        "\nkeywords: [probe]\ntools: [get_current_datetime]\n---\nfirst body\n",
        encoding="utf-8",
    )
    loader.clear_cache()
    first = loader.load_skills()
    assert len(first) == 1 and first[0].name == "probe_skill"
    assert "first body" in loader.skills_prompt_block("probe")

    skill_file.write_text(
        "---\nname: probe_skill\ndescription: " + '"probe workflow"' +
        "\nkeywords: [probe]\ntools: [get_current_datetime]\n---\nrefreshed body\n",
        encoding="utf-8",
    )
    loader.clear_cache()
    refreshed = loader.load_skills()
    assert "refreshed body" in refreshed[0].body


def test_scalar_frontmatter_lists_and_skill_name_lookup_are_normalized(monkeypatch, tmp_path):
    from jarvis.skills import loader

    monkeypatch.setattr(loader, "SKILLS_DIR", tmp_path)
    skill_file = tmp_path / "skillpacks" / "focused_workflow" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: focused_workflow\ndescription: A focused workflow\n"
        "keywords: focused workflow, precise matching\n"
        "tools: get_current_datetime, list_skills\n---\nFollow the workflow and verify each step.\n",
        encoding="utf-8",
    )
    loader.clear_cache()

    skill = loader.load_skills(force=True)[0]
    assert skill.keywords == ["focused workflow", "precise matching"]
    assert skill.tools == ["get_current_datetime", "list_skills"]

    exact = asyncio.run(loader.read_skill.ainvoke({"name": "focused_workflow"}))
    fuzzy = asyncio.run(loader.read_skill.ainvoke({"name": "focused workflo"}))
    path_only = asyncio.run(loader.read_skill.ainvoke({"name": "SKILL.md"}))
    assert "focused_workflow" in exact
    assert "focused_workflow" in fuzzy
    assert "not found" in path_only.lower() or "nahi mili" in path_only.lower()
