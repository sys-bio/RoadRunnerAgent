# Specification: libRoadRunner Agent Proof of Concept

**Status:** Draft 3
**Date:** 2026-09-02
**Supersedes:** `roadrunner-agent-spec.md` (Draft 1).

Draft 2 was written before any code ran. Draft 3 revises it against a working
implementation and real agent runs. Items marked *[measured]* were established
empirically, and several contradict Draft 2. Items marked *[new]* / *[changed]*
are Draft 2's changes from Draft 1, kept for the record.

**What measurement changed, in short:**

1. One of the five original evaluation cases does not reproduce at all - nleq2
   solves the conservation-law case by default (§9.1). Replaced.
2. Cost is dominated by turn count, not per-token price, and the cheapest
   model was not the cheapest answer (§11).
3. `submit_report` needs a `recommended_changes` field, or an agent that
   tidies up after itself leaves the user with advice they must retype (§5.1).
4. Not every model accepts the same request parameters (§5).
5. Streamlit was dropped for NiceGUI (§7).

## 1. Purpose

A Python application in which a user loads an Antimony model, simulates it, and adjusts parameters interactively. When the user observes behaviour they cannot explain or a problem they cannot fix, they hand the problem to an AI agent. The agent has unrestricted access to the libRoadRunner and libAntimony APIs, runs its own simulations and diagnostics on the live session, and returns a written report.

This is a proof of concept. The goal is to find out whether an agent with full API access produces useful diagnoses and fixes, not to ship a product.

## 2. Scope

In scope:

- Load, simulate, and edit an Antimony model in a simple GUI
- Hand a question plus the current session state to the agent
- Agent executes arbitrary Python against the session's `RoadRunner` object
- Agent may change anything: parameters, initial conditions, integrator, tolerances, the model text itself
- Agent returns a structured report
- Change log and one-step revert to the state at handoff

Out of scope:

- Sandboxing or resource limits on agent-executed code
- Multi-turn chat with the agent
- Multiple models or sessions
- Persistence beyond the running process
- Delphi or non-Python front ends

*[new]* **Security note.** `run_python` is an unsandboxed `exec` in the host process. The model can read and write any file the user can, and reach the network. This is acceptable for a single-user proof of concept on the user's own machine and is not acceptable anywhere else. Say so in the README; do not let the 8 kB output truncation be mistaken for a security boundary.

## 3. Architecture

```
+------------------+        +------------------+        +--------------------+
|  GUI             |        |  Session         |        |  Agent loop        |
|  (NiceGUI)       | <----> |  rr, original,   | <----> |  Messages API +    |
|  plot, params,   |        |  settings, log   |        |  run_python tool   |
|  ask, report     |        +------------------+        +--------------------+
+------------------+
```

Three modules, one process:

| Module | Responsibility |
|---|---|
| `session.py` | Owns the `RoadRunner` instance, the original Antimony text, the settings snapshot, and the change log |
| `agent.py` | Tool-use loop against the Anthropic Messages API; defines the `run_python` tool |
| `app.py` | GUI; calls into the other two |

### 3.1 *[new]* Rebinding

Any model edit means `te.loada(...)`, which returns a **new** `RoadRunner` object; the old one is dead. Nothing outside `Session` may hold a bare reference to `rr`. Access is always `session.rr` (a property), and the agent's execution namespace is refreshed from the session before every tool call rather than capturing `rr` once at start-up. Getting this wrong produces the nastiest failure this design admits: the agent successfully edits and validates a model that the GUI is no longer plotting.

### 3.2 *[measured]* Concurrency

A 25-turn agent loop takes minutes, so it cannot run inline on the UI thread.

Draft 2 assumed Streamlit and proposed blocking with a spinner. **NiceGUI was
used instead**, for one reason that matters to this application: Streamlit
re-runs its whole script on every widget interaction, which fights a
long-running loop, whereas NiceGUI keeps a persistent page and can stream the
agent's activity live. Watching the agent run simulations and compute
eigenvalues as it goes is the single most informative thing in the interface,
and the most persuasive in a demonstration.

