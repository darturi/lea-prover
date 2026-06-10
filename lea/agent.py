"""Lea agent — the core loop. Model calls tools until done.

`run_events()` is the generator core: it yields a typed event stream and never
prints. `run()` is a backward-compatible wrapper that drains those events through
the default stdout renderer and returns the final text (and optional transcript),
so existing callers (CLI, eval) keep working unchanged.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import LeaConfig, load_config
from .prompt import load_system_prompt
from .providers import stream, TextDelta, ToolCall, Done, _ToolMeta, Usage
from . import tools as _tools  # noqa: F401 — importing registers the built-in tools
from .registry import build_toolset, import_tool_modules
from .events import (
    SessionResumed,
    TurnStarted,
    AssistantTextDelta,
    ToolCalled,
    ToolResulted,
    ApprovalRequested,
    ApprovalResolved,
    UsageUpdated,
    ProjectEntryUpdated,
    Finished,
)
from .project import ProjectContext, project_context_message, record_project_entry
from .render import render_to_stdout

SESSIONS_DIR = Path.home() / ".lea" / "sessions"

_UNSET = object()  # sentinel: distinguishes "caller omitted arg" from an explicit None


_NARRATE_TOOL_STEPS_INSTRUCTION = """

When you are about to call one or more tools, first write a concise progress
summary for the user. Keep it to one or two sentences, use Markdown when helpful,
and include mathematical notation in normal LaTeX delimiters when useful. Explain
what you are trying next and why, then call the tool. Do not narrate after every
minor token or repeat boilerplate; summarize the meaningful proof step.
"""


_FORCED_TOOL_NARRATION_INSTRUCTION = """

You are Lea explaining the next proof action to the user. The main model turn
selected a tool call without first writing user-facing narration. Write the
missing narration now.

Rules:
- Write one concise paragraph unless the mathematical plan genuinely benefits
  from a short numbered list.
- Explain the mathematical or Lean proof move being attempted and why it is the
  next useful step.
- Use Markdown and ordinary LaTeX delimiters when helpful.
- Do not mention JSON, API internals, or hidden/private reasoning.
- Do not call tools. Return only the narration text.
"""


_THEOREM_TRANSLATION_PROMPT = """
You are Lea in theorem-translation review mode.

