# libRoadRunner Agent — proof of concept

Hand a misbehaving Antimony model to an agent that has full libRoadRunner API
access, and get back a diagnosis. See `roadrunner-agent-spec-v2.md`.

**Milestone 1 (complete):** `session.py` and `agent.py` driven from a script.
**Milestone 2 (ready to run):** all 8 evaluation cases built and verified,
with a scoring harness — `evaluate.py`.
**GUI (`app.py`) also built** — brought forward from milestone 3 for demos.

## Security

`run_python` is an unsandboxed `exec` in this process. Agent-generated code
can read and write any file you can, and reach the network. This is an
accepted property of a single-user proof of concept on your own machine, and
is not acceptable anywhere else. The 8 kB output truncation is a
context-budget measure, not a boundary.

## Setup

Already done — nothing to install. `.venv\` is a private Python for this
project: a folder holding its own package list, so installing the GUI
framework here cannot disturb the Tellurium distribution. It still *sees*
Tellurium, RoadRunner, numpy and matplotlib from the main install (that is
what `--system-site-packages` did), and adds `anthropic` and `nicegui` on top.
There is nothing to "activate" — just use `.venv\Scripts\python.exe` instead
of `python` and it works.

The one thing outstanding is an API key, from
<https://console.anthropic.com/settings/keys>. In PowerShell:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # this window only
setx ANTHROPIC_API_KEY "sk-ant-..."       # permanently (reopen the terminal)
```

`setx` only affects processes started **afterwards** — the terminal you typed
it in never sees it. Close that terminal and open a new one.

**If your key is identity-linked** you will also get a 400 saying
`anthropic-workspace-id is required`. The key is fine; it just needs to know
which workspace it acts in. Find the id in the Console under
Settings → Workspaces (it looks like `wrkspc_...`), then:

```powershell
setx ANTHROPIC_WORKSPACE_ID "wrkspc_..."
```

`agent.make_client()` sends it as the `anthropic-workspace-id` header when
that variable is set. A standard, non-identity-linked key needs no workspace
id at all.

The SDK reads the key from the environment; no application code touches it.

## Running

Use `.venv\Scripts\python.exe` for everything.

```powershell
.venv\Scripts\python.exe app.py                 # the GUI, on http://localhost:8080
.venv\Scripts\python.exe run_case.py --list
.venv\Scripts\python.exe run_case.py goodwin_damped --dry-run
.venv\Scripts\python.exe run_case.py goodwin_damped --truth
.venv\Scripts\python.exe test_milestone1.py     # 102 checks, no API key needed

# milestone 2 - the evaluation
.venv\Scripts\python.exe evaluate.py --estimate --repeats 3   # cost first
.venv\Scripts\python.exe evaluate.py --cases all --repeats 3
.venv\Scripts\python.exe evaluate.py --report results\<stamp>  # reports vs truth
```

`--dry-run` prints the exact handoff payload the agent would receive and
needs no credentials — the fastest way to see what it will be told.

## Cost

The report panel shows an estimated cost per question. Model and effort are
selectable next to the "Hand it over" button, and on the command line with
`--model` / `--effort`. Indicative, from one real 7-turn run
(57,728 input / 4,511 output tokens):

| Model | Est. cost | Input / output per MTok |
|---|---|---|
| `claude-opus-5` (default) | $0.32 at `high` | $5 / $25 |
| `claude-opus-5` | ~$0.22 at `low` | $5 / $25 |
| `claude-sonnet-5` | **$0.04 at `low`, measured** | $2 / $10 |
| `claude-haiku-4-5` | ~$0.03 | $1 / $5 |

### Measured on `goodwin_damped`

| Model / effort | Result | Cost |
|---|---|---|
| opus-5 / high | correct | $0.32 |
| opus-5 / medium | correct | - |
| opus-5 / low | correct | ~$0.22 |
| sonnet-5 / low | correct | **$0.05** |
| haiku-4-5 / low | **failed**, and burned turns doing it | - |
| haiku-4-5 / medium | correct | $0.13 |

