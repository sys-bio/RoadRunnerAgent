"""Tool-use loop against the Messages API, and the `run_python` tool.

SECURITY: `run_python` is an unsandboxed exec in this process. Agent code can
read and write any file this user can and reach the network. That is an
accepted property of this proof of concept, on a single user's own machine.
The output truncation below is a context-budget measure, not a boundary.
"""

from __future__ import annotations

import ast
import io
import os
import re
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
import numpy as np

import prompts
import providers

MODEL = "claude-opus-5"
# Start cheap and escalate only where it demonstrably fails. `low` diagnosed
# the Goodwin case correctly at roughly half the cost of `high`; whether it
# holds on the controls is what evaluate.py --escalate measures.
EFFORT = "low"

# USD per million tokens: (fresh input, cache write, cache read, output).
# Cache writes cost 1.25x base input, cache reads 0.1x. Used only for the
# estimate shown in the UI - check https://claude.com/pricing for current
# rates.
PRICING = {
    "claude-opus-5": (5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-5": (2.00, 2.50, 0.20, 10.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    # DeepSeek charges nothing to write the cache, and halves everything
    # off-peak, so its rates are a function of the clock (see deepseek_rates).
    "deepseek-v4-pro": lambda: deepseek_rates(1.32, 0.044, 3.96),
    "deepseek-v4-flash": lambda: deepseek_rates(0.44, 0.014, 1.32),
    "deepseek-v4-flash-vision-exp": lambda: deepseek_rates(0.44, 0.014, 1.32),
}


def deepseek_is_peak(when=None) -> bool:
    """DeepSeek peak hours: 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.

    Off-peak is half price, so quoting one rate would be wrong half the time.
    Published at https://api-docs.deepseek.com/quick_start/pricing/
    """
    import datetime

    now = when or datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() >= 5:          # Saturday, Sunday
        return False
    hour = now.hour
    return 1 <= hour < 4 or 6 <= hour < 10


def deepseek_rates(peak_in: float, peak_cache_read: float,
                   peak_out: float) -> tuple[float, float, float, float]:
    """(fresh input, cache write, cache read, output) for the current hour.

    Cache writes are free on DeepSeek, unlike the Anthropic API where they
    cost 1.25x input.
    """
    scale = 1.0 if deepseek_is_peak() else 0.5
    return (peak_in * scale, 0.0, peak_cache_read * scale, peak_out * scale)
MODEL_CHOICES = list(PRICING)
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]
# How much the report explains. "brief" suits a modeller mid-flow; "thorough"
# shows the reasoning, names the alternatives it ruled out, and computes
# thresholds for this parameterisation rather than quoting textbook values.
DETAIL = "brief"
DETAIL_CHOICES = ["brief", "thorough"]

# Not every model takes the same request shape. Adaptive thinking and
# `output_config.effort` arrived with the 4.6 generation; Haiku 4.5 rejects
# both with a 400 ("adaptive thinking is not supported on this model") and
# uses the older fixed thinking budget instead.
ADAPTIVE_THINKING = {"claude-opus-5", "claude-sonnet-5"}
# Effort has no equivalent on the older models, so map it onto a budget.
EFFORT_BUDGETS = {"low": 2048, "medium": 4096, "high": 8192,
                  "xhigh": 12288, "max": 15000}

MAX_TURNS = 25
MAX_SECONDS = 600
# Cumulative across the whole handoff, input + output. A runaway loop that
# keeps producing short turns can outlast the turn cap in cost terms, because
# every turn resends the conversation (spec section 11.1).
MAX_TOTAL_TOKENS = 400_000
MAX_TOKENS = 16000
OUTPUT_LIMIT = 8192


def request_shape(model: str, effort: str) -> dict[str, Any]:
    """The thinking/effort parameters this particular model will accept."""
    if model in ADAPTIVE_THINKING:
        return {"thinking": {"type": "adaptive"},
                "output_config": {"effort": effort}}
    budget = min(EFFORT_BUDGETS.get(effort, 8192), MAX_TOKENS - 1024)
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Execute Python in the live modelling session. `rr` (the RoadRunner "
        "instance), `session`, `te` (tellurium), `antimony` and `np` are in "
        "scope, and your own variables persist across calls. Returns captured "
        "stdout/stderr plus the repr of the final expression, or the "
        "traceback if it raised. Use session.apply_model_edit(text) to change "
        "the model; do not call te.loada yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."}
        },
        "required": ["code"],
        "additionalProperties": False,
    },
}

