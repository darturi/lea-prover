from pathlib import Path

import pytest

from lea import project


def test_validate_project_id_rejects_path_like_values():
    with pytest.raises(ValueError):
        project.validate_project_id("../epsilon")


def test_resolve_project_path_defaults_to_workspace_projects():
    path = project.resolve_project_path("epsilon")
    assert path == project.WORKSPACE_ROOT / "projects" / "epsilon.md"


def test_resolve_project_path_accepts_repo_relative_workspace_path():
    path = project.resolve_project_path("epsilon", "workspace/projects/epsilon.md")
    assert path == project.REPO_ROOT / "workspace" / "projects" / "epsilon.md"


def test_project_namespace_uses_pascal_case_slug_parts():
    assert project.project_namespace("epsilon") == "Lea.Epsilon"
    assert project.project_namespace("fqb-v2_feedback") == "Lea.FqbV2Feedback"
    assert project.project_namespace("2026-test") == "Lea.Project2026Test"
    assert project.project_proof_dir("epsilon") == "workspace/proofs/Lea/Epsilon"


def test_project_context_message_includes_namespace_layout(monkeypatch, tmp_path):
    repo_root = tmp_path / "lea"
    monkeypatch.setattr(project, "REPO_ROOT", repo_root)
    monkeypatch.setattr(project, "WORKSPACE_ROOT", repo_root / "workspace")
    monkeypatch.setattr(project, "PROJECTS_ROOT", repo_root / "workspace" / "projects")

    msg = project.project_context_message(project.ProjectContext(project_id="epsilon"))

    assert msg is not None
    assert "`workspace/proofs/Lea/Epsilon/`" in msg["content"]
    assert "`namespace Lea.Epsilon`" in msg["content"]
    assert "`Lea.Misc`" in msg["content"]
    assert "`Lea.Common`, `Lea.Experimental`, or `Lea.Examples`" in msg["content"]


def test_upsert_entry_appends_and_replaces_existing_theorem():
    first = project.render_project_entry(
        theorem_name="supporting",
        proof_path="workspace/proofs/supporting.lean",
        module_name="Lea.Supporting",
        signature="theorem supporting : True := by",
        description="A supporting lemma.",
        solving_process="Solved directly.",
    )
    markdown, action = project.upsert_entry("# Project epsilon\n", "supporting", first)
    assert action == "created"
    assert "A supporting lemma." in markdown
    assert '<!-- lea:theorem name="supporting" proof="workspace/proofs/supporting.lean" module="Lea.Supporting" -->' in markdown

    second = project.render_project_entry(
        theorem_name="supporting",
        proof_path="workspace/proofs/supporting.lean",
        signature="theorem supporting : True := by",
        description="Updated description.",
        solving_process="Updated summary.",
    )
    markdown, action = project.upsert_entry(markdown, "supporting", second)
    assert action == "updated"
    assert "Updated description." in markdown
    assert "A supporting lemma." not in markdown
    assert "module=" not in markdown


def test_parse_and_remove_project_entries():
    first = project.render_project_entry(
        theorem_name="first",
        proof_path="workspace/proofs/Lea/Epsilon/first.lean",
        module_name="Lea.Epsilon.first",
        signature="theorem first : True := by",
        description="First.",
        solving_process="Done.",
    )
    second = project.render_project_entry(
        theorem_name="second",
        proof_path="workspace/proofs/Lea/Epsilon/second.lean",
        module_name="Lea.Epsilon.second",
        signature="theorem second : True := by",
        description="Second.",
        solving_process="Done.",
    )
    markdown = "# Project epsilon\n\n" + first + "\n" + second

    entries = project.parse_project_entries(markdown)
    assert [entry.name for entry in entries] == ["first", "second"]
    assert entries[0].proof_path == "workspace/proofs/Lea/Epsilon/first.lean"
    assert entries[0].module_name == "Lea.Epsilon.first"
    assert project.project_entry_for_theorem(markdown, "second") == entries[1]

    updated, removed = project.remove_project_entry(markdown, "first")

    assert removed.name == "first"
    assert "## Theorem: first" not in updated
    assert "## Theorem: second" in updated