**The cheapest model is not the cheapest answer.** Haiku needed more turns,
and since every turn resends the whole conversation, its lower per-token
price was more than cancelled out: the Haiku run that worked cost 13 cents
against Sonnet's 5. Judge cost per *completed question*, never per token.
This is why the default escalation ladder starts at sonnet-5/low and does not
include Haiku at all.

### Evaluation results (milestone 2)

Three full passes at sonnet-5 / low, 8 cases each. Run 1 exposed defects;
runs 2 and 3 measured the fixes.

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| Classification correct | 8/8 | 8/8 | **8/8** |
| Cause correct (read by hand) | 8/8 | 8/8 | **8/8** |
| All changes reported | 7/8 | 7/8 | **8/8** |
| `session_state` honest | 7/8 | 7/8 | **8/8** |
| Unusable recommendations | 3 | 1 | **0** |
| Leaked markup | 2 | 1 | **0** |
| Turns | 2-4 | 2-10 | **1-4** |
| Cost | $0.23 | $0.38 | **$0.23** |

The controls held every time: `presupposed_bug` was contradicted with
evidence rather than explained away, `run_too_short` got the dull correct
answer, `stochastic_variation` identified the gillespie integrator first.

**Every defect found was in this application, not the model.** Run 1: a
cross-check that matched prose against log keys by substring; a
recommendation field that could only express numbers, which produced `set J3
= 0` for a rate-law fix and pressured a *correct* model into `set k2 = 0`.
Run 2: no way to recommend a solver setting, so the agent left the session on
a different integrator and reported `restored`. Giving it a `solver` kind and
telling it plainly that switching integrators counts as a change fixed that
in run 3 - it now recommends `integrator = cvode` and leaves the session
untouched.

**One thing the mechanical scoring cannot see.** On `goodwin_damped`,
sonnet-5/low has now three times cited the classical Griffith result that the
Hill coefficient must exceed 8, and once said "around 8-9". For *this*
parameterisation the Hopf bifurcation is at **n\* = 9.414**, which opus-5/high
computed with `brentq` on the dominant eigenvalue. Every run classifies
correctly and recommends a working fix; the cheap model recalls the textbook
where the expensive one computes the answer. Scoring says 8/8 either way -
only reading catches it.

## Layout

| File | |
|---|---|
| `session.py` | The RoadRunner instance, the user's Antimony source, snapshot, change log, revert |
| `prompts.py` | System prompt, Antimony reference, RoadRunner hazard list, handoff payload |
| `agent.py` | `run_python` executor, `submit_report` tool, the agent loop |
| `providers.py` | Model providers behind one interface: Anthropic, and any OpenAI-compatible API (DeepSeek) |
| `run_case.py` | Milestone 1 driver (console) |
| `app.py` | The GUI (NiceGUI + Plotly), including the Explore sliders |
| `cases/` | The 8 evaluation cases with ground truth (spec §9.1) |
| `evaluate.py` | Runs the case set, scores it, saves every run as JSON |
| `test_milestone1.py` | Everything testable without the API (102 checks) |

## How much the report explains

`--detail brief` (the default) or `--detail thorough`, and a dropdown in the
GUI. Comparing this agent against Claude Code on the same question and model
showed Claude Code giving much fuller answers - for three reasons, two of
them deliberate:

1. **We told it to be brief.** The prompt says *"Give them the finding, not a
   tutorial"*, and asks for a finding *"in a few sentences"*.
2. **We run at `low` effort** by default; Claude Code defaults to `xhigh`.
3. **Structured output compresses.** Four schema fields against free-form
   prose with headings and code blocks.

`thorough` replaces the terseness instructions with the opposite: give the
mechanism, name the alternatives considered and what ruled each out, say what
was *not* checked, and - directly from the `goodwin_damped` finding -
**compute a threshold for this parameterisation rather than quoting the
textbook value**. Selecting it in the GUI also raises effort from `low` to
`high`, visibly, so the coupling is not hidden.

