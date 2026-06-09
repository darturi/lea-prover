"""Default renderer — turns the agent event stream back into Lea's stdout.

`run_events()` yields events; the CLI (and the backward-compatible `run()`
wrapper) drain them through `render_to_stdout`, which prints Lea's terminal
output (including live token streaming and per-turn cost) and returns the final
`(text, transcript)`. A UI would write its own consumer instead of this.
"""

import sys

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


def _prompt_for_approval(event: ApprovalRequested) -> dict | None:
    print("\n--- theorem translation approval ---", flush=True)
    if event.theorem_name:
        print(f"Theorem: {event.theorem_name}", flush=True)
    print(f"Candidate: {event.candidate}", flush=True)
    print("\n```lean", flush=True)
    print(event.lean_code, flush=True)
    print("```", flush=True)
    print(f"\nLean check: {event.check_result}", flush=True)

    try:
        while True:
            choice = input("\nAccept theorem translation? [y = accept, n = reject with feedback]: ").strip().lower()
            if choice in {"y", "yes", "a", "accept"}:
                return {"decision": "accept"}
            if choice in {"n", "no", "r", "reject"}:
                feedback = input("Feedback for the next translation: ").strip()
                if feedback:
                    return {"decision": "reject", "feedback": feedback}
                print("Feedback is required when rejecting.", flush=True)
            else:
                print("Please enter y or n.", flush=True)
    except (EOFError, KeyboardInterrupt):
        print("\nRun cancelled before theorem translation approval.", flush=True)
        return None


def render_to_stdout(events) -> tuple[str, dict]:
    """Consume the event stream, print Lea's usual output, return (text, transcript)."""
    result: tuple[str, dict] = ("", {})
    current_turn = 0
    cum_in = cum_out = 0
    cum_cost = 0.0
    iterator = iter(events)
    pending_send = None

    while True:
        try:
            if pending_send is not None and hasattr(iterator, "send"):
                event = iterator.send(pending_send)
                pending_send = None
            else:
                event = next(iterator)
        except StopIteration:
            break

        if isinstance(event, AssistantTextDelta):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, TurnStarted):
            current_turn = event.turn
            print(f"\n--- turn {event.turn} ---", flush=True)
        elif isinstance(event, ToolCalled):
            print(f"\n  -> {event.name}({event.args})", flush=True)
        elif isinstance(event, ToolResulted):
            print(f"  <- {event.preview}", flush=True)
        elif isinstance(event, ApprovalRequested):
            decision = _prompt_for_approval(event)
            if decision is None:
                if hasattr(iterator, "close"):
                    iterator.close()
                result = ("Run cancelled before theorem translation approval.", {})
                break
            pending_send = decision
        elif isinstance(event, ApprovalResolved):
            if event.decision == "accept":
                print("Theorem translation accepted.", flush=True)
            else:
                print("Theorem translation rejected; requesting a revised candidate.", flush=True)
        elif isinstance(event, SessionResumed):
            print(f"Resuming session {event.session_id} ({event.message_count} messages)", flush=True)
        elif isinstance(event, UsageUpdated):
            cum_in += event.input_tokens
            cum_out += event.output_tokens
            cum_cost += event.cost
            turn_tok = event.input_tokens + event.output_tokens
            cum_tok = cum_in + cum_out
            print(
                f"  [turn {current_turn}: {turn_tok:,} tok · ${event.cost:.6f} "
                f"| total: {cum_tok:,} tok · ${cum_cost:.6f}]",
                flush=True,
            )
        elif isinstance(event, ProjectEntryUpdated):
            print(
                f"\nRecorded `{event.theorem_name}` in project `{event.project_id}` "
                f"({event.entry_action}): {event.project_path}",
                flush=True,
            )
        elif isinstance(event, Finished):
            if event.reason == "completed":
                print()  # blank line before the usage summary
            total = event.usage.input_tokens + event.usage.output_tokens
            print(
                f"\n--- {event.turns} turns, {total:,} tokens "
                f"(in: {event.usage.input_tokens:,}, out: {event.usage.output_tokens:,}), "
                f"~${event.cost:.4f} ---",
                flush=True,
            )
            result = (event.text, event.transcript)
    return result