def test_module_name_from_proof_path_only_for_importable_layout(monkeypatch, tmp_path):
    repo_root = tmp_path / "lea"
    monkeypatch.setattr(project, "REPO_ROOT", repo_root)
    monkeypatch.setattr(project, "WORKSPACE_ROOT", repo_root / "workspace")

    assert project.module_name_from_proof_path("workspace/proofs/flat.lean") is None
    assert (
        project.module_name_from_proof_path("workspace/proofs/Lea/Supporting.lean")
        == "Lea.Supporting"
    )


def test_record_project_entry_writes_markdown(monkeypatch, tmp_path):
    repo_root = tmp_path / "lea"
    proof = repo_root / "workspace" / "proofs" / "demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial\n")
    monkeypatch.setattr(project, "REPO_ROOT", repo_root)
    monkeypatch.setattr(project, "WORKSPACE_ROOT", repo_root / "workspace")
    monkeypatch.setattr(project, "PROJECTS_ROOT", repo_root / "workspace" / "projects")

    result = project.record_project_entry(
        project=project.ProjectContext(project_id="epsilon"),
        task="Prove True.",
        transcript={
            "messages": [{
                "role": "assistant",
                "content": [{"type": "tool_call", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}}],
            }],
        },
        signature="theorem demo : True := by sorry",
    )

    assert result is not None
    assert result.entry_action == "created"
    assert result.module_name is None
    text = Path(result.project_path).read_text()
    assert "<!-- lea:project id=\"epsilon\" -->" in text
    assert "<!-- lea:theorem name=\"demo\" proof=\"workspace/proofs/demo.lean\" -->" in text


def test_record_project_entry_writes_module_for_nested_module_path(monkeypatch, tmp_path):
    repo_root = tmp_path / "lea"
    proof = repo_root / "workspace" / "proofs" / "Lea" / "Demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial\n")
    monkeypatch.setattr(project, "REPO_ROOT", repo_root)
    monkeypatch.setattr(project, "WORKSPACE_ROOT", repo_root / "workspace")
    monkeypatch.setattr(project, "PROJECTS_ROOT", repo_root / "workspace" / "projects")

    result = project.record_project_entry(
        project=project.ProjectContext(project_id="epsilon"),
        task="Prove True.",
        transcript={
            "messages": [{
                "role": "assistant",
                "content": [{"type": "tool_call", "name": "lean_check", "args": {"path": "workspace/proofs/Lea/Demo.lean"}}],
            }],
        },
        signature="theorem demo : True := by sorry",
    )

    assert result is not None
    assert result.module_name == "Lea.Demo"
    text = Path(result.project_path).read_text()
    assert '<!-- lea:theorem name="demo" proof="workspace/proofs/Lea/Demo.lean" module="Lea.Demo" -->' in text


def test_record_project_entry_writes_project_namespace_module(monkeypatch, tmp_path):
    repo_root = tmp_path / "lea"
    proof = repo_root / "workspace" / "proofs" / "Lea" / "Epsilon" / "Demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("namespace Lea.Epsilon\n\ntheorem demo : True := by\n  trivial\n\nend Lea.Epsilon\n")
    monkeypatch.setattr(project, "REPO_ROOT", repo_root)
    monkeypatch.setattr(project, "WORKSPACE_ROOT", repo_root / "workspace")
    monkeypatch.setattr(project, "PROJECTS_ROOT", repo_root / "workspace" / "projects")

    result = project.record_project_entry(
        project=project.ProjectContext(project_id="epsilon"),
        task="Prove True.",
        transcript={
            "messages": [{
                "role": "assistant",
                "content": [{"type": "tool_call", "name": "lean_check", "args": {"path": "workspace/proofs/Lea/Epsilon/Demo.lean"}}],
            }],
        },
        signature="theorem demo : True := by sorry",
    )

    assert result is not None
    assert result.module_name == "Lea.Epsilon.Demo"
    text = Path(result.project_path).read_text()
    assert 'module="Lea.Epsilon.Demo"' in text