Both levels keep the parts that make the agent *correct* rather than merely
readable: the RoadRunner hazards, the Antimony reference, "do not manufacture
a fix for a model that does not need one", and the change-reporting rule.
That matters because the eight-case results were measured with `brief`, and
the terseness sits next to the instructions that stop it over-diagnosing.
**Re-run the case set before trusting `thorough` on the controls.**

## Running against a non-Anthropic model

`providers.py` puts both vendors behind one interface, so `--model` selects
the provider by prefix:

    evaluate.py --cases all --model claude-sonnet-5
    evaluate.py --cases all --model deepseek-v4-pro     # needs DEEPSEEK_API_KEY

Model ids come from the provider's own `/models` endpoint rather than a
product name - `deepseek-v4-pro`, `deepseek-v4-flash`, base URL
`https://api.deepseek.com/v1`.

**Three differences make this not quite like for like, and the code says so
rather than hiding them:**

1. **No prompt-caching breakpoint.** The Anthropic path pins the system
   prompt with `cache_control`, so the Antimony reference is billed at a tenth
   from the second handoff on. The OpenAI dialect has no equivalent to place;
   DeepSeek caches on its own and reports `prompt_cache_hit_tokens`, which the
   adapter records. Any headline price comparison quotes *uncached* rates, so
   the real gap on this workload is smaller than the headline.
2. **Effort has a different name.** It is sent as `reasoning_effort`. The
   published docs list none/low/high/max, but probing the API directly showed
   it also accepts `minimal`, `medium` and `xhigh`, while rejecting `ultra`,
   `banana` and `""` with a 400 - so the parameter is genuinely validated and
   the documentation is merely incomplete. Every level this application offers
   passes through unmapped. Whether `medium` and `xhigh` behave differently
   from their neighbours is unmeasured: both depth probes hit the max_tokens
   cap before the model finished. `none` disables thinking entirely and has no
   Anthropic equivalent here.
3. **Tool arguments arrive as a JSON string** and are parsed by the adapter.
   Without `strict` schema enforcement a malformed report is possible, so the
   loop now asks for it again instead of crashing - a repair turn the
   Anthropic path never needs.

DeepSeek rates (from their pricing page) are time-dependent - peak hours are
01:00-04:00 and 06:00-10:00 UTC Mon-Fri, and off-peak is half price - so
`PRICING` holds a callable for them rather than a fixed tuple, and the cost
column reflects the hour the run actually happened in. Cache writes are free,
unlike the Anthropic API where they cost 1.25x input.

### First head-to-head: `presupposed_bug`

| | sonnet-5 / low | deepseek-v4-pro |
|---|---|---|
| Classification | correct | correct |
| Changes reported / honest | yes / yes | yes / yes |
| **Turns** | **3** | **12** |
| Wall clock | 29 s | 147 s |
| Input tokens | 23,867 | 125,478 |
| Output tokens | 1,406 | 7,952 |
| **Cost** | **$0.047** | **$0.099 off-peak, $0.197 peak** |

Per token DeepSeek *is* cheaper than Sonnet - $0.66/$1.98 off-peak against
$2/$10 - and its cache-hit input rate of $0.022 is roughly ten times cheaper
than Anthropic's $0.20. But it used **5.3x the input tokens and 5.7x the
output** on the same question, because it took 12 turns where Sonnet took 3.
Net: it cost **2-4x more** to answer the same question correctly.

This is the Haiku result again, from a different direction, and it is the
central economic finding of this project: **per-token price tells you almost
nothing about what an agent loop costs.** Turns dominate, because every turn
resends the whole conversation.

## The Explore panel

