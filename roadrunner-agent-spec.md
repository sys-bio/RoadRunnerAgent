# Specification: libRoadRunner Agent Proof of Concept

**Status:** Draft
**Date:** 2026-09-02

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

## 3. Architecture

```
+------------------+        +------------------+        +--------------------+
|  GUI             |        |  Session         |        |  Agent loop        |
|  (Streamlit)     | <----> |  rr, original,   | <----> |  Messages API +    |
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

## 4. Session

The session holds:

- `rr` — the single `roadrunner.RoadRunner` instance, shared by the GUI and the agent
- `original_antimony` — the Antimony text at the last user load or accept
- `handoff_snapshot` — captured at the moment of handoff: Antimony text (`rr.getCurrentAntimony()`), all parameter values, initial conditions, integrator name and settings, last simulation call (`start`, `end`, `points`, `selections`)
- `change_log` — list of `(what, before, after)` entries derived by diffing the snapshot against current state after the agent finishes

Operations:

- `load(antimony_text)` — `te.loada`, sets `original_antimony`, clears the log
- `simulate(start, end, points, selections)` — thin wrapper, records the call
- `snapshot()` — captures `handoff_snapshot`
- `diff_since_snapshot()` — produces the change log
- `revert()` — reloads `original_antimony` and reapplies the snapshot's integrator settings

`saveState` is not used. Reverting means reloading the Antimony text.

## 5. Agent

### 5.1 Tool

A single tool, `run_python`:

```
name: run_python
description: Execute Python in the session. `rr` (RoadRunner), `te`
  (tellurium), `antimony`, `np` (numpy) are in scope. Return value is
  stdout plus repr of the last expression, or the exception traceback.
input: { "code": string }
```

The tool runs the code with `exec` in a persistent namespace so variables carry across calls. Output is captured and truncated at a fixed length (e.g. 8 kB) with a marker if truncated. Exceptions are returned as text, never raised into the loop.

No other tools. Curated typed tools (`simulate`, `steady_state`, `jacobian`, etc.) may be added later if the agent misuses the raw API, but not in the first pass.

### 5.2 Handoff payload

The user message sent to the agent contains:

1. The user's question, verbatim
2. The full current Antimony text
3. Current parameter and initial-condition values
4. Integrator name and settings
5. The last simulation call and the species that were plotted
6. Optionally, the numeric result of the last simulation (first and last rows, or a downsample) so the agent sees what the user saw

### 5.3 Loop

Standard Messages API tool-use loop:

1. Send system prompt + handoff message
2. While `stop_reason == "tool_use"`: execute each `run_python` call, append results, resend
3. On `end_turn`, the final text block is the report
4. Turn cap (default 25). If reached, the agent is sent one final message asking it to report on what it has so far

Model: whichever current model is configured; not fixed in this spec. Use a moderate `max_tokens` for the report (4k is enough).

### 5.4 System prompt

The system prompt must:

- Describe libRoadRunner briefly and state the agent has full API access via `run_python`
- Include the Antimony reference (the existing Antimony skill document, or an excerpt) so the agent can edit model text correctly
- State the audience: a user who understands the model and the mathematics. No tutorial explanations of Jacobians, stiffness, or MCA.
- Instruct the agent to first establish what kind of problem it is — numerical, structural, parametric, or a misunderstanding of correct behaviour — before changing anything
- State explicitly that "the model is behaving correctly and here is why" is an acceptable and common answer; the agent must not invent a fix when none is needed
- Permit any change to the session but require every change to be listed in the report
- Specify the report format (§6)

## 6. Report

The agent's final message is plain prose with three sections, in this order:

1. **Finding** — what the problem is, classified as numerical, structural, parametric, or expected behaviour
2. **Evidence** — what was run to establish this, briefly enough that the user can follow the reasoning and reproduce it
3. **Changes** — a short list of every change made to the session, or "None"

The app displays the report as-is. Section 3 is cross-checked against the session's own `change_log`; discrepancies are shown to the user, since an agent that omits a change from its report is the main failure mode worth catching.

## 7. GUI

Minimal, in Streamlit (or Dash; either is fine):

- Text area for Antimony, Load button
- Parameter table with editable values
- Simulation controls: start, end, points, species selection
- Plot
- Text box: "Ask the agent", Submit button
- Report panel: report text, change log, Revert button, Accept button (sets `original_antimony` to current)
- Collapsible transcript of every `run_python` call and its output, for inspection

No chat history. Each handoff is a single question → report round trip.

## 8. Dependencies

- Python ≥ 3.10
- `tellurium`, `libroadrunner`, `antimony`, `numpy`
- `anthropic`
- `streamlit` (or `dash`)

## 9. Milestones

1. `session.py` and `agent.py` working from a script: load a model, hand off a canned question, print the report. No GUI.
2. Test against a small set of known cases: a stiff model with default tolerances, a model with a conservation law confusing `steadyState`, a model with a mis-typed rate law, an oscillator whose parameters put it outside the oscillatory regime, and a model behaving correctly but unexpectedly. Judge report quality by hand.
3. GUI.
4. Decide whether curated tools, sandboxing, or multi-turn are needed. Not before.

## 10. Open questions

- Whether to send the last simulation's numeric output in the handoff, or let the agent re-run it. Sending it is cheaper and avoids the agent re-simulating; re-running is simpler.
- Whether the report should be Markdown or plain text. Markdown renders better; plain text is more predictable.
