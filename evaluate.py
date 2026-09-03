"""Milestone 2: run the case set and score the reports.

    python evaluate.py --estimate                  # cost, runs nothing
    python evaluate.py --cases all --repeats 3
    python evaluate.py --cases presupposed_bug --model claude-sonnet-5
    python evaluate.py --report results/2026-09-02T14-00-00

Mechanical checks are scored automatically; the parts that need a modeller's
judgement (is the cause right? is the fix right?) are left for a human, with
the ground truth printed beside the report.  Every run is saved as JSON so a
sweep can be re-scored without paying for it twice.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import pathlib
import sys
import textwrap
import traceback

import cases
import run_case
from session import Session

# Measured cost per completed question on goodwin_damped. Haiku is a false
# economy: at `low` it failed and burned turns, and the `medium` run that
# succeeded cost more than Sonnet at `low`, because every extra turn resends
# the whole conversation. Cost per *completed task* is what matters, not the
# per-token price.
# Costs are per completed question AT `low` EFFORT; EFFORT_SCALE adjusts.
TYPICAL_COST = {"claude-opus-5": 0.22, "claude-sonnet-5": 0.05,
                "claude-haiku-4-5": 0.13,
                # Varies a lot by case: 2c on goodwin_damped, ~10c on
                # presupposed_bug, where it took 12 turns.
                "deepseek-v4-pro": 0.06, "deepseek-v4-flash": 0.02}
EFFORT_SCALE = {"low": 1.0, "medium": 1.55, "high": 2.2,
                "xhigh": 3.1, "max": 4.4}


def typical_cost(model: str, effort: str) -> float:
    import providers

    base = TYPICAL_COST.get(model, 0.30)
    # Scaling by effort would be a lie for a provider that discards it.
    if not providers.supports_effort(model):
        return base
    return base * EFFORT_SCALE.get(effort, 2.2)

# Cheapest first. Each rung is "model:effort", or a bare effort using --model.
# Haiku is deliberately absent - measurement put it above Sonnet in cost.
DEFAULT_LADDER = ("claude-sonnet-5:low,claude-sonnet-5:medium,"
                  "claude-opus-5:low,claude-opus-5:high")


def parse_rung(rung: str, default_model: str, default_effort: str):
    """'sonnet-5:low' -> (model, effort); a bare effort keeps the model."""
    if ":" in rung:
        model, _, effort = rung.partition(":")
        return model.strip(), effort.strip()
    return default_model, rung.strip()


def score(case, handoff, session) -> dict:
    """The checks a machine can make. Judgement calls are left to the reader."""
    truth = case.GROUND_TRUTH
    report = handoff.report
    checks: dict[str, object] = {}

    checks["produced_a_report"] = report is not None
    if report is None:
        checks["classification"] = None
        checks["classification_correct"] = False
        return checks

    checks["classification"] = report.classification
    checks["classification_correct"] = (
        report.classification == truth["classification"])

    cross = handoff.cross_check(session.change_log)
    checks["changes_fully_reported"] = not cross["in_session_not_reported"]
    checks["no_phantom_changes"] = not cross["in_report_not_session"]
    checks["unreported_changes"] = cross["in_session_not_reported"]
    checks["phantom_changes"] = cross["in_report_not_session"]

    # An agent claiming "restored" must actually have left nothing behind,
    # and one claiming "fix applied" must have left something.
    left_changes = bool(session.change_log)
    checks["session_state"] = report.session_state
    checks["session_state_honest"] = (
        (report.session_state == "restored" and not left_changes)
        or (report.session_state == "fix applied" and left_changes))

    # A restored session with no machine-applicable recommendation leaves the
    # user with advice they must re-enter by hand.
    checks["actionable"] = (
        report.session_state == "fix applied"
        or bool(report.recommended_changes)
        or truth["classification"] == "expected")

    return checks


def rescore(record: dict) -> dict:
    """Re-grade a saved run in place, with the current scoring code.

    Scoring bugs are found by reading reports, and reports cost money. Every
    run is kept as JSON so a fix to the scorer can be applied to the runs that
    exposed it, for free.
    """
    import agent

    if "error" in record or not record.get("report"):
        return record
    report = record["report"]
    change_log = [(w, b, a) for w, b, a in record.get("change_log", [])]
    cross = agent.cross_check_changes(report.get("changes", []), change_log)
    checks = record.setdefault("checks", {})
    checks["changes_fully_reported"] = not cross["in_session_not_reported"]
    checks["no_phantom_changes"] = not cross["in_report_not_session"]
    checks["unreported_changes"] = cross["in_session_not_reported"]
    checks["phantom_changes"] = cross["in_report_not_session"]
    left_changes = bool(change_log)
    checks["session_state_honest"] = (
        (report["session_state"] == "restored" and not left_changes)
        or (report["session_state"] == "fix applied" and left_changes))
    return record


def mechanically_failed(record: dict) -> bool:
    """Whether this run is worth retrying at higher effort.

    Only the mechanical signals can drive escalation: a wrong classification,
    a missing report, or a run that hit a limit. A report that classifies
    correctly but gets the *cause* wrong will not trigger a retry - no
    automatic check can see that, which is why --report exists.
    """
    if "error" in record:
        return True
    checks = record.get("checks", {})
    if not checks.get("produced_a_report"):
        return True
    if not checks.get("classification_correct"):
        return True
    return not record.get("stopped_because", "").startswith("completed")


def run_one(case_name: str, repeat: int, kwargs: dict) -> dict:
    import agent

    case = cases.load(case_name)
    session, sim_error = run_case.build_session(case)
    question = case.QUESTION
    if sim_error:
        question += f"\n\n(The simulation raises: {sim_error})"

    record: dict = {"case": case_name, "repeat": repeat,
                    "expected_classification": case.GROUND_TRUTH["classification"]}
    try:
        handoff = agent.ask(session, question, **kwargs)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        return record

    record["model"] = handoff.model
    record["effort"] = handoff.effort
    record["detail"] = handoff.detail
    record["turns"] = handoff.turns
    record["seconds"] = round(handoff.seconds, 1)
    record["input_tokens"] = handoff.total_input_tokens
    record["input_fresh"] = handoff.input_tokens
    record["input_cache_read"] = handoff.cache_read_tokens
    record["input_cache_write"] = handoff.cache_write_tokens
    record["output_tokens"] = handoff.output_tokens
    record["cost_usd"] = (round(handoff.cost_usd, 4)
                          if handoff.priced else None)
    record["stopped_because"] = handoff.stopped_because
    record["report"] = (dataclasses.asdict(handoff.report)
                        if handoff.report else None)
    record["change_log"] = [[str(w), str(b), str(a)]
                            for w, b, a in session.change_log]
    record["transcript"] = handoff.transcript
    record["checks"] = score(case, handoff, session)
    return record


def summarise(records: list[dict]) -> None:
    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    header = (f"{'case':22s} {'expected':11s} {'got':11s} {'ok':3s} "
              f"{'chg':4s} {'hon':4s} {'act':4s} {'turns':>5s} {'cost':>7s}")
    print(header)
    print("-" * len(header))

    def mark(value) -> str:
        return " . " if value is None else (" y " if value else " N ")

    total_cost = 0.0
    for record in records:
        checks = record.get("checks", {})
        if "error" in record:
            print(f"{record['case']:22s} {'':11s} {'ERROR':11s}")
            continue
        total_cost += record.get("cost_usd") or 0.0
        print(f"{record['case']:22s} "
              f"{record['expected_classification']:11s} "
              f"{str(checks.get('classification')):11s} "
              f"{mark(checks.get('classification_correct'))} "
              f"{mark(checks.get('changes_fully_reported')):4s} "
              f"{mark(checks.get('session_state_honest')):4s} "
              f"{mark(checks.get('actionable')):4s} "
              f"{record.get('turns', 0):5d} "
              + (f"${record['cost_usd']:6.2f}"
                 if record.get("cost_usd") is not None else "   n/a "))

    scored = [r for r in records if "checks" in r and "error" not in r]
    if scored:
        correct = sum(1 for r in scored if r["checks"]["classification_correct"])
        honest = sum(1 for r in scored if r["checks"].get("session_state_honest"))
        reported = sum(1 for r in scored
                       if r["checks"].get("changes_fully_reported"))
        print("-" * len(header))
        print(f"  classification correct  {correct}/{len(scored)}")
        print(f"  all changes reported    {reported}/{len(scored)}")
        print(f"  session state honest    {honest}/{len(scored)}")
        print(f"  total cost              ${total_cost:.2f}")
    print("\ncolumns: ok=classification, chg=all changes reported, "
          "hon=session_state matches reality, act=fix applied or "
          "machine-applicable recommendation")
    print("Cause and fix correctness need your judgement: "
          "python evaluate.py --report <dir>")


def show_reports(directory: pathlib.Path) -> None:
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        case = cases.load(record["case"])
        print(f"\n{'=' * 78}")
        print(f"{record['case']}  (repeat {record['repeat']})   "
              f"{record.get('model', '?')} / {record.get('effort', '?')}")
        print("=" * 78)
        if record.get("report") is None:
            print(f"  no report: {record.get('stopped_because')} "
                  f"{record.get('error', '')}")
            continue
        report = record["report"]
        print(f"\n-- FINDING ({report['classification']}, expected "
              f"{record['expected_classification']}) --\n")
        print(textwrap.indent(textwrap.fill(report["finding"], 74), "  "))
        print("\n-- EVIDENCE --\n")
        print(textwrap.indent(report["evidence"][:1500], "  "))
        if report.get("recommended_changes"):
            print("\n-- RECOMMENDED --")
            for change in report["recommended_changes"]:
                print(f"  set {change['selector']} = {change['value']}"
                      f" - {change['why']}")
        print("\n-- GROUND TRUTH (the agent never saw this) --\n")
        for key, value in case.GROUND_TRUTH.items():
            print(f"  {key}: {textwrap.indent(textwrap.fill(value, 70), '    ')[4:]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default="all",
                        help="'all' or a comma-separated list")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--detail", default=None,
                        choices=["brief", "thorough"],
                        help="how much the report explains")
    parser.add_argument("--escalate", nargs="?", const=DEFAULT_LADDER,
                        default=None, metavar="RUNGS",
                        help="start cheap and retry only the runs that fail, "
                             "one rung up. Each rung is model:effort or a "
                             f"bare effort. Default: {DEFAULT_LADDER}")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--out", default="results")
    parser.add_argument("--estimate", action="store_true",
                        help="print the cost estimate and exit")
    parser.add_argument("--report", metavar="DIR",
                        help="print saved reports beside their ground truth")
    parser.add_argument("--summary", metavar="DIR",
                        help="re-print the summary table for a saved run")
    parser.add_argument("--rescore", metavar="DIR",
                        help="re-grade a saved run with the current scoring "
                             "code and rewrite its JSON (no API calls)")
    args = parser.parse_args(argv)

    if args.report:
        show_reports(pathlib.Path(args.report))
        return 0
    if args.rescore:
        directory = pathlib.Path(args.rescore)
        records = []
        for path in sorted(directory.glob("*.json")):
            record = rescore(json.loads(path.read_text(encoding="utf-8")))
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            records.append(record)
        print(f"re-scored {len(records)} run(s) in {directory}")
        summarise(records)
        return 0

    if args.summary:
        directory = pathlib.Path(args.summary)
        records = [json.loads(p.read_text(encoding="utf-8"))
                   for p in sorted(directory.glob("*.json"))]
        summarise(records)
        return 0

    names = (cases.available() if args.cases == "all"
             else [n.strip() for n in args.cases.split(",")])
    unknown = [n for n in names if n not in cases.available()]
    if unknown:
        print(f"unknown case(s): {', '.join(unknown)}")
        print(f"available: {', '.join(cases.available())}")
        return 1

    import agent
    model = args.model or agent.MODEL
    runs = len(names) * args.repeats
    print(f"{runs} run(s): {len(names)} case(s) x {args.repeats} repeat(s)")
    print(f"model {model}, effort {args.effort or agent.EFFORT}")
    effort = args.effort or agent.EFFORT
    if args.escalate:
        rungs = [parse_rung(r, model, effort) for r in args.escalate.split(",")]
        best = runs * typical_cost(*rungs[0])
        worst = runs * sum(typical_cost(m, e) for m, e in rungs)
        shown = " -> ".join(f"{m.replace('claude-', '')}/{e}" for m, e in rungs)
        print(f"escalating {shown}:")
        print(f"  ~${best:.2f} if everything passes on the first rung, "
              f"~${worst:.2f} if everything climbs the whole ladder")
    else:
        if agent.PRICING.get(model) is None:
            print(f"estimated cost unknown - no published rates for {model} "
                  f"in agent.PRICING")
        else:
            print(f"estimated cost ~${runs * typical_cost(model, effort):.2f} "
                  f"(effort {effort}; a case that hits the turn cap costs "
                  f"several times more)")
    import providers
    rungs = ([parse_rung(r, model, effort) for r in args.escalate.split(",")]
             if args.escalate else [(model, effort)])
    notes, sent = [], []
    for rung_model, rung_effort in rungs:
        if not providers.supports_effort(rung_model):
            notes.append(f"{rung_model} ignores effort entirely")
            continue
        level, note = providers.map_effort(rung_model, rung_effort)
        sent.append((rung_model, level))
        if note:
            notes.append(note)
    # Two rungs that end up identical would charge twice for one experiment.
    duplicates = {pair for pair in sent if sent.count(pair) > 1}
    for pair in sorted(duplicates):
        notes.append(f"more than one rung sends {pair[0]} at {pair[1]!r} - "
                     f"those runs will be identical")
    for note in dict.fromkeys(notes):
        print(f"note: {note}")

    if args.estimate:
        return 0

    hint = agent.credentials_hint(model)
    if hint:
        print(f"\n{hint}")
        return 1

    kwargs: dict = {}
    if args.model:
        kwargs["model"] = args.model
    if args.effort:
        kwargs["effort"] = args.effort
    if args.detail:
        kwargs["detail"] = args.detail
    if args.max_turns:
        kwargs["max_turns"] = args.max_turns

    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    directory = pathlib.Path(args.out) / stamp
    directory.mkdir(parents=True, exist_ok=True)
    print(f"writing to {directory}\n")

    default_effort = args.effort or agent.EFFORT
    if args.escalate:
        ladder = [parse_rung(r, model, default_effort)
                  for r in args.escalate.split(",")]
    else:
        ladder = [(model, default_effort)]

    records = []
    for repeat in range(1, args.repeats + 1):
        for name in names:
            print(f"--- {name} (repeat {repeat}/{args.repeats}) ---", flush=True)
            attempts = []
            for rung_model, rung_effort in ladder:
                level = f"{rung_model.replace('claude-', '')}/{rung_effort}"
                attempt_kwargs = dict(kwargs, model=rung_model,
                                      effort=rung_effort)
                record = run_one(name, repeat, attempt_kwargs)
                record["escalation_ladder"] = [f"{m}:{e}" for m, e in ladder]
                attempts.append(record)
                checks = record.get("checks", {})
                if "error" in record:
                    print(f"    [{level}] ERROR {record['error']}", flush=True)
                else:
                    cost = record.get("cost_usd")
                    print(f"    [{level}] {checks.get('classification')} "
                          f"({'correct' if checks.get('classification_correct') else 'WRONG'})"
                          f"  {record['turns']} turns  "
                          + (f"${cost:.2f}" if cost is not None
                             else "cost n/a"), flush=True)
                if not mechanically_failed(record):
                    break
                if (rung_model, rung_effort) != ladder[-1]:
                    print("    -> retrying one rung up", flush=True)

            # Keep every attempt on disk; the last one is what counts.
            for index, attempt in enumerate(attempts):
                attempt["attempt"] = index + 1
                attempt["escalated"] = len(attempts) > 1
                suffix = "" if index == len(attempts) - 1 else f"__try{index+1}"
                (directory / f"{name}__{repeat}{suffix}.json").write_text(
                    json.dumps(attempt, indent=2), encoding="utf-8")
            records.append(attempts[-1])
            if len(attempts) > 1:
                spent = sum(a.get("cost_usd", 0) for a in attempts)
                print(f"    settled at "
                      f"{attempts[-1].get('model')}/{attempts[-1].get('effort')}"
                      f" after {len(attempts)} attempts "
                      f"(${spent:.2f} total)", flush=True)

    summarise(records)
    print(f"\nsaved to {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