Translate the user's natural-language theorem into a Lean 4 theorem declaration.
Return exactly one self-contained Lean 4 file inside a ```lean code block.

Rules:
- Include imports and any necessary namespace/open statements.
- Include exactly one top-level theorem or lemma that corresponds to the user's theorem.
- The theorem body must be `by sorry`.
- Do not prove the theorem.
- Do not introduce helper lemmas.
- Do not weaken, simplify, or replace the user's mathematical claim.
- If previous feedback is provided, incorporate it into the next declaration.
- Use Lean 4 syntax only.
- Use `fun x => ...` or Lean 4 lambda syntax with `=>`; never use Lean 3 lambda syntax with a comma.
- Lean namespaces are case-sensitive. Use `Finset.range`, not `finset.range`.
- Prefer `import Mathlib` unless there is a strong reason to use narrower imports.
- The returned file must pass `lean_check` with no errors. `sorry` warnings are acceptable in this preflight step.
"""


def _extract_lean_code(text: str) -> str:
    """Pull a Lean code block from model output, or use the raw text as a fallback."""
    match = re.search(r"```(?:lean|Lean)?\s*(.*?)```", text, re.DOTALL)
    code = match.group(1) if match else text
    return code.strip()


def _extract_theorem_name(code: str) -> str | None:
    match = re.search(r"^\s*(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_']*)\b", code, re.MULTILINE)
    return match.group(1) if match else None


def _as_sorry_skeleton(code: str) -> str:
    """Keep imports/header, force the first theorem/lemma body to `by sorry`."""
    match = re.search(
        r"^\s*(?:theorem|lemma)\s+[A-Za-z_][A-Za-z0-9_']*\b.*?:=",
        code,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return code
    return code[:match.end()].rstrip() + " by sorry"


def _theorem_header(code: str, theorem_name: str | None = None) -> str | None:
    """Return a whitespace-normalized declaration header through `:=`.

    This intentionally stays lightweight: it catches ordinary statement drift
    without trying to parse Lean.
    """
    if theorem_name:
        pattern = rf"^\s*(?:theorem|lemma)\s+{re.escape(theorem_name)}\b.*?:="
    else:
        pattern = r"^\s*(?:theorem|lemma)\s+[A-Za-z_][A-Za-z0-9_']*\b.*?:="
    match = re.search(pattern, code, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    return " ".join(match.group(0).split())


def _proposal_file(session_id: str, candidate: int) -> Path:
    directory = Path(__file__).resolve().parent.parent / "workspace" / "proofs" / ".lea_proposals"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}_theorem_translation_{candidate}.lean"


def _lean_check_has_error(output: str) -> bool:
    return bool(re.search(r"(^|\n).*error[:\s]", output, re.IGNORECASE))


def _tool_result_ok(output: str) -> bool:
    return bool(output.strip()) and not output.strip().lower().startswith("error:")


def _format_translation_attempt(attempt: dict) -> str:
    return (
        f"Attempt {attempt['attempt']} candidate:\n"
        f"{attempt['code']}\n\n"
        f"Attempt {attempt['attempt']} diagnostics:\n"
        f"{attempt['check']}"
    )


class TheoremTranslationError(RuntimeError):
    """A failed theorem-translation preflight, including model usage spent."""

    def __init__(self, message: str, usage: Usage, cost: float):
        super().__init__(message)
        self.usage = usage
        self.cost = cost


class ProofVerificationState:
    """Track whether the latest Lean proof file has passed a fresh check."""

    def __init__(self):
        self.latest_proof_path: str | None = None
        self.unchecked_write = False
        self.latest_check_path: str | None = None
        self.latest_check_output: str | None = None
        self.latest_check_passed: bool | None = None

    def note_tool_result(self, tool_name: str, args: dict, result: str) -> None:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return
        if tool_name in {"write_file", "edit_file"}:
            if _tool_result_ok(result) and path.endswith(".lean"):
                self.latest_proof_path = path
                self.unchecked_write = True
            return
        if tool_name == "lean_check":
            passed = not _lean_check_has_error(result)
            self.latest_check_path = path
            self.latest_check_output = result
            self.latest_check_passed = passed
            if path == self.latest_proof_path:
                self.unchecked_write = False

    def needs_final_check(self) -> bool:
        if not self.latest_proof_path:
            return False
        return (
            self.unchecked_write
            or self.latest_check_path != self.latest_proof_path
        )

    def latest_proof_verified(self) -> bool:
        return (
            bool(self.latest_proof_path)
            and not self.unchecked_write
            and self.latest_check_path == self.latest_proof_path
            and self.latest_check_passed is True
        )


_FINAL_GATE_FAILURE_MESSAGE = (
    "Error: final verification gate failed. Lea claimed the proof was complete, "
    "but the latest proof file did not pass lean_check."
)


def _checked_theorem_translation(
    *,
    model: str,
    task: str,
    feedback: list[str],
    config: LeaConfig,
    candidate: int,
    session_id: str,
    project: ProjectContext | None = None,
):
    """Generate and typecheck a theorem translation candidate.

    Retries invalid Lean internally according to config. Returns
    (code, theorem_name, check_result, usage, cost).
    """
    messages = []
    project_message = project_context_message(project)
    if project_message:
        messages.append(project_message)
    messages.append({"role": "user", "content": task})
    if feedback:
        messages.append({
            "role": "user",
            "content": "User feedback on previous theorem translations:\n" + "\n".join(
                f"- {item}" for item in feedback
            ),
        })

    usage = Usage()
    cost = 0.0
    failed_attempts: list[dict] = []

    for attempt in range(1, config.theorem_translation_max_retries + 1):
        attempt_messages = list(messages)
        if failed_attempts:
            previous = failed_attempts[-1]
            attempt_messages.append({
                "role": "user",
                "content": (
                    "The previous Lean theorem declaration did not typecheck.\n"
                    "Return a corrected complete Lean file only.\n\n"
                    "Requirements:\n"
                    "- Preserve the user's mathematical claim.\n"
                    "- Keep exactly one theorem or lemma.\n"
                    "- Use Lean 4 syntax only.\n"
                    "- Prefer `import Mathlib` if the error may be caused by missing imports.\n"
                    "- The body must be `by sorry`.\n\n"
                    "Previous candidate:\n"
                    "```lean\n"
                    f"{previous['code']}\n"
                    "```\n\n"
                    "Diagnostics:\n"
                    f"{previous['check']}"
                ),
            })

        text = ""
        for event in stream(
            model,
            _THEOREM_TRANSLATION_PROMPT,
            attempt_messages,
            [],
            config.model_kwargs,
            streaming=config.stream,
        ):
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, Done):
                usage.input_tokens += event.usage.input_tokens
                usage.output_tokens += event.usage.output_tokens
                cost += event.cost

        code = _as_sorry_skeleton(_extract_lean_code(text))
        path = _proposal_file(session_id, candidate)
        path.write_text(code + "\n")
        check = _tools.lean_check(str(path))
        if not _lean_check_has_error(check):
            return code, _extract_theorem_name(code), check, usage, cost
        failed_attempts.append({"attempt": attempt, "code": code, "check": check})

    attempts = "\n\n".join(_format_translation_attempt(item) for item in failed_attempts)
    raise TheoremTranslationError(
        "theorem translation failed to typecheck after "
        f"{config.theorem_translation_max_retries} attempts.\n\n"
        f"{attempts}",
        usage,
        cost,
    )


