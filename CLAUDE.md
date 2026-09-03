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
- `test_milestone1.py`: **211 checks**, no API key needed. Run it after any
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
| `worker.py` | The subprocess that owns the Session and runs agent code |
| `remote.py` | Host-side `WorkerSession`: spawns the worker, proxies the Session |
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

## Security - credential theft is closed, the rest is not

`run_python` used to be an unsandboxed `exec` in the host process: agent code
could read `os.environ` and walk off with every key there.

**That is now fixed.** `worker.py` holds the Session and the exec namespace in
a subprocess started with an *allowlisted* environment (`remote.py`), so there
are no credentials in the process where agent code runs. An allowlist, not a
denylist - a denylist has to anticipate every name worth stealing, and the one
it forgets is the one that matters. The worker also gets a scratch home
directory rather than the user's, because tellurium's import chain reaches
parso, which demands a home, and the real one points at `~/.aws` and `~/.ssh`.

Use it by passing a `WorkerSession` where a `Session` would go; `agent.ask()`
takes either. The in-process `PythonRunner` remains for single-user local runs
and for the tests.

Three properties the tests pin down, because a silent regression here hands a
stranger an API key:

- a key set in the parent is not visible in the child, nor to agent code;
- a process death (`os._exit`, a native segfault) and a hang both come back as
  ordinary error results, and the worker restarts with the model reloaded;
- state persists across tool calls, so `rr` and the agent's variables survive
  between turns exactly as they did in-process.

**Still open, and unfixable in one shared container:** the filesystem, the
network, and CPU/memory. Agent code in the worker can still read what the host
user can read and open sockets. Only per-session containers close those, which
Streamlit Community Cloud cannot give - one container serves every viewer.

**Nothing crossing the boundary is ever pickled.** The worker runs untrusted
code; unpickling what it sends would hand that code the host process it was
moved out of. JSON only, one object per line.

## Where we are

`streamlit_app.py` - a probe, not the app - is deployed to Streamlit
Community Cloud from `sys-bio/RoadRunnerAgent`, branch `main`. **The whole
simulation stack now runs green there.** Nothing about hosting the
simulation blocks progress; what remains is security and the UI.

### What the host installs

94 packages, Python 3.11, `libroadrunner 2.10.0`, `antimony 3.1.3`,
`numpy 2.2.6`, tellurium 2.2.13.1. The three behaviours in "Verified facts"
below were only ever checked against the local 2.9.1; the probe confirms
they still hold on 2.10.0.

- **numpy must be pinned to `2.2.6`.** The first deploy segfaulted at
  startup with no traceback. The 1.x/2.x ABI theory was **wrong** and
  should not be retried: libroadrunner 2.10.0 declares `numpy~=2.2`, so
  `numpy<2` cannot resolve at all - it fails the install outright.
- **tellurium is required**, despite the ~114 MB. `session.py` calls
  `te.loada` in five places, `agent.py` puts `te` in the agent namespace
  and imports matplotlib, and `prompts.py` promises the agent `te` is
  there. The tellurium-free build died at import - a fault the probe never
  caught, because it never imported `session`. No conflict: tellurium asks
  only for `numpy>=1.23` and `libroadrunner>=2.8`. Its heaviest dependency,
  rrplugins, is Windows-only by marker and never installs on the host.

### Operating the deployment

- **The app is in the `sys-bio` workspace, not a personal one.**
  share.streamlit.io shows an empty dashboard and offers to create an app
  if you are in the wrong one - use the switcher, top right. Sign-in
  identity matters too: GitHub, Google and email logins are separate
  accounts even with one address.
- **Reboot after a `requirements.txt` change** (**⋮ -> Reboot app**);
  auto-rebuild on push is not prompt. **Manage app** opens the build log.
- Anything deployed here is public under the lab's name.

### Check a requirements change before pushing

Resolve against the host's platform rather than learning from a failed
deploy. Pass the older manylinux tags too - a `manylinux2014` wheel runs
fine on Debian, but `--platform manylinux_2_28_x86_64` alone will not match
it and invents conflicts that do not exist:

```bash
.venv/Scripts/python.exe -m pip install --dry-run --ignore-installed \
  --report rep.json --python-version 3.11 --only-binary=:all: \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --platform manylinux_2_17_x86_64 --platform any \
  --target ./pipdry -r requirements.txt
```

And read what a package actually demands before pinning around it:

```bash
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('https://pypi.org/pypi/libroadrunner/2.10.0/json'))['info']['requires_dist'])"
```

### What blocks hosting the agent

1. **Sandboxing is half done.** Credential theft is closed (see "Security");
   filesystem, network and resource abuse are not, and need a container per
   session, which Community Cloud cannot give. `app.py` has not been moved
   onto `WorkerSession` yet - it still holds an in-process `Session` and
   touches `session.rr` in five places, all of them ids and single values
   that `remote.RemoteModel` already covers.
2. **There is no Streamlit UI for the agent.** `app.py` is NiceGUI
   (1,057 lines); `streamlit_app.py` is only the probe. Hosting on
   Community Cloud means porting the UI; hosting somewhere that runs a
   long-lived ASGI process (Fly, Render, Cloud Run) means `app.py` runs
   as-is *and* per-session containers become possible, which is the only
   real answer to (1).