SUBMIT_REPORT_TOOL = {
    "name": "submit_report",
    "description": "Deliver the final report. Call exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["numerical", "structural", "parametric", "expected"],
            },
            "finding": {"type": "string"},
            "evidence": {"type": "string"},
            "changes": {
                "type": "array",
                "description": (
                    "Every change you are LEAVING in place. `selector` is the "
                    "RoadRunner id or setting name you changed (e.g. 'k1', "
                    "'init([S1])', 'integrator.stiff') so the application can "
                    "match your report against what it observed; `what` is "
                    "the human description."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "what": {"type": "string"},
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                    },
                    "required": ["selector", "what", "before", "after"],
                    "additionalProperties": False,
                },
            },
            "recommended_changes": {
                "type": "array",
                "description": (
                    "Changes you recommend but have NOT left applied - for "
                    "example when you restored the session. "
                    "LEAVE THIS EMPTY when no change is needed: on a model "
                    "that is behaving correctly, an empty list is the right "
                    "answer and inventing a change is a mistake. "
                    "Only fill fields belonging to the `kind` you choose; "
                    "leave the others as empty string or 0."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["value", "solver", "model_text",
                                     "simulation"],
                            "description": (
                                "'value' - a parameter or initial value. "
                                "'solver' - the integrator or one of its "
                                "settings, or a steady-state solver setting. "
                                "'model_text' - the model itself is wrong (a "
                                "rate law, stoichiometry, a missing "
                                "reaction). 'simulation' - the model is fine "
                                "but the time course is not."
                            ),
                        },
                        "selector": {
                            "type": "string",
                            "description": (
                                "kind=value: a RoadRunner id, e.g. 'k1' or "
                                "'init([S1])'. kind=solver: 'integrator' to "
                                "switch integrator, or a dotted setting such "
                                "as 'integrator.seed', 'integrator.stiff', "
                                "'steadyStateSolver.allow_presimulation'. "
                                "kind=simulation: 'end_time'. Empty for "
                                "model_text."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "The value to set, written as text: a number "
                                "('12', '1e-9'), an integrator name "
                                "('cvode', 'gillespie'), or 'true'/'false'. "
                                "Empty for model_text."
                            ),
                        },
                        "model_text": {
                            "type": "string",
                            "description": (
                                "kind=model_text only: the COMPLETE corrected "
                                "Antimony model, ready to load. Empty "
                                "otherwise."
                            ),
                        },
                        "why": {"type": "string"},
                    },
                    "required": ["kind", "selector", "value", "model_text",
                                 "why"],
                    "additionalProperties": False,
                },
            },
            "session_state": {
                "type": "string",
                "enum": ["fix applied", "restored"],
            },
        },
        "required": ["classification", "finding", "evidence", "changes",
                     "recommended_changes", "session_state"],
        "additionalProperties": False,
    },
}


def make_client() -> anthropic.Anthropic:
    """Build the API client from the environment.

    The SDK resolves the key itself.  An identity-linked API key additionally
    requires the workspace it acts in, sent as a header - the SDK only adds
    that automatically for profile-based auth, so pass it here from
    ANTHROPIC_WORKSPACE_ID when one is set.
    """
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    return anthropic.Anthropic(default_headers=headers)


# Every run measured so far spends about 94% of its tokens on input, because
# each turn resends the conversation. 90/10 is a fair rule for pricing a
# budget before it is spent.
BUDGET_INPUT_SHARE = 0.9


def budget_cost(model: str, total_tokens: int) -> float:
    """What a token budget is worth in money for this model, or NaN.

    A budget in tokens means very different amounts of money per model - the
    same 400,000 is about $2 on Opus and $0.26 on DeepSeek - so the GUI shows
    the money rather than leaving the user to work it out.
    """
    rates = PRICING.get(model)
    if rates is None:
        return float("nan")
    if callable(rates):
        rates = rates()
    fresh, _write, _read, out = rates
    inputs = total_tokens * BUDGET_INPUT_SHARE
    outputs = total_tokens * (1.0 - BUDGET_INPUT_SHARE)
    return (inputs * fresh + outputs * out) / 1_000_000