The loop runs in a worker thread via `nicegui.run.io_bound`. Agent events are
appended to a `collections.deque` from that thread and drained by a
`ui.timer` on the event loop; **UI objects are never touched from the worker.**
The model and parameter widgets are disabled while a run is in flight, which
is the cheap alternative to locking `Session`.

## 4. Session

*[changed]* The session holds:

- `rr` — the single `roadrunner.RoadRunner` instance, shared by the GUI and the agent, exposed only as a property
- `source_antimony` — **the user's own Antimony text**, verbatim, as last loaded or accepted. This is the model of record, not `rr.getCurrentAntimony()` (see 4.1)
- `handoff_snapshot` — captured at the moment of handoff: `source_antimony`; the round-tripped `rr.getCurrentAntimony()` taken **immediately after `rr.reset()`**; all global parameter values; floating-species initial values; boundary species values; compartment volumes; integrator name and its full settings dict; steady-state solver name and settings; and the last simulation call (`start`, `end`, `points`, `selections`)
- `change_log` — list of `(what, before, after)` entries derived by diffing the snapshot against current state after the agent finishes

### 4.1 *[new]* The `getCurrentAntimony` trap

Verified against the roadrunner 2.9.1 installed here: `getCurrentAntimony()` serialises **current state**, not initial conditions. After a simulation, the species initialisations in the emitted text are the end-of-run values:

```
fresh             S1 = 0;                   k1 = 1;
after simulate    S1 = 2.49915896332442;    k1 = 1;
after reset()     S1 = 0;                   k1 = 1;
```

Consequences the design must respect:

- Never call `getCurrentAntimony()` without a preceding `rr.reset()`, or the text silently redefines the model's initial conditions to wherever the last run happened to end.
- The **Accept** button must not blindly set the model of record to `getCurrentAntimony()`. The round trip also drops the user's comments and reformats everything (confirmed: `// a comment describing the model` does not survive; `is "Display Name"` does). *[measured]* Resolved as follows: a **structural** rewrite replaces the source outright and says plainly that comments were lost; a **numeric** change is substituted into the user's own text in place, preserving comments and layout. The substitution compares against the handoff snapshot so only what actually changed is written - otherwise every untouched number is reformatted, and RoadRunner's implicit `default_compartment` is reported as unwritable. Lines carrying events (`at (...)`), assignment rules (`:=`) and rate rules (`S'=`) are skipped: an assignment inside an event body is not an initial value and must never be rewritten.
- Because current values leak into the text, "the Antimony text" and "the parameter values" in the handoff payload are not independent quantities. Send both anyway, but take the text post-`reset()` so the two agree.

### 4.2 *[new]* Reset semantics

Also verified: `reset()` restores species to their initial values and leaves parameters alone; `resetAll()` and `resetToOrigin()` restore parameters too (`k1=99` returns to `k1=1`). An agent that reaches for `resetAll()` between diagnostic runs will silently destroy the parameter change it is in the middle of testing, and then conclude the change had no effect. The system prompt must state the distinction explicitly (§5.4).

### 4.3 Operations

- `load(antimony_text)` — `te.loada`, sets `source_antimony`, clears the log
- `simulate(start, end, points, selections)` — thin wrapper, records the call
- `snapshot()` — captures `handoff_snapshot`
- `diff_since_snapshot()` — produces the change log
- `revert()` — reloads `source_antimony` and reapplies the snapshot's integrator and steady-state solver settings

`saveState` is not used. Reverting means reloading the Antimony text.

*[new]* `diff_since_snapshot()` detects **net** change, not activity: an agent that lowers a tolerance, tests it, and puts it back leaves no entry. That is the intended behaviour — the report's Changes section describes what the agent left behind, and the transcript (§7) is where the user sees everything it tried. The diff must also survive the id set changing under a model edit: report ids present only before as removals and only after as additions, rather than raising.

## 5. Agent

*[measured]* Model: `claude-opus-5`, with adaptive thinking
(`thinking={"type": "adaptive"}`) and `output_config={"effort": ...}`.

**Default effort is `low`, not `high`.** Draft 2 assumed this task would repay
maximum effort. On the one case measured so far it did not: opus-5 reached the
correct diagnosis at `high`, `medium` and `low`, and so did sonnet-5 at `low`
for a fifth of the price. Start at the cheapest setting and escalate only
where it demonstrably fails (§11). If the cheap setting answers the question,
the expensive one was never buying anything.

*[measured]* **Not every model takes the same request shape.** Adaptive
thinking and `output_config.effort` arrived with the 4.6 generation. Haiku 4.5
rejects both with `400: adaptive thinking is not supported on this model`, and
needs the older `thinking={"type": "enabled", "budget_tokens": N}` instead
(minimum 1024, must be below `max_tokens`). `agent.request_shape(model,
effort)` owns this, mapping `effort` onto a budget size so the setting still
means something on older models. Verified against the live API for all three
models. Any new model added to the roster needs a line in that table, not a
guess. `max_tokens=16000` on loop turns — a truncated turn is a wasted round trip, and the report itself will be nowhere near that. Use `client.messages.stream(...)` with `get_final_message()` so a long thinking turn cannot hit the HTTP timeout.

Write the loop by hand rather than using the SDK's beta tool runner: it needs a turn cap, a wall-clock cap, per-call transcript capture, and namespace refreshing between calls, and it is about fifteen lines.

### 5.1 Tool

A single tool, `run_python`:

```
name: run_python
description: Execute Python in the session. `rr` (RoadRunner), `te`
  (tellurium), `antimony`, `np` (numpy) are in scope. Return value is
  stdout plus repr of the last expression, or the exception traceback.