Sliders beside the plot vary any parameter or initial value on the live model,
redrawing as you drag. Measured on this machine, a 1000-point run of the
Goodwin model takes **6.6 ms** (1.7 ms at 200 points), so the simulation is
nowhere near the limiting factor - the round trip and the redraw are.

Slider moves are therefore *coalesced*: a move records the new value, and a
20 Hz timer applies the newest value per identifier and redraws once. Applying
every event would queue the simulation behind the slider and lag further
behind the longer you drag.

- **Copy to model text** writes the values you arrived at by dragging into
  your own Antimony source, comments and layout intact. It is the same
  in-place substitution the agent's Accept uses, referenced against the source
  rather than a handoff, so it needs no agent at all. Explore by hand, then
  keep what you found.
- **lock y-axis** freezes the scale so the shape change is visible rather than
  the axis rescaling under it.
- The reset button reloads the values from the model text.
- **Adaptive ranges.** Sliders start at 0 to twice the current value (0 to 1
  where the value is zero, which has no scale of its own), and then adapt -
  asymmetrically, because the two directions are not alike:
  - *Growing* happens **during** the drag: reaching the end of the track is
    an unambiguous request for more room, so the range doubles and the handle
    returns to mid-track. Push again for another doubling.
  - *Shrinking* happens only **on release**, and only when the value has ended
    up in the bottom quarter of the range - the one case where the slider has
    become useless because every interesting value is in the first few pixels.
    Shrinking mid-drag would move the handle under the pointer.
- Dragging `n` on the shipped Goodwin model through ~9.4 turns the damped
  decay into a sustained limit cycle in real time - the bifurcation the agent
  diagnoses, visible by hand.

## Three things worth knowing before reading the code

Each is verified against the installed roadrunner 2.9.1, and each is a place
where the obvious implementation is silently wrong.

1. **`getCurrentAntimony()` serialises current state, not initial
   conditions.** After a simulation, the species initialisations in the
   emitted text are the end-of-run values. `Session.model_antimony()` resets
   first and restores the live values afterwards.

2. **`reset()` keeps parameter changes; `resetAll()` and `resetToOrigin()`
   discard them.** Nothing here calls the latter two, and the system prompt
   warns the agent explicitly — an agent that calls `resetAll()` between
   diagnostic runs destroys the change it is testing and then reports that
   the change had no effect.

3. **A model edit means a new RoadRunner object.** `Session.rr` is a property,
   and `PythonRunner` re-binds `rr` in the agent's namespace before every tool
   call. Agent code changes the model through `session.apply_model_edit(text)`;
   calling `te.loada` directly leaves the session — and the user's plot —
   pointing at the old object.

## Status

- Session, change log, revert, model-edit rebinding: tested.
- `run_python` executor (last-expression repr, persistent namespace, error
  capture, truncation, headless matplotlib): tested.
- Report cross-check in both directions: tested.
- Both cases verified to reproduce their documented failure *and* to be fixed
  by their documented fix.
- GUI verified in a real browser: model loads, parameter edits drive the
  model live (setting `n = 12` turns the damped Goodwin into a limit cycle),
  the activity feed streams, and the report panel renders. The agent-run paths
  were exercised with a stubbed agent, which is also how the spurious
  "model text rewritten" diff entry was found and fixed.
- **First real agent run completed** (goodwin_damped): the agent reproduced
  the run, ruled out the integrator by re-running at 1e-10/1e-14 tolerances,
  and located the Hopf bifurcation numerically with `brentq` at n* = 9.414 —
  sharper than the case's own ground truth. It restored the session
  afterwards.
- **Accept** writes the agent's parameter changes back into your own Antimony
  source in place, preserving comments and layout, so the editor and the live
  model cannot drift apart. Assignments inside events, assignment rules and
  rate rules are never rewritten. A structural rewrite replaces the text
  outright and says that comments were lost.
- Report panel: cost shown at the top of the card; a copy button yields the
  report plus full transcript; Accept and Revert no longer destroy the report,
  they disable themselves and confirm in place.