def credentials_hint(model: str = MODEL) -> str | None:
    """A human-readable reason the API will fail, or None if it looks usable.

    Which key is needed depends on the model: each provider brings its own.
    """
    try:
        variable = providers.key_env_for(model)
    except ValueError as exc:
        return str(exc)
    if not os.environ.get(variable, "").strip():
        return (f"{variable} is not set in this process. If you used setx, "
                "open a new terminal - setx only affects processes started "
                "afterwards.")
    return None


# Models occasionally emit their own structured-output markers inside a
# string field, e.g. a `finding` that ends "...no root exists.</finding>
# <parameter name="evidence">Computed analytically: ...". Strip them rather
# than show the user raw markup.
_LEAKED_TAG = re.compile(
    r"</?(?:antml|invoke|function_calls|finding|evidence|parameter|changes|"
    r"recommended_changes|session_state|classification)"
    r"(?:\s+[^<>]*?)?/?>")
# Enough real content before a close tag to treat it as "the rest is spill".
_SPILL_THRESHOLD = 40


def clean_field(text: str) -> str:
    """Strip leaked markup without destroying the field.

    Models occasionally emit their own structured-output markers inside a
    string - a `finding` ending "...no root exists.</finding> <parameter
    name=\"evidence\">Computed analytically..." , or an `evidence` wrapped in
    stray </antml> tags. Truncate at a close tag only when real content
    precedes it, or a field that merely opens with one would be emptied.
    """
    if not isinstance(text, str):
        return text
    for match in _LEAKED_TAG.finditer(text):
        if match.group(0).startswith("</") and match.start() >= _SPILL_THRESHOLD:
            text = text[:match.start()]
            break
    text = _LEAKED_TAG.sub("", text).strip()
    # A bare "[]" left behind where an array field spilled in.
    if text.rstrip().endswith("[]"):
        text = text.rstrip()[:-2].rstrip()
    return text


def _truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Trim the middle - a traceback's last line is the informative one."""
    if len(text) <= limit:
        return text
    head, tail = limit // 2, limit - limit // 2 - 80
    dropped = len(text) - head - tail
    return (f"{text[:head]}\n\n... [{dropped} characters of output "
            f"truncated] ...\n\n{text[-tail:]}")


class PythonRunner:
    """Persistent exec namespace bound to a Session."""

    def __init__(self, session) -> None:
        self.session = session
        import matplotlib
        matplotlib.use("Agg")  # a blocking show() would hang the host process
        import tellurium as te
        import antimony

        np.set_printoptions(threshold=200, edgeitems=3, linewidth=100)
        self.namespace: dict[str, Any] = {
            "__name__": "__agent__",
            "te": te,
            "antimony": antimony,
            "np": np,
            "session": session,
        }

    def run(self, code: str) -> tuple[str, bool]:
        """Returns (output, is_error)."""
        # Rebind rr every call: a model edit replaces the object (spec 3.1).
        if self.session.loaded:
            self.namespace["rr"] = self.session.rr

        buffer = io.StringIO()
        is_error = False
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return _truncate(traceback.format_exc()), True

        body, tail = tree.body, None
        if body and isinstance(body[-1], ast.Expr):
            body, tail = body[:-1], body[-1]

        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]),
                                 "<agent>", "exec"), self.namespace)
                if tail is not None:
                    value = eval(  # noqa: S307 - unsandboxed by design
                        compile(ast.Expression(tail.value), "<agent>", "eval"),
                        self.namespace,
                    )
                    if value is not None:
                        print(repr(value), file=buffer)
        except BaseException:  # noqa: BLE001 - never raise into the loop
            buffer.write(traceback.format_exc())
            is_error = True

        output = buffer.getvalue()
        return _truncate(output) if output.strip() else "(no output)", is_error


@dataclass
class Report:
    classification: str
    finding: str
    evidence: str
    changes: list[dict[str, str]]
    session_state: str
    recommended_changes: list[dict[str, Any]] = field(default_factory=list)

    def as_markdown(self) -> str:
        lines = [f"**Finding** ({self.classification})", "", self.finding, "",
                 "**Evidence**", "", self.evidence, "", "**Changes**", ""]
        if self.changes:
            lines += [f"- {c['what']}: `{c['before']}` -> `{c['after']}`"
                      for c in self.changes]
        else:
            lines.append("None.")

        if self.recommended_changes:
            lines += ["", "**Recommended (not applied)**", ""]
            for change in self.recommended_changes:
                kind = change.get("kind", "value")
                if kind == "model_text":
                    head = "rewrite the model"
                elif kind == "simulation":
                    head = (f"simulate to t = "
                            f"{float(change.get('end_time') or 0):g}")
                else:
                    head = (f"set `{change['selector']}` to "
                            f"`{float(change['value']):g}`")
                lines.append(f"- {head} - {change['why']}")
                if kind == "model_text" and change.get("model_text"):
                    lines += ["", "```", change["model_text"].strip(), "```"]

        lines += ["", f"Session left: {self.session_state}."]
        return "\n".join(lines)


