"""Milestone 1 driver: load a case, hand it to the agent, print the report.

    python run_case.py --list
    python run_case.py goodwin_damped --dry-run     # no API call
    python run_case.py goodwin_damped
"""

from __future__ import annotations

import argparse
import sys
import textwrap

import cases
import prompts
from session import Session


def build_session(case, sandboxed: bool = False):
    """Reproduce the state the user was in when they gave up.

    With `sandboxed`, the model and the agent's code live in a worker
    subprocess with no credentials in its environment (see remote.py) - the
    same arrangement the GUI uses. Off by default: scoring runs are trusted
    local code, and an in-process Session starts ~4.5s sooner per case.
    """
    if sandboxed:
        from remote import WorkerSession
        session = WorkerSession()
    else:
        session = Session()
    session.load(case.MODEL)
    if hasattr(case, "SETUP"):
        if sandboxed:
            session.setup_case(case.__name__.rsplit(".", 1)[-1])
        else:
            case.SETUP(session)

    sim_error = None
    start, end, points = case.SIMULATION
    try:
        session.simulate(start, end, points)
    except Exception as exc:  # the user's own run failed - that is the question
        sim_error = f"{type(exc).__name__}: {exc}"
        session.last_sim = None
    return session, sim_error


def rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", nargs="?", help="case module name")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the handoff, make no API call")
    parser.add_argument("--truth", action="store_true",
                        help="print the case's ground truth after the report")
    parser.add_argument("--model", default=None,
                        help="claude-opus-5 (default), claude-sonnet-5, "
                             "claude-haiku-4-5")
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--detail", default=None,
                        choices=["brief", "thorough"],
                        help="how much the report explains")
    parser.add_argument("--max-turns", type=int, default=None)
    args = parser.parse_args(argv)

    if args.list or not args.case:
        print("cases:")
        for name in cases.available():
            print(f"  {name}")
        return 0

    case = cases.load(args.case)
    session, sim_error = build_session(case)

    question = case.QUESTION
    if sim_error:
        question += f"\n\n(The simulation raises: {sim_error})"

    rule(f"case: {case.NAME}")
    print(textwrap.indent(question.strip(), "  "))
    if sim_error:
        print(f"\n  user's simulation failed: {sim_error}")
    else:
        print(f"\n  simulated {case.SIMULATION}, "
              f"{session.last_result.shape[0]} rows, "
              f"columns {list(session.last_result.colnames)}")

    if args.dry_run:
        session.snapshot()
        rule("handoff payload (no API call made)")
        print(prompts.build_handoff(session, question))
        rule("system prompt")
        print(f"{len(prompts.SYSTEM_PROMPT)} characters")
        return 0

    import agent  # imported late so --dry-run works without credentials

    def on_event(kind, payload):
        if kind == "turn":
            print(f"\n--- turn {payload} ---", flush=True)
        elif kind == "text":
            print(textwrap.indent(payload.strip(), "  | "), flush=True)
        elif kind == "code":
            print(textwrap.indent(payload.strip(), "  > "), flush=True)
        elif kind == "output":
            body = payload if len(payload) < 1200 else payload[:1200] + " ..."
            print(textwrap.indent(body.strip(), "  . "), flush=True)
        elif kind == "limit":
            print(f"  !! {payload}", flush=True)

    kwargs = {"on_event": on_event}
    if args.model:
        kwargs["model"] = args.model
    if args.effort:
        kwargs["effort"] = args.effort
    if args.detail:
        kwargs["detail"] = args.detail
    if args.max_turns:
        kwargs["max_turns"] = args.max_turns

    rule("agent")
    handoff = agent.ask(session, question, **kwargs)

    rule("report")
    if handoff.report is None:
        print(f"No report produced ({handoff.stopped_because}).")
    else:
        print(handoff.report.as_markdown())

    rule("session change log")
    if session.change_log:
        for what, before, after in session.change_log:
            print(f"  {what}: {before!r} -> {after!r}")
    else:
        print("  (no net change)")

    check = handoff.cross_check(session.change_log)
    if check["in_session_not_reported"] or check["in_report_not_session"]:
        rule("cross-check discrepancies")
        for item in check["in_session_not_reported"]:
            print(f"  changed but NOT reported: {item}")
        for item in check["in_report_not_session"]:
            print(f"  reported but not present in session: {item}")

    rule("cost")
    print(f"  model              {handoff.model} (effort {handoff.effort}, "
          f"{handoff.detail})")
    print(f"  turns              {handoff.turns}")
    print(f"  wall clock         {handoff.seconds:.1f}s")
    print(f"  input tokens       {handoff.total_input_tokens:,}"
          f"  ({handoff.input_tokens:,} fresh"
          f" + {handoff.cache_write_tokens:,} cache write"
          f" + {handoff.cache_read_tokens:,} cache read)")
    print(f"  output tokens      {handoff.output_tokens:,}")
    print(f"  estimated cost     {handoff.cost_text()}")
    print(f"  stopped because    {handoff.stopped_because}")

    if args.truth:
        rule("ground truth (not shown to the agent)")
        for key, value in case.GROUND_TRUTH.items():
            print(f"  {key}:")
            print(textwrap.indent(textwrap.fill(value, 66), "    "))

    return 0


if __name__ == "__main__":
    sys.exit(main())
