"""Project markdown support for reusable Lea theorem context."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT / "workspace"
PROJECTS_ROOT = WORKSPACE_ROOT / "projects"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_LEAN_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_THEOREM_MARKER_RE = re.compile(
    r"(?m)^## Theorem: .+\n\n"
    r"<!-- lea:theorem name=\"(?P<name>[^\"]+)\" proof=\"[^\"]*\""
    r"(?: module=\"(?P<module>[^\"]+)\")? -->\n"
)


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    project_path: str | None = None
    project_context: str | None = None
    record_on_success: bool = True


@dataclass(frozen=True)
class ProjectUpdateResult:
    project_id: str
    project_path: str
    theorem_name: str
    proof_path: str
    entry_action: str
    module_name: str | None = None


@dataclass(frozen=True)
class ProjectTheoremEntry:
    name: str
    proof_path: str
    module_name: str | None = None


@dataclass(frozen=True)
class _ProjectTheoremSection:
    entry: ProjectTheoremEntry
    start: int
    end: int


def validate_project_id(project_id: str) -> str:
    slug = str(project_id or "").strip()
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError("project_id must be 1-80 characters using letters, numbers, '_' or '-'.")
    return slug


def project_namespace_part(project_id: str) -> str:
    slug = validate_project_id(project_id)
    part = "".join(token[:1].upper() + token[1:] for token in re.split(r"[-_]+", slug) if token)
    if not part:
        part = "Project"
    if not re.match(r"^[A-Za-z_]", part):
        part = f"Project{part}"
    return part


def project_namespace(project_id: str) -> str:
    return f"Lea.{project_namespace_part(project_id)}"


def project_proof_dir(project_id: str) -> str:
    return f"workspace/proofs/Lea/{project_namespace_part(project_id)}"


def resolve_project_path(project_id: str, project_path: str | None = None) -> Path:
    slug = validate_project_id(project_id)
    if project_path:
        path = Path(project_path).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path) if path.parts and path.parts[0] == "workspace" else (WORKSPACE_ROOT / path)
        path = path.resolve()
        root = WORKSPACE_ROOT.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("project_path must resolve inside the Lea workspace.") from exc
        if path.suffix != ".md":
            raise ValueError("project_path must point to a .md file.")
        return path
    return PROJECTS_ROOT / f"{slug}.md"


def load_project_context(project: ProjectContext | None) -> ProjectContext | None:
    if project is None:
        return None
    project_id = validate_project_id(project.project_id)
    path = resolve_project_path(project_id, project.project_path)
    context = project.project_context
    if context is None and path.exists():
        context = path.read_text()
    return ProjectContext(
        project_id=project_id,
        project_path=str(path),
        project_context=context or "",
        record_on_success=project.record_on_success,
    )


def project_context_message(project: ProjectContext | None) -> dict[str, str] | None:
    project = load_project_context(project)
    if project is None:
        return None
    body = project.project_context.strip() or "(The project file is empty so far.)"
    namespace = project_namespace(project.project_id)
    proof_dir = project_proof_dir(project.project_id)
    return {
        "role": "user",
        "content": (
            "Existing project facts and reusable Lean artifacts for project "
            f"`{project.project_id}` follow. Use prior theorem locations from this "
            "markdown when they help, importing existing proofs instead of reproving "
            "supporting lemmas. The `proof` attributes are filesystem paths, not Lean "
            "import paths; only use a `module` attribute as an import path when it is "
            "present. Write new project proof files under "
            f"`{proof_dir}/`, wrap project declarations in `namespace {namespace}` / "
            f"`end {namespace}`, and use module paths beginning with `{namespace}`. "
            "Do not create `Lea.Common`, `Lea.Experimental`, or `Lea.Examples`; "
            "non-project proofs belong under `Lea.Misc`. "
            "Do not edit the project markdown during proof search; "
            "Lea will record the final result after the proof succeeds.\n\n"
            f"{body}"
        ),
    }


def record_project_entry(
    *,
    project: ProjectContext,
    task: str,
    transcript: dict[str, Any],
    signature: str | None,
) -> ProjectUpdateResult | None:
    project = load_project_context(project)
    if project is None or not project.record_on_success:
        return None

    proof_path = final_proof_path(transcript)
    if not proof_path:
        return None
    if not _workspace_file_exists(proof_path):
        return None
    module_name = module_name_from_proof_path(proof_path)
    theorem_name = theorem_name_from_signature(signature) or theorem_name_from_file(proof_path) or "unnamed_theorem"
    signature = normalize_signature(signature) or signature_from_file(proof_path) or f"theorem {theorem_name} : _ := by"
    summary = solving_summary(transcript)

    path = resolve_project_path(project.project_id, project.project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else _project_header(project.project_id)
    entry = render_project_entry(
        theorem_name=theorem_name,
        proof_path=proof_path,
        module_name=module_name,
        signature=signature,
        description=task,
        solving_process=summary,
    )
    updated, action = upsert_entry(existing, theorem_name, entry)
    path.write_text(updated)
    return ProjectUpdateResult(project.project_id, str(path), theorem_name, proof_path, action, module_name)


def render_project_entry(
    *,
    theorem_name: str,
    proof_path: str,
    module_name: str | None = None,
    signature: str,
    description: str,
    solving_process: str,
) -> str:
    attrs = (
        f'name="{html.escape(theorem_name, quote=True)}" '
        f'proof="{html.escape(proof_path, quote=True)}"'
    )
    if module_name:
        attrs += f' module="{html.escape(module_name, quote=True)}"'
    return (
        f"## Theorem: {theorem_name}\n\n"
        f"<!-- lea:theorem {attrs} -->\n\n"
        "### Signature\n\n"
        "```lean\n"
        f"{signature.strip()}\n"
        "```\n\n"
        "### Description\n\n"
        f"{description.strip() or '(No description recorded.)'}\n\n"
        "### Solving Process\n\n"
        f"{solving_process.strip() or '(No solving summary recorded.)'}\n\n"
        "### Lean Location\n\n"
        f"`{proof_path}`\n"
    )


def upsert_entry(markdown: str, theorem_name: str, entry: str) -> tuple[str, str]:
    marker_matches = list(_THEOREM_MARKER_RE.finditer(markdown))
    for index, match in enumerate(marker_matches):
        if match.group("name") != theorem_name:
            continue
        start = match.start()
        end = marker_matches[index + 1].start() if index + 1 < len(marker_matches) else len(markdown)
        return markdown[:start].rstrip() + "\n\n" + entry.rstrip() + "\n", "updated"
    return markdown.rstrip() + "\n\n" + entry.rstrip() + "\n", "created"


def parse_project_entries(markdown: str) -> list[ProjectTheoremEntry]:
    return [section.entry for section in _project_theorem_sections(markdown)]


def project_entry_for_theorem(markdown: str, theorem_name: str) -> ProjectTheoremEntry | None:
    for entry in parse_project_entries(markdown):
        if entry.name == theorem_name:
            return entry
    return None


def remove_project_entry(markdown: str, theorem_name: str) -> tuple[str, ProjectTheoremEntry]:
    for section in _project_theorem_sections(markdown):
        if section.entry.name != theorem_name:
            continue
        updated = markdown[:section.start].rstrip() + "\n\n" + markdown[section.end:].lstrip()
        return updated.rstrip() + "\n", section.entry
    raise ValueError(f"Project theorem entry not found: {theorem_name}")


def _project_theorem_sections(markdown: str) -> list[_ProjectTheoremSection]:
    marker_matches = list(_THEOREM_MARKER_RE.finditer(markdown))
    sections: list[_ProjectTheoremSection] = []
    for index, match in enumerate(marker_matches):
        start = match.start()
        end = marker_matches[index + 1].start() if index + 1 < len(marker_matches) else len(markdown)
        attrs = _parse_theorem_marker_attrs(match.group(0))
        name = attrs.get("name")
        proof = attrs.get("proof")
        if not name or not proof:
            continue
        sections.append(
            _ProjectTheoremSection(
                entry=ProjectTheoremEntry(
                    name=html.unescape(name),
                    proof_path=html.unescape(proof),
                    module_name=html.unescape(attrs["module"]) if attrs.get("module") else None,
                ),
                start=start,
                end=end,
            )
        )
    return sections


def _parse_theorem_marker_attrs(marker: str) -> dict[str, str]:
    return {
        key: value
        for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"', marker)
    }


def final_proof_path(transcript: dict[str, Any]) -> str | None:
    paths: list[str] = []
    for item in _walk(transcript):
        if not isinstance(item, dict):
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        path = args.get("path") or item.get("path")
        if isinstance(path, str) and path.endswith(".lean") and ".lea_proposals" not in Path(path).parts:
            paths.append(_workspace_relative(path))
    return paths[-1] if paths else None


def solving_summary(transcript: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in _walk(transcript):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content.strip())
        elif isinstance(content, list):
            texts.extend(
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    text = "\n\n".join(part for part in texts if part).strip()
    if not text:
        return "Lea completed the proof and verified the final Lean file."
    return text[-1800:]


def normalize_signature(signature: str | None) -> str | None:
    if not signature:
        return None
    text = signature.strip()
    match = re.search(r"(?ms)^\s*(?:theorem|lemma)\s+.*?:=", text)
    if match:
        text = " ".join(match.group(0).split()) + " by"
    return text


def theorem_name_from_signature(signature: str | None) -> str | None:
    if not signature:
        return None
    match = re.search(r"(?m)^\s*(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_']*)\b", signature)
    return match.group(1) if match else None


def theorem_name_from_file(proof_path: str) -> str | None:
    text = _read_workspace_file(proof_path)
    return theorem_name_from_signature(text)


def signature_from_file(proof_path: str) -> str | None:
    text = _read_workspace_file(proof_path)
    return normalize_signature(text)


def module_name_from_proof_path(proof_path: str) -> str | None:
    path = (REPO_ROOT / proof_path).resolve()
    try:
        rel = path.relative_to((WORKSPACE_ROOT / "proofs").resolve())
    except ValueError:
        return None
    if rel.suffix != ".lean":
        return None
    parts = rel.with_suffix("").parts
    if not parts or any(not _LEAN_MODULE_PART_RE.fullmatch(part) for part in parts):
        return None
    # With the current Lake layout, flat files under proofs/ are standalone source
    # files, not importable library modules. Nested paths like proofs/Lea/Foo.lean
    # map to import Lea.Foo.
    if len(parts) < 2:
        return None
    return ".".join(parts)


def _project_header(project_id: str) -> str:
    return f"# Project {project_id}\n\n<!-- lea:project id=\"{project_id}\" -->\n"


def _read_workspace_file(proof_path: str) -> str:
    path = (REPO_ROOT / proof_path).resolve()
    try:
        path.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return ""
    return path.read_text() if path.exists() else ""


def _workspace_file_exists(proof_path: str) -> bool:
    path = (REPO_ROOT / proof_path).resolve()
    try:
        path.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return path.exists() and path.is_file()


def _workspace_relative(path: str) -> str:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(REPO_ROOT.resolve()))
        except ValueError:
            return str(candidate)
    return str(candidate)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
