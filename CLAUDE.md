# RoadRunner Agent - working notes

A proof of concept: a modeller hands a misbehaving Antimony model to an agent
that has full libRoadRunner API access, and gets back a diagnosis.

Read `roadrunner-agent-spec-v2.md` (Draft 3) for the design and its reasoning,
and `README.md` for measured results. This file is the short version plus the
things that are easy to get wrong.

## Status

Milestones 1 and 2 are **done**. The GUI was brought forward and is done too.

- 8 evaluation cases, each verified to reproduce its symptom *and* to be
  repaired by its documented fix.
- Best measured run: **8/8 classification, 8/8 cause, 8/8 changes reported,
  8/8 session-state honest, $0.23** for the whole set at sonnet-5/low.
- `test_milestone1.py`: **191 checks**, no API key needed. Run it after any
  change; it is the safety net for everything below.

**Current task:** deploying a probe to Streamlit Community Cloud to decide
whether the agent can be hosted. See "Where we are" at the bottom.

## Environment

- Python 3.12 from the Tellurium WinPython distribution; `.venv/` beside the
  project adds `nicegui`, `anthropic`, `openai` on top of it.
- **The user runs Git Bash inside Windows Terminal.** Write bash, not
  PowerShell. Paths use forward slashes.
- API keys live in environment variables set with `setx` (persisted in the
  Windows user registry). **Never in files in the repo** - two were once
  committed to a public repo and auto-revoked by GitHub secret scanning
  within minutes. `.gitignore` blocks `*Key.txt`, `.venv/`, `results/`.
- `setx` only affects processes started *afterwards*. A shell that predates
  it will not see the variable. This has caused confusion more than once.
- The Anthropic key is **identity-linked**, so it needs an
  `anthropic-workspace-id` header; `ANTHROPIC_WORKSPACE_ID` supplies it via
  `agent.make_client()`. Without it the API returns 400, which looks like an
  auth failure but is not.

```bash
.venv/Scripts/python.exe app.py                     # GUI on :8080
.venv/Scripts/python.exe test_milestone1.py         # 191 checks, free
.venv/Scripts/python.exe evaluate.py --estimate --cases all
.venv/Scripts/python.exe evaluate.py --cases all --model claude-sonnet-5
.venv/Scripts/python.exe evaluate.py --report results/<stamp>
.venv/Scripts/python.exe evaluate.py --rescore results/<stamp>   # free
```

## Layout

| file | |
|---|---|
| `session.py` | RoadRunner instance, user's Antimony source, snapshot, change log, revert, accept |
| `agent.py` | `run_python` executor, `submit_report` schema, the agent loop |
| `providers.py` | Anthropic and OpenAI-compatible (DeepSeek) behind one interface |
| `prompts.py` | System prompt (brief/thorough), Antimony reference, hazards, handoff payload |
| `app.py` | NiceGUI GUI - the only UI-specific file (1,057 lines; the rest is UI-agnostic) |
| `evaluate.py` | Runs the case set, scores it, saves JSON |
| `cases/` | 8 cases with ground truth |
| `streamlit_app.py` | Deployment probe for Streamlit Cloud (not the app) |

## Verified facts - do not re-derive, do not assume

All checked against the installed roadrunner 2.9.1. Each one silently
produces a wrong answer if ignored.

- **`getCurrentSBML()` serialises current state; `getSBML()` gives the
  model.** tellurium's `getCurrentAntimony()` wraps the former, so it emits
  end-of-run values as initial conditions. `Session.model_antimony()` resets
  first and restores after.
- **`reset()` keeps parameter changes. `resetAll()` and `resetToOrigin()`
  discard them.** Never call the latter two.
- **A model edit returns a NEW RoadRunner object.** `Session.rr` is a
  property; `PythonRunner` rebinds `rr` before every tool call. Agent code
  edits via `session.apply_model_edit(text)`.
- **In Antimony the semicolon is a separator, not a terminator.** `n = 8`
  alone on a line is valid; only two statements on one line with nothing
  between them is an error.
- **nleq2 solves conserved-moiety models by default** (`auto_moiety_analysis`
  is true), so the classic "conservation law breaks steadyState" case does
  not reproduce.
- Without tellurium, id accessors move to `rr.model.getFloatingSpeciesIds()`
  etc., and `getCurrentAntimony` becomes
  `antimony.loadSBMLString(rr.getSBML())` + `getAntimonyString(...)`.
- Simulation is ~5-7 ms for 1000 points, so the model is never the bottleneck.

## Economics - the central finding

**Cost per completed question, never per token.** Every turn resends the whole
conversation, so input grows with the square of the turn count. Measured:

| | result | cost |
|---|---|---|
| sonnet-5 / low | correct | **$0.05** |
| opus-5 / high | correct | $0.32 |
| haiku-4-5 / low | **failed** | - |
| haiku-4-5 / medium | correct | $0.13 |
| deepseek-v4-pro | correct, 12 turns vs 3 | $0.10-0.20 |

Cheaper models were *more* expensive per answer, twice, from different
vendors. Default is `claude-opus-5` at `low` effort; `evaluate.py --escalate`
climbs `sonnet-5:low -> sonnet-5:medium -> opus-5:low -> opus-5:high`.

Turn count is **not** a quality proxy - a 10-turn run gave a vaguer answer
than a 3-turn one. It matters for cost only.

Opus computes; Sonnet recalls. On `goodwin_damped`, opus found the Hopf
bifurcation at **n\* = 9.414** with `brentq`; sonnet quotes the textbook value
of 8 every time. Both pass every mechanical check. Only reading catches it -
which is why `--detail thorough` tells the agent to compute thresholds for
*this* parameterisation.

## How to work on this

- **Verify against the running system.** Two of the five original evaluation
  cases did not reproduce; the documentation for DeepSeek's effort levels was
  incomplete; a "definitely fine" assumption about semicolons was wrong. Run
  the thing.
- **Every defect the evaluation found was in this harness, not the model** -
  a cross-check matching prose by substring, a recommendation schema that
  could only express numbers, no way to name a solver setting. *A schema that
  cannot express the right answer will be filled with a wrong one.*
- Add a regression test for anything a real run exposes.
- Do not spend the user's API credits without saying so. `--estimate` first.

## Security - unresolved, and it gates hosting

`run_python` is an unsandboxed `exec` in the host process. Acceptable for one
user on their own machine; **not acceptable hosted**. A public app means
strangers' code running in your container, reaching every key in the
environment and every other viewer's data. Bring-your-own-key fixes billing,
not this.

Prerequisite for hosting: move `run_python` into a worker subprocess with an
empty environment, holding the RoadRunner instance for the session so `rr`
state still persists across tool calls. That closes credential theft; it does
not close filesystem, network or resource abuse, which need per-session
containers (Community Cloud cannot give those - one container serves all
viewers).

## Where we are

Deploying `streamlit_app.py` to Streamlit Community Cloud
(`sys-bio/RoadRunnerAgent`, public, main file `streamlit_app.py`).

- **Install works.** 51 packages, `libroadrunner 2.10.0`, `antimony 3.1.3`,
  Python 3.11.16, no memory trouble. `requirements.txt` deliberately omits
  tellurium (saves ~114 MB of wheels).
- **The probe now runs green on the host.** `libroadrunner==2.10.0` on
  `numpy==2.2.6` imports cleanly, and all three behaviours in "Verified
  facts" below still hold on 2.10.0 - they were only ever checked against
  the local 2.9.1. The hosted simulation stack is therefore viable.
- The startup segfault is resolved by the numpy pin. The 1.x/2.x ABI theory
  was **wrong** and should not be retried: libroadrunner 2.10.0 declares
  `numpy~=2.2`, so `numpy<2` cannot even resolve - it fails the install.
  Check a package's `requires_dist` before pinning around it:

```bash
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('https://pypi.org/pypi/libroadrunner/2.10.0/json'))['info']['requires_dist'])"
```

**One blocker remains before the agent itself can be hosted.**

`run_python` is still an unsandboxed `exec` in the host process. See
"Security" above; this is the real gate, and it is unchanged.

A second blocker was closed by shipping tellurium in `requirements.txt`
after all. `session.py` calls `te.loada` in five places, `agent.py` puts
`te` in the agent namespace and imports matplotlib, and `prompts.py`
promises the agent `te` is there - so the tellurium-free build died at
import, a fault the probe never saw because it never imported `session`.
Resolving for the host (Linux, py3.11) gives 94 packages with the
`numpy==2.2.6` and `libroadrunner==2.10.0` pins intact; tellurium asks only
for `numpy>=1.23` and `libroadrunner>=2.8`, and its heaviest Windows-only
dependency, rrplugins, never installs there.

**Check a requirements change against the host's platform before pushing**,
rather than learning from a failed deploy. Pass the older manylinux tags
too - a `manylinux2014` wheel runs fine on Debian, but `--platform
manylinux_2_28_x86_64` alone will not match it and invents conflicts:

```bash
.venv/Scripts/python.exe -m pip install --dry-run --ignore-installed   --report rep.json --python-version 3.11 --only-binary=:all:   --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64   --platform manylinux_2_17_x86_64 --platform any   --target /tmp/pipdry -r requirements.txt
```
