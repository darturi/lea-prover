"""The agent event contract — the event-out half of config-in / event-out.

`run_events()` (in agent.py) yields these immutable events instead of printing.
Three consumers share this one contract: the CLI renderer (render.py) reproduces
today's stdout from them, a UI can render them live, and eval can collect them.
"""

from dataclasses import dataclass

from .providers import Usage


@dataclass(frozen=True)
class SessionResumed:
    """Emitted once at startup when --resume loads an existing session."""
    session_id: str
    message_count: int


@dataclass(frozen=True)
class TurnStarted:
    """Start of a loop iteration (1-based)."""
    turn: int


@dataclass(frozen=True)
class AssistantTextDelta:
    """A streaming chunk of assistant text (deltas, not the whole message)."""
    text: str


@dataclass(frozen=True)
class ToolCalled:
    """The model asked to run a tool."""
    name: str
    args: dict


@dataclass(frozen=True)
class ToolResulted:
    """A tool finished. `content` is the full result; `preview` is the truncation shown to the user."""
    name: str
    content: str
    preview: str


@dataclass(frozen=True)
class ApprovalRequested:
    """The agent is paused until a user accepts or rejects a proposed action."""
    approval_id: str
    tier: str
    candidate: int
    lean_code: str
    theorem_name: str | None
    check_result: str


@dataclass(frozen=True)
class ApprovalResolved:
    """A previously requested approval was answered by the user."""
    approval_id: str
    decision: str
    feedback: str | None = None


@dataclass(frozen=True)
class UsageUpdated:
    """Token usage + cost for a single turn's model response (a per-turn delta)."""
    input_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True)
class ProjectEntryUpdated:
    """A successful proof was recorded in a project markdown file."""
    project_id: str
    project_path: str
    theorem_name: str
    proof_path: str
    entry_action: str
    module_name: str | None = None


@dataclass(frozen=True)
class Finished:
    """Terminal event. `reason` is "completed" or "max_turns"."""
    reason: str
    text: str
    turns: int
    session_id: str
    model: str
    usage: Usage
    cost: float
    transcript: dict


# Union of everything run_events() can yield — handy for type annotations.
AgentEvent = (
    SessionResumed
    | TurnStarted
    | AssistantTextDelta
    | ToolCalled
    | ToolResulted
    | ApprovalRequested
    | ApprovalResolved
    | UsageUpdated
    | ProjectEntryUpdated
    | Finished
)