def _identifier(key: str) -> str:
    """The bit of a change-log key that names the thing changed.

    Log keys look like "parameter k1", "integrator stiff", "model text". The
    last token is the identifier, and it is what an agent's prose is likely to
    mention however it phrases the rest.
    """
    return str(key).strip().lower().split()[-1] if str(key).strip() else ""


def cross_check_changes(reported: list[dict], change_log) -> dict[str, list[str]]:
    """Compare what the agent said it changed with what the session observed.

    Both directions are informative: in the log but not the report is an
    omission - the failure this check exists to catch; in the report but not
    the log is a fix claimed but never applied, or applied then reverted.

    Matching is on identifiers, not prose. An earlier version compared the
    agent's sentence against the log key as substrings, which flagged
    "Enabled stiff (BDF) mode on the CVODE integrator" as failing to report
    "integrator stiff" - a false positive in both directions at once.
    """
    reported_blobs = []
    for change in reported:
        blob = " ".join(str(change.get(field, ""))
                        for field in ("selector", "what", "before", "after"))
        reported_blobs.append((change, blob.strip().lower()))

    actual = [(str(what), _identifier(what)) for what, _, _ in change_log]

    missing = [what for what, ident in actual
               if not any(ident and ident in blob for _, blob in reported_blobs)]

    phantom = []
    identifiers = {ident for _, ident in actual}
    for change, blob in reported_blobs:
        if not any(ident and ident in blob for ident in identifiers):
            phantom.append(change.get("selector") or change.get("what", "?"))

    return {"in_session_not_reported": sorted(missing),
            "in_report_not_session": sorted(phantom)}


@dataclass
class Handoff:
    """Everything one question-to-report round trip produced."""

    report: Report | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    model: str = MODEL
    effort: str = EFFORT
    detail: str = DETAIL
    turns: int = 0
    seconds: float = 0.0
    # These four are disjoint - the API reports uncached input, cache writes
    # and cache reads separately, so they add up rather than overlap.
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    stopped_because: str = "completed"
    # Turns spent by the run this one continues, if it is a follow-up.
    turns_before: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    def cost_text(self) -> str:
        """Cost for display: rates we do not have are said so, not guessed."""
        return f"${self.cost_usd:.2f}" if self.priced else "cost n/a"

    @property
    def priced(self) -> bool:
        """Whether we hold real rates for this model."""
        return PRICING.get(self.model) is not None

    @property
    def cost_usd(self) -> float:
        """Estimated cost in US dollars, or NaN when rates are unknown."""
        rates = PRICING.get(self.model)
        if rates is None:
            return float("nan")
        if callable(rates):          # time-of-day pricing
            rates = rates()
        fresh, write, read, out = rates
        return (self.input_tokens * fresh
                + self.cache_write_tokens * write
                + self.cache_read_tokens * read
                + self.output_tokens * out) / 1_000_000

    def cross_check(self, change_log) -> dict[str, list[str]]:
        return cross_check_changes(
            self.report.changes if self.report else [], change_log)