def _approval_decision(value) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        raise RuntimeError("approval decision required: send {'decision': 'accept'|'reject', 'feedback': str}.")
    decision = value.get("decision")
    feedback = value.get("feedback")
    if decision not in {"accept", "reject"}:
        raise RuntimeError("approval decision must be 'accept' or 'reject'.")
    if feedback is not None and not isinstance(feedback, str):
        raise RuntimeError("approval feedback must be a string when provided.")
    return decision, feedback


def _guarded_tool_handlers(tool_handlers: dict, accepted_header: str | None, theorem_name: str | None) -> dict:
    if not accepted_header:
        return tool_handlers

    def check_content(content: str) -> str | None:
        header = _theorem_header(content, theorem_name)
        if header is not None and header != accepted_header:
            return (
                "Error: attempted to change the accepted top-level theorem statement. "
                "Keep the theorem declaration header exactly as approved by the user."
            )
        return None

    guarded = dict(tool_handlers)
    write_handler = tool_handlers.get("write_file")
    edit_handler = tool_handlers.get("edit_file")

    if write_handler:
        def guarded_write(args):
            content = args.get("content")
            if isinstance(content, str):
                error = check_content(content)
                if error:
                    return error
            return write_handler(args)
        guarded["write_file"] = guarded_write

    if edit_handler:
        def guarded_edit(args):
            path = args.get("path")
            old = args.get("old_string")
            new = args.get("new_string")
            if isinstance(path, str) and isinstance(old, str) and isinstance(new, str):
                p = Path(path).expanduser()
                if p.exists() and p.suffix == ".lean":
                    text = p.read_text()
                    if old in text:
                        error = check_content(text.replace(old, new, 1))
                        if error:
                            return error
            return edit_handler(args)
        guarded["edit_file"] = guarded_edit

    return guarded


def _tool_call_for_prompt(name: str, args: dict) -> dict:
    """Compact a tool call enough to show a narration-only model pass."""
    compact: dict = {}
    for key, value in args.items():
        if isinstance(value, str):
            if key == "content" and len(value) > 1600:
                compact[key] = value[:1600] + "\n... [truncated]"
            elif len(value) > 800:
                compact[key] = value[:800] + "... [truncated]"
            else:
                compact[key] = value
        else:
            compact[key] = value
    return {"name": name, "args": compact}


