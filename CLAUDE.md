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
- `test_milestone1.py`: **223 checks**, no API key needed. Run it after any
  change; it is the safety net for everything below.
- `test_streamlit_app.py`: **53 checks** on the hosted build, also free -
  real page builds driven through real widgets via Streamlit's `AppTest`,
  including the password-gated path.

**Current task:** deploying `streamlit_app.py` - the agent UI, now ported to
Streamlit and verified in a browser. The simulation stack is proven on Linux
and the sandbox is verified with a real key. See "Where we are" at the
bottom.

## Environment

- Python 3.12 from the Tellurium WinPython distribution; `.venv/` beside the
  project adds `nicegui`, `anthropic`, `openai` on top of it.
- **The user runs Git Bash inside Windows Terminal.** Write bash, not
  PowerShell. Paths use forward slashes.
- The Anthropic key lives in **`RRAGENT_ANTHROPIC_KEY`**, not
  `ANTHROPIC_API_KEY`. Claude Code reads the latter out of the environment
  for its own authentication, and an identity-linked key adopted that way is
  sent without the workspace header it requires - so every request 400s and
  the CLI becomes unusable until the variable is removed. That happened once.
  `ANTHROPIC_API_KEY` is still honoured as a fallback
  (`providers.REGISTRY["anthropic"]["key_env"]` lists both, preferred first);
  do not set it on a machine that also runs Claude Code.
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
.venv/Scripts/python.exe test_milestone1.py         # 223 checks, free
.venv/Scripts/python.exe evaluate.py --estimate --cases all
.venv/Scripts/python.exe evaluate.py --cases all --model claude-sonnet-5
.venv/Scripts/python.exe evaluate.py --report results/<stamp>
.venv/Scripts/python.exe evaluate.py --rescore results/<stamp>   # free
.venv/Scripts/python.exe evaluate.py --cases all --sandbox       # in a worker
RRAGENT_SANDBOX=0 .venv/Scripts/python.exe app.py                # GUI, unsandboxed
```

## Layout

| file | |
|---|---|
| `session.py` | RoadRunner instance, user's Antimony source, snapshot, change log, revert, accept |
| `agent.py` | `run_python` executor, `submit_report` schema, the agent loop |
| `providers.py` | Anthropic and OpenAI-compatible (DeepSeek) behind one interface |
| `prompts.py` | System prompt (brief/thorough), Antimony reference, hazards, handoff payload |
| `app.py` | NiceGUI GUI - the only UI-specific file; runs on a `WorkerSession` |
| `worker.py` | The subprocess that owns the Session and runs agent code |
| `remote.py` | Host-side `WorkerSession`: spawns the worker, proxies the Session |
| `evaluate.py` | Runs the case set, scores it, saves JSON |
| `cases/` | 8 cases with ground truth |
| `streamlit_app.py` | **The hosted app** - the agent UI, ported to Streamlit |
| `pages/2_Deployment_probe.py` | The old probe: proves the stack installs on Linux |
| `test_streamlit_app.py` | 53 headless checks on the hosted build, no key needed |
| `gate.py` | The shared password gate - every page must call it |

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

Confirmed again on 2026-09-03: sonnet asserted "at n=8 exactly the fixed
point is right at the Hopf bifurcation" when max Re(eig) is -0.054 there,
nowhere near marginal. Every mechanical check still passed.

The same run showed that `classification` wobbles between **parametric** and
**expected** on this case - three earlier sonnet-5/low runs said parametric,
this one said expected - while the prose, the cause and the recommendation
were identical and correct each time. The two labels genuinely both fit a
model that is right, an output that is right, and a fix that is a parameter
change. Treat a single classification miss on `goodwin_damped` as sampling
variance, not a regression; the handoff is byte-identical run to run.

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
takes either. **`app.py` does this by default** - one worker per browser tab,
closed when the tab disconnects. Set `RRAGENT_SANDBOX=0` for the old
in-process behaviour, which is faster to start and easier to debug and which
lets agent code read every key the GUI can. The in-process `PythonRunner`
remains for that mode and for the tests.

Two things the proxy has to get right, both of which bit once:

- **Attribute writes must cross the pipe.** `app.py` does
  `session.last_sim = None`; without `WorkerSession.__setattr__` that
  silently makes a proxy-local attribute shadowing the worker's, and the two
  disagree from then on.
- **`apply_recommendation` runs inside the worker.** It reaches
  `rr.setIntegrator` and `rr.integrator.setValue` - solver objects that
  cannot cross a pipe - so the whole call is one op, not several. Case
  `SETUP` functions reach the same objects and go over as `setup_case`.
- **An exception must arrive wearing its own type name.** Callers format
  errors as `f"{type(exc).__name__}: {exc}"`, so folding the type into the
  message in the worker produced `RuntimeError: RuntimeError: CVODE Error
  ...` - which went into the question the agent was asked, and nothing
  failed loudly. The type crosses as its own field and `remote._worker_error`
  rebuilds a `WorkerError` subclass wearing that name.

`evaluate.py --sandbox` runs the case set this way; without the flag it uses
an in-process Session, which starts ~4.5s sooner per case. All 8 cases build
byte-identical handoffs either way, except `stochastic_variation`, which
differs by its RNG seed - which is the point of that case.

**The sandbox is transparent to the agent loop, measured with a real key**
(2026-09-03, `goodwin_damped`, sonnet-5/low): 3 turns, 20.1s, $0.0496,
report produced, session restored honestly, no phantom changes - the same
shape and price as the three in-process runs of that case (3-4 turns,
$0.037-0.048). The worker subprocess costs nothing in agent behaviour.

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

1. **Sandboxing is half done.** Credential theft is closed (see "Security")
   and the GUI runs on it. Filesystem, network and resource abuse are not
   closed, and need a container per session, which Community Cloud cannot
   give - one container serves every viewer.
2. ~~There is no Streamlit UI for the agent.~~ **Done, 2026-09-03.**
   `streamlit_app.py` is now the agent UI: model text, live values with a
   slider each, plot, the question box, a streamed activity feed and the
   full report with Try it / Discard / Write into model text. Verified in a
   browser against a sandboxed worker - dragging `n` from 8 to 12 turns the
   damped wobble into a sustained limit cycle, which is the case file's
   measured physics. `app.py` (NiceGUI) remains the local build.

   Two things did not port, and both are inherent to Streamlit re-running
   the script on every interaction:

   - **No Stop button.** While `agent.ask` blocks there is no interaction to
     catch, so `stop_check` can never fire. The turn/second/token budgets in
     the sidebar are the only brakes.
   - **A slower explore loop.** Simulation is ~6 ms; the script re-run is
     what costs. Usable, not the NiceGUI build's 60 fps.

   The spec's stated reason for dropping Streamlit (§7 - reruns fight a
   long-running loop) turned out **not** to apply to the activity feed:
   during a run nothing can touch a widget, so `on_event` paints into a
   container as events arrive and the feed streams fine.

### Five Streamlit rules this port had to learn

All five produced a working-looking page that was quietly wrong.

- **`streamlit run` re-executes the whole file on every interaction.** A
  module-level `_SESSIONS = {}` is therefore rebuilt each time, so the app
  made a fresh unloaded Session on every click and nothing stayed loaded.
  The registry must be `@st.cache_resource`, the one store that outlives a
  rerun. `test_streamlit_app.py` pins this down.
- **Writing to a widget-backed key after its widget exists raises.** The
  report's buttons rewrite the model text box, which is rendered above them.
  Hence `queue()` / `apply_pending()`: park the value, apply it at the top of
  the next run.
- **Seed session state *after* the password gate, not before.** Streamlit
  discards state for widgets a run did not render. `init_state()` ran ahead
  of `password_gate()`, so `source`, `question`, `start`, `end` and `points`
  were seeded on a run that built none of their widgets and were gone by the
  time the boxes were drawn - empty fields, and a point count under the
  solver's minimum, so the error named the solver rather than the field. The
  plot still rendered, which made it look like a display bug. **This is
  invisible locally unless a password is set**, which is why it reached the
  deployment; `test_streamlit_app.py` now writes a temporary
  `.streamlit/secrets.toml` and drives the gated path.
- **Every page needs its own gate.** `pages/` are independently reachable,
  by URL and from the sidebar, so `password_gate()` in `streamlit_app.py`
  guarded that file and nothing else - the probe page was open to anyone
  with the deployment's address. The gate lives in `gate.py` and both pages
  call it; `authorised` is session state, so answering once covers the app.
- **`st.secrets` is cached once read.** A test that configures a password
  cannot un-configure it for later tests in the same process, so the gated
  checks run last.
- **There is no disconnect event.** `app.py` kills a tab's worker from
  `client.on_disconnect`; Streamlit has no equivalent, so a closed tab would
  leak a subprocess until the box died. Sessions carry a last-touched stamp
  and `reap_idle_sessions` closes them after 30 minutes.

### Next actions, in order

**Done, 2026-09-03 - do not redo:** the API key is replaced and persisted as
`RRAGENT_ANTHROPIC_KEY`; one case has been run end to end through the
sandbox with it; and the Streamlit UI is built and verified. The numbers are
under "Security" and "Economics" above. To re-check the key without
printing it:

```bash
# //v, not /v - MSYS rewrites a lone /v into a Windows path, and reg
# then reports Invalid syntax with exit status 0, which reads as
# "not set" when it is set.
reg query 'HKCU\Environment' //v RRAGENT_ANTHROPIC_KEY >/dev/null 2>&1 && echo persisted
echo "key: ${RRAGENT_ANTHROPIC_KEY:+set}"
```

1. **Run one agent question through the hosted UI.** Everything else about
   it is verified headlessly and in a browser; what no test covers is the
   streamed feed and the report panel during a real run, because that needs
   a key pasted into the sidebar. Paste it, click **Answer my question**,
   and watch the feed. About $0.05.

2. **Deploy it.** Point Community Cloud at `streamlit_app.py` (it currently
   serves the probe, which is now `pages/2_Deployment_probe.py` and still
   reachable from the sidebar), reboot after the requirements change, and
   set `APP_PASSWORD` in the app's secrets. Without that password the app is
   open to anyone with the URL - `password_gate()` allows it, deliberately,
   so that running locally needs no setup, which makes forgetting it on the
   deployment the easy mistake.

3. **The hosting question is still open, and the answer has not changed.**
   Community Cloud is now a real option rather than a port away - but it

   still cannot isolate viewers from each other, and that has not been
   fixed by anything above.

   - *Community Cloud* is deployable today. One container serves every
     viewer, so it is defensible only for a few named colleagues behind
     `APP_PASSWORD`, each bringing their own key - which is exactly how
     `streamlit_app.py` is written.
   - *Fly / Render / Cloud Run* runs `app.py` as-is - NiceGUI is ASGI, so
     there is no UI port at all - and can give a container per session, which
     is the only real answer to the filesystem, network and CPU abuse the
     worker subprocess deliberately does not close.

   **The recommendation is still the second**, but the argument has changed:
   the UI port is no longer the cost, because it is done. What remains is
   only that Community Cloud cannot give a container per session. Ship on
   Community Cloud now for colleagues; move to Fly or Cloud Run when it
   needs to face anyone else.

2. **Once hosting is chosen**, the work behind it is: a Dockerfile pinning the
   host's resolved set (Python 3.11, `libroadrunner 2.10.0`, `numpy 2.2.6`,
   tellurium - see "What the host installs"), a container or equivalent
   boundary per session to close filesystem/network/CPU, and bring-your-own-key
   in the GUI so the deployment holds no credential of the lab's.

3. **Optional, cheap, not blocking:** re-run the full set through the sandbox
   (`--cases all --sandbox`, ~$0.40 at sonnet-5/low) to confirm the 8/8 result
   holds there as well as in-process. One case already shows it does.