def ask(session, question: str, *, client: anthropic.Anthropic | None = None,
        model: str = MODEL, effort: str = EFFORT, detail: str = DETAIL,
        max_turns: int = MAX_TURNS,
        max_seconds: float = MAX_SECONDS,
        max_total_tokens: int = MAX_TOTAL_TOKENS,
        on_event: Callable[[str, Any], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
        previous: "Handoff | None" = None) -> Handoff:
    """One question, one report. Blocks until the agent is done.

    Pass `previous` (an earlier Handoff) to ask a follow-up: the agent keeps
    its whole investigation - every simulation it ran and every result it saw
    - instead of starting again. Only the new question is added, so a
    follow-up costs a fraction of a fresh handoff.

    The follow-up must use the same provider as the run it continues; message
    formats are not interchangeable between vendors.
    """
    runner = PythonRunner(session)
    handoff = Handoff(model=model, effort=effort, detail=detail)

    resuming = previous is not None and previous.messages
    if resuming and (providers.provider_for(previous.model)
                     != providers.provider_for(model)):
        raise ValueError(
            f"cannot continue a {previous.model} conversation on {model}: "
            "message formats differ between providers. Ask it fresh instead.")

    # What the user did to the session between the two questions - the agent
    # is about to be asked about a session it has not seen since its report.
    since = []
    if resuming and session.handoff_snapshot is not None:
        since = session.diff_since_snapshot()

    session.snapshot()
    provider = providers.make_provider(
        model=model, effort=effort,
        system=prompts.system_prompt(detail),
        tools=[RUN_PYTHON_TOOL, SUBMIT_REPORT_TOOL], max_tokens=MAX_TOKENS,
        client=client)
    if resuming:
        provider.resume(previous.messages,
                        prompts.build_follow_up(question, since))
        handoff.turns_before = previous.turns
    else:
        provider.start(prompts.build_handoff(session, question))
    handoff.messages = provider.messages

    def emit(kind, payload):
        if on_event:
            on_event(kind, payload)

    started = time.monotonic()
    winding_up = False

    while True:
        handoff.turns += 1
        emit("turn", handoff.turns)

        # On the wind-up turn, offer only submit_report. Asking politely
        # for a report was not enough: an agent that answered with another
        # run_python call left the run with nothing to show, having spent the
        # whole budget. Taking the other tool away removes the choice.
        turn = provider.turn([SUBMIT_REPORT_TOOL] if winding_up else None)
        handoff.input_tokens += turn.input_tokens
        handoff.output_tokens += turn.output_tokens
        handoff.cache_read_tokens += turn.cache_read_tokens
        handoff.cache_write_tokens += turn.cache_write_tokens

        if turn.stop_reason == "refusal":
            handoff.stopped_because = "refusal"
            emit("refusal", turn.refusal_details)
            break

        for text in turn.texts:
            emit("text", text)

        if not turn.tool_calls:
            handoff.stopped_because = "ended without submit_report"
            break

        results = []
        finished = False
        for call in turn.tool_calls:
            if call.name == "submit_report":
                fields = dict(call.input)
                for key in ("finding", "evidence"):
                    fields[key] = clean_field(fields.get(key, ""))
                try:
                    handoff.report = Report(**fields)
                    content, is_error = "Report received.", False
                    finished = True
                except TypeError as exc:
                    # A provider without strict schema enforcement can send a
                    # malformed report; ask for it again rather than crash.
                    content = (f"That report did not match the schema ({exc}). "
                               "Call submit_report again with every required "
                               "field.")
                    is_error = True
                results.append({"id": call.id, "content": content,
                                "is_error": is_error})
                if finished:
                    emit("report", handoff.report)
                continue

            code = call.input.get("code", "")
            if "__parse_error__" in call.input:
                output, is_error = (
                    f"Could not parse your tool arguments as JSON: "
                    f"{call.input['__parse_error__']}", True)
            else:
                emit("code", code)
                output, is_error = runner.run(code)
                emit("output", output)
                handoff.transcript.append(
                    {"code": code, "output": output, "is_error": is_error})
            results.append({"id": call.id, "content": output,
                            "is_error": is_error})

        if finished:
            provider.append_results(results)
            break

        if winding_up:
            handoff.stopped_because += " (no report after final request)"
            break

        limit = None
        if stop_check is not None and stop_check():
            # The user pressed stop: wind up rather than abort, so the run
            # still produces whatever the agent has established.
            limit = "a stop request from the user"
        elif handoff.turns >= max_turns:
            limit = f"the {max_turns}-turn cap"
        elif time.monotonic() - started >= max_seconds:
            limit = f"the {max_seconds:.0f}-second time limit"
        elif (handoff.total_input_tokens + handoff.output_tokens
              >= max_total_tokens):
            limit = f"the {max_total_tokens:,}-token budget"

        wind_up_text = ""
        if limit:
            handoff.stopped_because = f"stopped by {limit}"
            emit("limit", handoff.stopped_because)
            wind_up_text = (
                f"The investigation must end now ({limit}). run_python is no "
                "longer available to you - submit_report is the only tool you "
                "can call. Report what you have established so far; if you "
                "are not certain of the cause, say so in the finding rather "
                "than guessing.")
            winding_up = True

        provider.append_results(results, wind_up_text)
        handoff.messages = provider.messages

    handoff.seconds = time.monotonic() - started
    session.diff_since_snapshot()
    return handoff