input: { "code": string }
```

The tool runs the code in a persistent namespace so variables carry across calls. Output is captured and truncated at a fixed length (8 kB) with a marker if truncated. Exceptions are returned as text — as a `tool_result` with `is_error: true` — never raised into the loop.

*[new]* Implementation notes, because "stdout plus repr of the last expression" is not what `exec` does:

- Parse with `ast.parse`. If the final statement is an `ast.Expr`, `exec` everything before it and `eval` that one; append `repr(value)` when not `None`. Otherwise plain `exec`.
- Re-bind `rr`, `te`, `np` from the session immediately before each call (§3.1); user-defined names persist.
- Set `np.set_printoptions(threshold=200, edgeitems=3)` in the namespace so a stray `print(result)` on a 10 000-row array cannot eat the whole budget.
- Force a headless matplotlib backend (`matplotlib.use("Agg")`) at import. The agent will try to plot, and a blocking `show()` inside the host process hangs the app.
- Truncate the middle, not the tail — a traceback's last line is the informative one.

No other tools in the first pass. Curated typed tools (`simulate`, `steady_state`, `jacobian`, …) may be added later if the agent misuses the raw API.

*[measured]* **A second tool, `submit_report`, is required, not optional.**
Draft 2 offered it as a recommendation; implementation settled it. Schema, with
`strict: true`:

```
classification        "numerical" | "structural" | "parametric" | "expected"
finding               string (Markdown)
evidence              string (Markdown)
changes               [{what, before, after}]        - left in place
recommended_changes   [{selector, value, why}]       - NOT applied
session_state         "fix applied" | "restored"
```

Two things forced this. First, the `changes` list is the one thing you want to
cross-check mechanically against `change_log` (§6), and parsing it out of prose
is the flakiest part of the design.

Second, and only visible once a real agent ran: **an agent that tidies up after
itself leaves the user with nothing to act on.** On the first real run the agent
diagnosed the Goodwin case correctly, then restored every value it had touched
and reported `session_state: restored`. Its fix - raise the Hill coefficient
above the Hopf threshold - existed only as prose. The application had no change
to apply, Accept correctly did nothing, and the user had to retype the fix by
hand. That is a diagnosis the user cannot act on with one click, which is worth
much less than one they can.

`recommended_changes` is therefore machine-applicable by construction. The
GUI renders it as a "Try it" button (§7).

*[measured]* **A numbers-only recommendation field distorts the diagnosis.**
The first full evaluation shipped a schema where a recommendation was always
`{selector, value}`. Three of eight cases produced unusable output as a
result: a rate-law fix became `set J3 = 0`, "run it for longer" became `set
end_time = 300`, and - worst - a model that was behaving *correctly* was
handed `set k2 = 0`, a change that would make the model say something the
modeller never meant. The prompt's insistence that an applicable
recommendation beats prose supplied the pressure; the narrow schema supplied
the only outlet. A schema that cannot express the right answer will be filled
with a wrong one.

The field now carries a `kind`:

| kind | fields | applied as |
|---|---|---|
| `value` | `selector`, `value` | `rr[selector] = value` |
| `model_text` | `model_text` (complete corrected Antimony) | `session.apply_model_edit(...)` |
| `simulation` | `end_time` | re-run the time course |

and the prompt states plainly that **an empty list is often the right answer**,
that a change must not be invented to have something to put there, and that a
parameter zeroed to force an expected-looking result is worse than no
recommendation at all.

*[measured]* **String fields need sanitising.** Two of eight reports carried
the model's own structured-output markers inside a string - a `finding` ending
`...no root exists.</finding> <parameter name="evidence">Computed
analytically...`. `clean_field()` truncates at a leaked close tag and strips
stray tags; the prompt also states that fields are plain text and that
evidence belongs in `evidence`. Both are needed: the prompt reduces the
frequency, the sanitiser handles what still gets through.

### 5.2 Handoff payload

The user message sent to the agent contains:

1. The user's question, verbatim
2. The full current Antimony text — `source_antimony`, and the post-`reset()` round trip if it differs
3. Current parameter and initial-condition values
4. Integrator name and settings; steady-state solver name and settings *[changed: solver added]*
5. The last simulation call and the species that were plotted
6. *[changed — resolves Open Question 1]* The last simulation's numeric result, downsampled to ~30 rows, plus per-column min, max and final value. Send it. It is cheap, it is what the user actually saw, and without it the agent's first turn is always a re-simulation that may not reproduce the user's conditions.
7. *[new, optional]* The plot the user is looking at, as a PNG image block. Questions of the form "why does this oscillation decay" are answered far faster from the picture than from thirty rows of numbers, and the model is multimodal. Cheap to add; try it in milestone 2 and keep it if it changes report quality.

*[new]* Cache the system prompt (§5.4) with `cache_control` — it carries the Antimony reference and is byte-identical across every handoff. Put the volatile payload after it, and check `usage.cache_read_input_tokens` is non-zero on the second handoff.

### 5.3 Loop

Standard Messages API tool-use loop:

1. Send system prompt + handoff message
2. While `stop_reason == "tool_use"`: execute each `run_python` call, append **all** results in a single user message, resend
3. On `end_turn`, the final text block is the report — or, with `submit_report`, on that tool call
4. Turn cap (default 25). If reached, send one final message asking the agent to report on what it has so far
5. *[new]* Wall-clock cap (default 10 minutes) and a cumulative token cap, both handled the same way as the turn cap — one final "report what you have" message, never a hard abort with nothing to show
6. *[new]* Guard `stop_reason == "refusal"` before reading `content`, and record `usage` per turn so the evaluation in §9.1 has cost numbers attached to each case
7. *[measured]* **On the wind-up turn, offer only `submit_report`.** Asking politely for a report is not enough. A real run hit the 25-turn cap, was asked to report, called `run_python` again instead, and ended with "no report after final request" - the entire budget spent and nothing to show for it. Withdrawing `run_python` for that one turn removes the choice, and is more reliable than forcing `tool_choice`, which interacts awkwardly with thinking on some models.
8. *[measured]* **Follow-up questions.** `ask(..., previous=handoff)` continues a finished conversation instead of starting over, so the agent keeps every result it computed. Measured: a first question took 2 turns and $0.04; the follow-up took 1 turn, no tool calls, and $0.02, answering from the time course it had already generated. Anything the user changed in between is described first, so it knows which of its earlier conclusions may no longer hold. Conversations cannot cross providers - the message formats differ - and the attempt is refused rather than mangled.

Keep the full `messages` list on the session after the run. Multi-turn is out of scope, but structuring it this way makes "the user pushes back once" a ten-line change rather than a rewrite — and that is the most likely thing you will want after the first evaluation.

### 5.4 System prompt

The system prompt must:

- Describe libRoadRunner briefly and state that the agent has full API access via `run_python`
- Include the Antimony reference (the existing Antimony skill document, or an excerpt) so the agent can edit model text correctly
- State the audience: a user who understands the model and the mathematics. No tutorial explanations of Jacobians, stiffness, or MCA.
- Instruct the agent to first establish what kind of problem it is — numerical, structural, parametric, or a misunderstanding of correct behaviour — before changing anything
- State explicitly that "the model is behaving correctly and here is why" is an acceptable and common answer; the agent must not invent a fix when none is needed
- Permit any change to the session but require every change to be listed in the report
- Specify the report format (§6)

*[new]* It must also carry a short list of RoadRunner-specific hazards, because these are exactly where a capable model reaches a confidently wrong conclusion:

- `reset()` restores species only; `resetAll()` and `resetToOrigin()` also restore parameters and will undo your own parameter changes. Use `reset()` between runs unless you mean otherwise.
- `getCurrentAntimony()` writes *current* values as initialisations. Call `reset()` first if you intend to serialise the model rather than the state.
- Integrator settings live on `rr.integrator` (`relative_tolerance` 1e-6, `absolute_tolerance` 1e-12, `stiff`, `maximum_num_steps` 20000, `variable_step_size`); steady-state settings on `rr.steadyStateSolver` (`nleq2`: `allow_presimulation`, `presimulation_time`, `approx_tolerance`, `maximum_iterations`). Read the actual values rather than assuming these defaults.
- A singular Jacobian at steady state usually means a conservation law, not a broken model: try `rr.conservedMoietyAnalysis = True`, or `allow_presimulation`.
- Editing the model means `te.loada(...)`, which produces a new object. Rebind it in the session; do not mutate a stale handle.
- Stochastic simulation (`rr.setIntegrator('gillespie')`) needs a fixed seed before any claim about a difference between two runs.
- Leave the session in a state you can name: either the proposed fix applied, or restored to how you found it. Say which in the report.

## 6. Report

The report has three sections, in this order:

1. **Finding** — what the problem is, classified as numerical, structural, parametric, or expected behaviour
2. **Evidence** — what was run to establish this, briefly enough that the user can follow the reasoning and reproduce it
3. **Changes** — a short list of every change made to the session, or "None"

*[changed — resolves Open Question 2]* Markdown. The GUI renders it, the model writes it well unprompted, and "predictable" is not a property plain text actually buys here.

*[measured]* A fourth element, **Recommended (not applied)**, is rendered when `recommended_changes` is non-empty, with a button that applies it (§5.1, §7.1).

The app displays the report as-is. Section 3 is cross-checked against the session's own `change_log`; discrepancies are shown to the user, since an agent that omits a change from its report is the main failure mode worth catching. *[new]* Report the cross-check in **both** directions — a change in the log but not the report (the dangerous case), and a change in the report but not the log (the agent claiming a fix it never applied, or applied and then reverted). Both are informative.

## 7. GUI

*[measured]* NiceGUI with Plotly, not Streamlit (§3.2). Layout: model source,
live values and simulation controls on the left; plot, question box, streaming
activity feed and report on the right.

- Antimony text area, Load button, example picker
- Editable value table (parameters and initial values)
- Simulation controls: start, end, points, species selection
- Interactive plot
- Question box, model and effort selectors, "Answer my question" button, Stop button
- Activity feed, streaming each `run_python` call and its output as it happens
- Report panel: classification, finding, evidence, recommended fix, change log, cross-check result, and the action buttons
- Run details at the foot of the report: model, effort, turns, wall clock, token breakdown, estimated cost, stop reason
- Copy buttons on the report and activity, yielding report + run metrics + full transcript as Markdown
- Model and parameter widgets disabled while the agent is running (§3.2)

No chat history. Each handoff is a single question -> report round trip.

### 7.1 *[measured]* Three layers, and saying which one you are changing

The interface exposes three things that users conflate, and every naming
mistake made during implementation came from failing to distinguish them:

| Layer | What it is |
|---|---|
| **Model text** | The user's Antimony source. Saved, reloaded, holds their comments. |
| **Live values** | The current RoadRunner state. What is actually simulating. |
| **Plot** | The result of the live state. |

The action buttons must name the layer they act on. What works:

- **Try it** - applies `recommended_changes` to the *live model*. Plot and values update; the text is untouched, so the user can look before committing.
- **Write into model text** - copies the live values into the *source text* (§4.1).
- **Discard** - returns the live model to the handoff state.

Two failures worth recording, because both looked like bugs in the writing
logic and were not:

1. **A no-op must not be terminal.** "Write into model text" clicked before
   "Try it" has nothing to write. The first implementation disabled the
   buttons anyway, so the subsequent "Try it" changed the live model and the
   user could no longer write it back. The button appeared broken.
2. **Loading a model must not silently arm stale actions.** Load keeps the
   previous report and activity on screen - they are the record of the last
   question - but disables their buttons, because they act on a session that
   no longer exists.

## 8. Dependencies

- Python ≥ 3.10 — *installed here: 3.12.10*
- `tellurium` *(2.2.11.1)*, `libroadrunner` *(2.9.1)*, `antimony`, `numpy` — all already present
- `anthropic` *(1.3.0)*
- `nicegui` *(3.16.0, installed in `.venv`)* - Streamlit was not used (§3.2)
- `matplotlib` — present via tellurium

Credentials: the SDK resolves `ANTHROPIC_API_KEY`, then `ANTHROPIC_AUTH_TOKEN`, then an `ant auth login` profile. Construct the client with no key in application code.

*[measured]* An **identity-linked** API key additionally requires the workspace it acts in, sent as the `anthropic-workspace-id` header. The SDK adds this automatically only for profile-based auth, so `make_client()` reads `ANTHROPIC_WORKSPACE_ID` and sets the header. Without it the API returns `400 anthropic-workspace-id is required` - which authenticates fine and looks like a credentials failure but is not.

*[measured]* The GUI runs from a `venv --system-site-packages` beside the Tellurium distribution, so installing a web framework cannot disturb the scientific stack it depends on.

## 9. Milestones

1. **Done.** `session.py` and `agent.py` driven from a script; first real agent run diagnosed `goodwin_damped` correctly, locating the Hopf bifurcation numerically at n* = 9.414 - sharper than the case's own ground truth, which was corrected to match.
2. **Done.** Three full passes at sonnet-5/low. Final: 8/8 classification, 8/8 cause (read by hand), 8/8 changes reported, 8/8 session-state honest, 1-4 turns, $0.23. The controls held every time.

   *[measured]* **Every defect the evaluation found was in the harness, not the model.** Three rounds of it: a cross-check that compared prose to log keys by substring; a recommendation schema that could only express numbers, which produced nonsense for structural fixes and pressed a correct model into a damaging one; and no way to recommend a solver setting, so the agent left the session on a different integrator and called it restored. Each was invisible until a real run produced it, and each was fixed by widening what the agent was allowed to say rather than by instructing it harder.
3. **Done early.** The GUI was brought forward from milestone 3 because a demonstration was wanted. Not regretted: several defects in the Accept/recommendation flow were only visible through it (§7.1).
4. Decide whether curated tools, sandboxing, or multi-turn are needed. Not before.

*[measured]* Doing milestone 3 before milestone 2 was the right call for a
different reason than the one it was made for: the interface exposed design
faults - a fix the user could not apply, an action that acted on the wrong
layer - that a console driver would not have surfaced at all.

### 9.1 *[measured]* Evaluation cases and rubric

Every case is a file under `cases/` holding the Antimony model, the question as
a user would phrase it, and a written ground truth the agent never sees.
**Each was verified to reproduce its documented symptom, and to be repaired by
its documented fix**; the numbers below are measured, not asserted.

| Case | Class | Verified symptom |
|---|---|---|
| `stiff_robertson` | numerical | `CV_TOO_MUCH_WORK` with `stiff` off; integrates fine with it on |
| `goodwin_damped` | parametric | damped oscillation at n=8; Hopf at n* = 9.414 |
| `missing_substrate` | structural | B reaches -38 because the rate law omits B |
| `no_steady_state` | parametric | S1 -> 899 by t=200; `steadyState` fails |
| `conserved_moiety` | expected | S1 sticks at exactly 2.0; total conserved at 10 |
| `run_too_short` | expected | S1 = 9.06 against a true steady state of 50 |
| `presupposed_bug` | expected | total conserved to 1.1e-14 - the premise is false |
| `stochastic_variation` | expected | 20 / 22 / 25 across three seeds |

**Draft 2's conservation-law case does not exist.** Draft 1 and Draft 2 both
listed "a model with a conservation law confusing `steadyState`" as a numerical
case. It does not reproduce on roadrunner 2.9.1: nleq2's `auto_moiety_analysis`
defaults to `true` and solves it without complaint. The lesson generalises -
**a case that has not been run is a guess.** Two of the five original cases
needed rework once tested, and the replacement `conserved_moiety` case tests
something different (a modeller who has forgotten the conservation law, whose
model is correct).

`no_steady_state` is the most valuable case in the set, and was not in Draft 2.
The solver reports `Jacobian matrix singular in NLEQ`, pointing squarely at
itself, while the real answer is that substrate inhibition caps the pathway's
flux at 2.612 and the input flux is 4.5, so **no steady state exists**. Both
remedies the user proposes are wrong and the error message encourages them. It
tests the numerical-versus-parametric discrimination that §5.4 asks for, under
adversarial conditions.

Four of the eight are classified `expected`. That is deliberate: over-diagnosis
is the failure mode this design is most exposed to, and `presupposed_bug` is
the sharpest test of it - the question supplies a specific, plausible, entirely
fictional number (a drift to 9.97) and the only correct answer contradicts the
user.

### 9.2 *[measured]* Scoring

`evaluate.py` scores mechanically what a machine can see, and prints the rest
beside its ground truth for a modeller to judge:

| Check | Mechanical? |
|---|---|
| Classification matches ground truth | yes |
| Every change reported (cross-checked against `change_log`) | yes |
| No change claimed that was not applied | yes |
| `session_state` matches reality - "restored" with changes left behind is a lie | yes |
| Fix applied, or a machine-applicable recommendation given | yes |
| Turns, wall clock, tokens, cost | yes |
| Is the *cause* right? Is the *fix* right? Is the evidence reproducible? | **no - read it** |

Every run is saved as JSON so a sweep can be re-scored without paying for it
again. Run each case three times: the variance between runs on one case says
more about whether this approach is dependable than any single report does.

## 10. Open questions

Resolved since Draft 2:

- ~~Send the last simulation's numeric output in the handoff~~ - yes, downsampled (§5.2).
- ~~Markdown or plain text~~ - Markdown (§6).
- ~~Is one `run_python` tool enough, or is `submit_report` needed~~ - needed, and it needs `recommended_changes` too (§5.1).
- ~~What "Accept" means for a numeric-only change~~ - in-place substitution into the user's own text (§4.1).

Still open:

- Whether sending the plot image improves reports (§5.2.7). Untested.
- Whether `low` effort holds up on the controls, `presupposed_bug` especially. Every combination tried so far has succeeded on `goodwin_damped`, which is turning out to be an easy case - the signal is clean and the method standard. The controls are where model and effort should start to matter, and nothing is known yet.
- Whether turn count discriminates quality better than cost (§11.3).
- Whether the agent should be allowed a second exchange when the user pushes back. Out of scope by decision, but the `messages` list is retained on the session so it remains a ten-line change, and it is the most likely thing to want after the first full evaluation.

## 11. *[measured]* Economics

The finding that most changes how this should be run, and the one least
predictable from the outside.

### 11.1 Cost per completed question, never per token

Measured on `goodwin_damped`, all reaching the correct diagnosis unless noted:

| Model / effort | Result | Cost |
|---|---|---|
| opus-5 / high | correct | $0.32 |
| opus-5 / medium | correct | - |
| opus-5 / low | correct | ~$0.22 |
| sonnet-5 / low | correct | **$0.05** |
| haiku-4-5 / low | **failed**, and used more turns failing | - |
| haiku-4-5 / medium | correct | $0.13 |

**The cheapest model produced the more expensive answer.** Haiku's per-token
price is half Sonnet's, but it needed more turns, and the run that finally
worked cost 2.6x Sonnet's.

The mechanism is structural, not incidental to this model or this case. Every
turn resends the entire conversation - system prompt, model, and all previous
code and results - so **input tokens grow with the square of the turn count.**
A model that needs three more turns to reach the same answer pays for its
discount several times over. This makes the turn cap the single most effective
cost control in the design, more so than model choice.

Two consequences for anyone reading a bill:

- Compare cost per *completed question*, never per token or per request.
- `input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`
  are disjoint in the API. Adding only the first two, or presenting cache reads
  as a subset of input, understates the total. (Draft 2's implementation did
  exactly this and under-reported input by a third.)

### 11.2 Start cheap, escalate on failure

Default effort is `low` and the default escalation ladder is

```
sonnet-5/low  ->  sonnet-5/medium  ->  opus-5/low  ->  opus-5/high
```

`evaluate.py --escalate` runs each case at the first rung and retries only the
runs that fail, one rung up, stopping as soon as one passes. Every attempt is
kept. For the eight-case set this is ~$0.40 if everything passes on the first
rung against ~$6.65 if everything climbs the whole ladder - so the good case is
cheaper than a single flat pass at `high`, and the bad case has bought you the
knowledge of where the money must go.

Haiku is deliberately absent from the ladder: measurement placed it above
Sonnet in cost per completed question.

**The ladder can only see mechanical failure** - a wrong classification, a
missing report, a run that hit a limit. A report that classifies correctly but
gets the cause wrong will not trigger a retry, because no automatic check can
detect that. Escalation saves money; it does not save reading (§9.2).

### 11.3 Turn count as a quality signal

*[measured]* **Rejected, at least in this form.** The hypothesis was that more
turns indicate searching rather than knowing. Across three passes the same
case ranged from 3 to 10 turns, and the 10-turn run gave the *vaguest* answer
of the three: it brute-force scanned `n = 8,10,15,20,30,50,100` by simulation
instead of computing eigenvalues. So turns track how the agent chose to
approach the problem, not how well it did. Turns remain the right thing to
watch for **cost**, since input grows with their square; they are not a
quality proxy.

### 11.4 *[measured]* What cheap models cost you

Three sonnet-5/low passes classified `goodwin_damped` correctly every time and
recommended a working fix every time - and stated the wrong number every time.
It cites the classical Griffith threshold of n > 8 for a three-step Goodwin
loop; for this parameterisation the Hopf bifurcation is at n* = 9.414, which
opus-5/high found by running `brentq` on the dominant eigenvalue's real part.

The cheap model recalls the literature; the expensive one computes the
specific answer. Both pass every mechanical check, because the classification,
the fix and the change accounting are all correct. **The difference is only
visible by reading the report against ground truth**, which is the argument
for §9.2's insistence that scoring does not replace judgement - and for
escalating on any case where a quantitative threshold is the answer.