def _forced_tool_narration(
    *,
    model: str,
    system: str,
    messages: list,
    tool_name: str,
    tool_args: dict,
    config: LeaConfig,
):
    """Ask the model for narration when a tool-only turn would otherwise be silent."""
    narration_messages = messages + [{
        "role": "user",
        "content": (
            "Write the user-facing narration that should appear immediately "
            "before this Lea tool call:\n"
            f"{json.dumps(_tool_call_for_prompt(tool_name, tool_args), ensure_ascii=False, indent=2)}"
        ),
    }]
    text = ""
    usage = Usage()
    cost = 0.0
    try:
        for event in stream(
            model,
            system + _FORCED_TOOL_NARRATION_INSTRUCTION,
            narration_messages,
            [],
            config.model_kwargs,
            streaming=config.stream,
        ):
            if isinstance(event, TextDelta):
                text += event.text
                yield event
            elif isinstance(event, Done):
                usage.input_tokens += event.usage.input_tokens
                usage.output_tokens += event.usage.output_tokens
                cost += event.cost
    except Exception:
        fallback = _fallback_tool_narration(tool_name, tool_args)
        text += fallback
        yield TextDelta(fallback)
    return text.strip(), usage, cost


def _fallback_tool_narration(tool_name: str, args: dict) -> str:
    path = args.get("path")
    if tool_name == "write_file":
        if isinstance(path, str) and path:
            return f"I will write the next Lean proof attempt in `{path}` and then check whether it compiles."
        return "I will write the next Lean proof attempt and then check whether it compiles."
    if tool_name == "edit_file":
        if isinstance(path, str) and path:
            return f"I will revise `{path}` to address the previous Lean feedback, then re-run the checker."
        return "I will revise the Lean proof to address the previous checker feedback, then re-run it."
    if tool_name == "lean_check":
        if isinstance(path, str) and path:
            return f"I will run Lean on `{path}` to verify the current proof and inspect any errors."
        return "I will run Lean to verify the current proof and inspect any errors."
    if tool_name == "search_mathlib":
        query = args.get("query")
        if isinstance(query, str) and query:
            return f"I will search Mathlib for lemmas related to `{query}` so the next proof step can use existing results."
        return "I will search Mathlib for a relevant lemma before continuing the proof."
    return f"I will use `{tool_name}` for the next proof step and then use its result to continue."


def _save_session(session_id: str, model: str, messages: list, usage: Usage):
    """Persist conversation to disk."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{session_id}.json"
    # Strip raw_part (non-serializable provider objects) from messages
    clean_messages = []
    for msg in messages:
        if msg["role"] == "assistant" and isinstance(msg["content"], list):
            clean_content = [
                {k: v for k, v in item.items() if k != "raw_part"}
                for item in msg["content"]
            ]
            clean_messages.append({"role": msg["role"], "content": clean_content})
        else:
            clean_messages.append(msg)
    data = {
        "id": session_id,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        "messages": clean_messages,
    }
    path.write_text(json.dumps(data, indent=2))


def _load_session(session_id: str | None) -> dict:
    """Load a session by ID, or the most recent one if ID is None."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if session_id:
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return json.loads(path.read_text())
    # Find most recent
    sessions = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        raise FileNotFoundError("No sessions found.")
    return json.loads(sessions[0].read_text())


def list_sessions() -> list[dict]:
    """Return a list of session summaries."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    summaries = []
    for path in sessions[:20]:
        data = json.loads(path.read_text())
        # Extract the original task from the first user message
        task = data["messages"][0]["content"] if data["messages"] else ""
        if isinstance(task, str) and len(task) > 80:
            task = task[:80] + "..."
        summaries.append({
            "id": data["id"],
            "timestamp": data.get("timestamp", ""),
            "model": data.get("model", ""),
            "task": task,
            "turns": len([m for m in data["messages"] if m["role"] == "assistant"]),
        })
    return summaries


def run_events(
    config: LeaConfig,
    task: str,
    *,
    resume: str | bool = False,
    project: dict | ProjectContext | None = None,
):
    """Core loop as a generator: yields typed events, never prints.

    Yields SessionResumed?, then per turn: TurnStarted, AssistantTextDelta*,
    ToolCalled*, UsageUpdated, ToolResulted*, and finally Finished.

    Owns MCP lifecycle: starts configured servers (which register their tools)
    before the inner loop resolves the toolset, and stops them when the event
    stream ends or is closed.
    """
    mcp_manager = None
    if config.mcp_servers:
        from .mcp import MCPManager
        mcp_manager = MCPManager(config.mcp_servers)
        mcp_manager.start()
    try:
        yield from _run_events_inner(config, task, resume=resume, project=project)
    finally:
        if mcp_manager is not None:
            mcp_manager.stop()


def _run_events_inner(
    config: LeaConfig,
    task: str,
    *,
    resume: str | bool = False,
    project: dict | ProjectContext | None = None,
):
    system = load_system_prompt(config.prompt_variant, config.skills)
    if config.narrate_tool_steps:
        system += _NARRATE_TOOL_STEPS_INSTRUCTION
    model = config.model_name

    # Resolve the active toolset once: import any user tool modules so their
    # tools register, then select per config (None → all registered tools).
    import_tool_modules(config.tool_modules)
    tools_schema, tool_handlers = build_toolset(config.tools)
    if isinstance(project, dict):
        project = ProjectContext(
            project_id=str(project.get("project_id") or ""),
            project_path=project.get("project_path"),
            project_context=project.get("project_context"),
            record_on_success=bool(project.get("record_on_success", True)),
        )

    if resume:
        session = _load_session(resume if isinstance(resume, str) else None)
        messages = session["messages"]
        model = session.get("model", model)
        session_id = session["id"]
        total_usage = Usage(
            session.get("usage", {}).get("input_tokens", 0),
            session.get("usage", {}).get("output_tokens", 0),
        )
        if task:
            messages.append({"role": "user", "content": task})
        yield SessionResumed(session_id, len(messages))
    else:
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        messages = []
        project_message = project_context_message(project)
        if project_message:
            messages.append(project_message)
        messages.append({"role": "user", "content": task})
        total_usage = Usage()

    total_cost = 0.0
    accepted_signature: str | None = None
    proof_state = ProofVerificationState()

    def transcript(turns: int) -> dict:
        clean = []
        for msg in messages:
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                clean.append({"role": "assistant", "content": [
                    {k: v for k, v in item.items() if k != "raw_part"}
                    for item in msg["content"]
                ]})
            else:
                clean.append(msg)
        return {
            "session_id": session_id,
            "model": model,
            "turns": turns,
            "usage": {"input_tokens": total_usage.input_tokens, "output_tokens": total_usage.output_tokens},
            "messages": clean,
        }

    if config.permission_tier == "theorem_translation":
        feedback: list[str] = []
        candidate = 0
        while True:
            candidate += 1
            try:
                code, theorem_name, check_result, proposal_usage, proposal_cost = _checked_theorem_translation(
                    model=model,
                    task=task,
                    feedback=feedback,
                    config=config,
                    candidate=candidate,
                    session_id=session_id,
                    project=project,
                )
            except TheoremTranslationError as e:
                total_usage.input_tokens += e.usage.input_tokens
                total_usage.output_tokens += e.usage.output_tokens
                total_cost += e.cost
                if e.usage.input_tokens or e.usage.output_tokens or e.cost:
                    yield UsageUpdated(e.usage.input_tokens, e.usage.output_tokens, e.cost)
                _save_session(session_id, model, messages, total_usage)
                yield Finished(
                    "theorem_translation_failed",
                    f"Error: theorem translation failed: {type(e).__name__}: {e}",
                    0,
                    session_id,
                    model,
                    total_usage,
                    total_cost,
                    transcript(0),
                )
                return
            except Exception as e:
                _save_session(session_id, model, messages, total_usage)
                yield Finished(
                    "theorem_translation_failed",
                    f"Error: theorem translation failed: {type(e).__name__}: {e}",
                    0,
                    session_id,
                    model,
                    total_usage,
                    total_cost,
                    transcript(0),
                )
                return

            total_usage.input_tokens += proposal_usage.input_tokens
            total_usage.output_tokens += proposal_usage.output_tokens
            total_cost += proposal_cost
            if proposal_usage.input_tokens or proposal_usage.output_tokens or proposal_cost:
                yield UsageUpdated(proposal_usage.input_tokens, proposal_usage.output_tokens, proposal_cost)

            approval_id = f"{session_id}-theorem-translation-{candidate}"
            decision_value = yield ApprovalRequested(
                approval_id=approval_id,
                tier="theorem_translation",
                candidate=candidate,
                lean_code=code,
                theorem_name=theorem_name,
                check_result=check_result,
            )
            decision, rejection_feedback = _approval_decision(decision_value)
            yield ApprovalResolved(approval_id, decision, rejection_feedback)

            if decision == "accept":
                accepted_signature = code
                accepted_header = _theorem_header(code, theorem_name)
                tool_handlers = _guarded_tool_handlers(tool_handlers, accepted_header, theorem_name)
                messages.append({
                    "role": "user",
                    "content": (
                        "The user accepted this top-level Lean theorem translation. "
                        "Use this declaration exactly as the top-level theorem statement. "
                        "Do not change its name, binders, type signature, or proposition while proving it.\n\n"
                        "```lean\n"
                        f"{code}\n"
                        "```"
                    ),
                })
                _save_session(session_id, model, messages, total_usage)
                break

            rejection_feedback = (rejection_feedback or "").strip()
            if not rejection_feedback:
                raise RuntimeError("rejecting a theorem translation requires natural-language feedback.")
            feedback.append(rejection_feedback)
            messages.append({
                "role": "user",
                "content": (
                    "The user rejected the previous top-level Lean theorem translation with this feedback:\n"
                    f"{rejection_feedback}"
                ),
            })
            _save_session(session_id, model, messages, total_usage)

    turn = 0
    while True:
        turn += 1
        if config.max_turns and turn > config.max_turns:
            _save_session(session_id, model, messages, total_usage)
            yield Finished("max_turns", "Error: max turns reached without completing the proof.",
                           turn - 1, session_id, model, total_usage, total_cost, transcript(turn - 1))
            return

        yield TurnStarted(turn)

        assistant_parts = []
        current_text = ""
        tool_calls = []
        forced_narration_emitted = False

        for event in stream(model, system, messages, tools_schema, config.model_kwargs, streaming=config.stream):
            if isinstance(event, TextDelta):
                current_text += event.text
                yield AssistantTextDelta(event.text)
            elif isinstance(event, ToolCall):
                if config.narrate_tool_steps and not forced_narration_emitted and not current_text and not any(
                    part.get("type") == "text" and part.get("text") for part in assistant_parts
                ):
                    narration = _forced_tool_narration(
                        model=model,
                        system=system,
                        messages=messages,
                        tool_name=event.name,
                        tool_args=event.args,
                        config=config,
                    )
                    try:
                        while True:
                            narration_event = next(narration)
                            current_text += narration_event.text
                            yield AssistantTextDelta(narration_event.text)
                    except StopIteration as result:
                        _, narration_usage, narration_cost = result.value
                        total_usage.input_tokens += narration_usage.input_tokens
                        total_usage.output_tokens += narration_usage.output_tokens
                        total_cost += narration_cost
                        if narration_usage.input_tokens or narration_usage.output_tokens or narration_cost:
                            yield UsageUpdated(
                                narration_usage.input_tokens,
                                narration_usage.output_tokens,
                                narration_cost,
                            )
                    forced_narration_emitted = True
                if current_text:
                    assistant_parts.append({"type": "text", "text": current_text})
                    current_text = ""
                yield ToolCalled(event.name, event.args)
                tool_calls.append({"name": event.name, "args": event.args, "id": None, "raw_part": event.raw_part})
            elif isinstance(event, _ToolMeta):
                if tool_calls:
                    tool_calls[-1]["id"] = event.tool_use_id
            elif isinstance(event, Done):
                total_usage.input_tokens += event.usage.input_tokens
                total_usage.output_tokens += event.usage.output_tokens
                total_cost += event.cost
                yield UsageUpdated(event.usage.input_tokens, event.usage.output_tokens, event.cost)

        if current_text:
            assistant_parts.append({"type": "text", "text": current_text})
        for tc in tool_calls:
            assistant_parts.append({
                "type": "tool_call",
                "name": tc["name"],
                "args": tc["args"],
                "id": tc["id"],
                "raw_part": tc.get("raw_part"),
            })
        messages.append({"role": "assistant", "content": assistant_parts})

        if not tool_calls:
            text = "".join(p["text"] for p in assistant_parts if p["type"] == "text")
            if proof_state.needs_final_check():
                check_path = proof_state.latest_proof_path
                assert check_path is not None
                check_args = {"path": check_path}
                yield ToolCalled("lean_check", check_args)
                handler = tool_handlers.get("lean_check")
                if handler:
                    try:
                        result = handler(check_args)
                    except Exception as e:
                        result = f"Error: tool 'lean_check' raised {type(e).__name__}: {e}"
                else:
                    result = "Error: unknown tool 'lean_check'"
                preview = result[:200] + "..." if len(result) > 200 else result
                yield ToolResulted("lean_check", result, preview)
                proof_state.note_tool_result("lean_check", check_args, result)

                tool_result = {"type": "tool_result", "tool_name": "lean_check", "content": result}
                gate_call_id = f"final_gate_lean_check_{turn}"
                messages.append({"role": "assistant", "content": [{
                    "type": "tool_call",
                    "name": "lean_check",
                    "args": check_args,
                    "id": gate_call_id,
                }]})
                tool_result["tool_use_id"] = gate_call_id
                tool_result["tool_call_id"] = gate_call_id
                messages.append({"role": "user", "content": [tool_result]})
            if proof_state.latest_proof_path and not proof_state.latest_proof_verified():
                diagnostic = proof_state.latest_check_output or "No successful lean_check was observed."
                messages.append({"role": "user", "content": f"{_FINAL_GATE_FAILURE_MESSAGE}\n\n{diagnostic}"})
                _save_session(session_id, model, messages, total_usage)
                continue

            _save_session(session_id, model, messages, total_usage)
            final_transcript = transcript(turn)
            if project:
                try:
                    update = record_project_entry(
                        project=project,
                        task=task,
                        transcript=final_transcript,
                        signature=accepted_signature,
                    )
                    if update:
                        yield ProjectEntryUpdated(
                            update.project_id,
                            update.project_path,
                            update.theorem_name,
                            update.proof_path,
                            update.entry_action,
                            update.module_name,
                        )
                except Exception as exc:
                    yield AssistantTextDelta(
                        f"\n\nProject recording failed: {type(exc).__name__}: {exc}"
                    )
            yield Finished("completed", text or "(no response)", turn, session_id, model,
                           total_usage, total_cost, final_transcript)
            return

        tool_results = []
        for tc in tool_calls:
            handler = tool_handlers.get(tc["name"])
            if handler:
                try:
                    result = handler(tc["args"])
                except Exception as e:
                    result = f"Error: tool '{tc['name']}' raised {type(e).__name__}: {e}"
            else:
                result = f"Error: unknown tool '{tc['name']}'"

            preview = result[:200] + "..." if len(result) > 200 else result
            yield ToolResulted(tc["name"], result, preview)
            proof_state.note_tool_result(tc["name"], tc["args"], result)

            tool_result = {"type": "tool_result", "tool_name": tc["name"], "content": result}
            if tc["id"]:
                tool_result["tool_use_id"] = tc["id"]
                tool_result["tool_call_id"] = tc["id"]
            tool_results.append(tool_result)

        messages.append({"role": "user", "content": tool_results})
        _save_session(session_id, model, messages, total_usage)


def run(
    task: str,
    model: str | None = None,
    max_turns=_UNSET,
    provider: str | None = None,  # accepted for back-compat; LiteLLM routes by model name
    resume: str | bool = False,
    return_transcript: bool = False,
    prompt_variant: str | None = None,
) -> str | tuple[str, dict]:
    """Backward-compatible wrapper: run the agent and render to stdout.

    Builds a LeaConfig from defaults + any explicit overrides, drains the event
    stream through the default renderer, and returns the final text (and the
    transcript dict if return_transcript is True).
    """
    config = load_config(None)
    if model is not None:
        config.model_name = model
    if prompt_variant is not None:
        config.prompt_variant = prompt_variant
    if max_turns is not _UNSET:
        config.max_turns = max_turns

    text, transcript = render_to_stdout(run_events(config, task or "", resume=resume))
    return (text, transcript) if return_transcript else text
