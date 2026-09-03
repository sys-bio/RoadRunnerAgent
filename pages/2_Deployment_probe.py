"""Deployment probe for Streamlit Community Cloud.

The first deployment segfaulted: the packages installed cleanly and then the
process died in native code, so no Python traceback ever appeared. A segfault
cannot be caught in-process, so every risky import and every RoadRunner call
happens in a SUBPROCESS here. A crash then shows up as an exit code
(-11 = SIGSEGV) attached to a named step, instead of taking the app with it.

What it answers:

  1. Which import, if any, crashes - numpy, roadrunner, antimony.
  2. Whether the three RoadRunner behaviours this project relies on still
     hold on whatever version the host installed (verified locally on 2.9.1;
     the host installs 2.10.0).
  3. Simulation speed and peak memory.

It runs no agent and needs no API key.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import textwrap

import streamlit as st

st.set_page_config(page_title="RoadRunner stack probe", page_icon="🧪",
                   layout="centered")
st.title("RoadRunner stack probe")
st.caption("Where does the simulation stack break on this host?")


def run(label: str, code: str, timeout: int = 120) -> dict:
    """Execute code in a fresh interpreter; report how it ended.

    Returns a dict with `ok`, `returncode`, `signal` (a readable name where
    the process was killed), `stdout` and `stderr`.
    """
    try:
        done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"label": label, "ok": False, "returncode": None,
                "signal": "timeout", "stdout": "", "stderr": ""}
    signals = {-11: "SIGSEGV (segmentation fault)", -6: "SIGABRT",
               -9: "SIGKILL (out of memory?)", -4: "SIGILL", -8: "SIGFPE"}
    return {"label": label, "ok": done.returncode == 0,
            "returncode": done.returncode,
            "signal": signals.get(done.returncode, ""),
            "stdout": done.stdout.strip(), "stderr": done.stderr.strip()[-1500:]}


def show(result: dict) -> bool:
    if result["ok"]:
        st.success(f"{result['label']} - OK"
                   + (f"\n\n```\n{result['stdout']}\n```" if result["stdout"]
                      else ""))
        return True
    detail = result["signal"] or f"exit code {result['returncode']}"
    st.error(f"{result['label']} - FAILED ({detail})")
    if result["stderr"]:
        st.code(result["stderr"], language="text")
    return False


# --------------------------------------------------------------- 1. versions

st.subheader("1. What was installed")
rows = {"python": platform.python_version(),
        "platform": platform.platform(),
        "libc": " ".join(platform.libc_ver()) or "n/a"}
import importlib.metadata as md
for package in ("libroadrunner", "antimony", "numpy", "streamlit", "plotly",
                "anthropic", "openai", "tellurium", "pandas"):
    try:
        rows[package] = md.version(package)
    except md.PackageNotFoundError:
        rows[package] = "not installed"
st.table({"": list(rows), "value": list(rows.values())})

# ---------------------------------------------------------------- 2. imports

st.subheader("2. Imports, each in its own process")
st.caption("A segfault here kills only the subprocess, so we learn which one.")

ok_numpy = show(run("import numpy", """
    import numpy
    print("numpy", numpy.__version__)
"""))

ok_rr = show(run("import roadrunner", """
    import roadrunner
    print("roadrunner", roadrunner.__version__)
"""))

ok_anti = show(run("import antimony", """
    import antimony
    print("antimony loaded")
"""))

ok_both = show(run("import numpy then roadrunner together", """
    import numpy
    import roadrunner
    print("both imported; numpy", numpy.__version__)
"""))

if not (ok_rr and ok_both):
    st.warning(
        "RoadRunner cannot be imported on this host. The most likely cause is "
        "an ABI mismatch: a compiled extension built against numpy 1.x will "
        "segfault under numpy 2.x. The fix is to pin numpy in "
        "requirements.txt - try `numpy<2` - and redeploy."
    )
    st.stop()

# ------------------------------------------------- 3. behaviour and 4. speed

st.subheader("3. Behaviours this project depends on")
st.caption("Verified locally against 2.9.1. Any difference here means "
           "session.py needs revisiting before the agent is hosted.")

PROBE = """
    import json, time
    import numpy as np
    try:                      # Linux only; absent on Windows
        import resource
    except ImportError:
        resource = None
    import roadrunner, antimony

    def loada(text):
        antimony.clearPreviousLoads()
        if antimony.loadAntimonyString(text) < 0:
            raise ValueError(antimony.getLastError())
        return roadrunner.RoadRunner(
            antimony.getSBMLString(antimony.getMainModuleName()))

    def to_antimony(sbml):
        antimony.clearPreviousLoads()
        antimony.loadSBMLString(sbml)
        return antimony.getAntimonyString(antimony.getMainModuleName())

    GOODWIN = '''
    J1: -> S1; v0/(1 + (S3/K)^n);
    J2: S1 -> S2; k1*S1;
    J3: S2 -> S3; k2*S2;
    J4: S3 -> ; k3*S3;
    v0 = 8; K = 1; n = 8; k1 = 1; k2 = 1; k3 = 1;
    S1 = 0.1; S2 = 0.2; S3 = 0.3;
    '''

    out = {}
    rr = loada(GOODWIN)
    out["species"] = list(rr.model.getFloatingSpeciesIds())
    out["parameters"] = list(rr.model.getGlobalParameterIds())
    out["roadrunner"] = roadrunner.__version__

    # (a) getSBML gives the model; getCurrentSBML gives the state.
    rr.reset(); rr.k1 = 99.0; rr.simulate(0, 40, 200)
    plain = to_antimony(rr.getSBML())
    current = to_antimony(rr.getCurrentSBML())
    out["sbml_is_model"] = ("k1 = 1" in plain and "S1 = 0.1" in plain)
    out["current_sbml_is_state"] = ("k1 = 99" in current
                                    and "S1 = 0.1" not in current)

    # (b) reset keeps parameter changes; resetAll discards them.
    rr.reset(); rr.k1 = 99.0; rr.reset()
    out["reset_keeps"] = rr.k1 == 99.0
    rr.resetAll()
    out["resetall_discards"] = rr.k1 != 99.0

    # (c) nleq2 solves a conserved moiety unaided.
    try:
        cons = loada("J1: S1 -> S2; k1*S1; J2: S2 -> S1; k2*S2;"
                     " k1 = 0.6; k2 = 0.15; S1 = 10; S2 = 0;")
        cons.steadyState()
        out["moiety_S1"] = float(cons.S1)
    except Exception as exc:
        out["moiety_error"] = f"{type(exc).__name__}: {exc}"

    # speed
    rr.resetAll()
    times = []
    for _ in range(20):
        rr.reset()
        t0 = time.perf_counter(); rr.simulate(0, 100, 1000)
        times.append((time.perf_counter() - t0) * 1000)
    out["sim_ms_median"] = float(np.median(times))
    out["peak_rss_mb"] = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                          / 1024) if resource else -1.0
    print("JSON:" + json.dumps(out))
"""

result = run("model load, behaviours, timing", PROBE)
if not show(result):
    st.stop()

payload = {}
for line in result["stdout"].splitlines():
    if line.startswith("JSON:"):
        payload = json.loads(line[5:])

if payload:
    st.write(f"species `{payload['species']}` · parameters "
             f"`{payload['parameters']}` · roadrunner "
             f"`{payload['roadrunner']}`")

    checks = [
        ("getSBML() gives the model, getCurrentSBML() the state",
         payload.get("sbml_is_model") and payload.get("current_sbml_is_state")),
        ("reset() keeps parameter changes, resetAll() discards them",
         payload.get("reset_keeps") and payload.get("resetall_discards")),
        ("nleq2 solves a conserved moiety unaided (S1 -> 2.0)",
         abs(payload.get("moiety_S1", -1) - 2.0) < 1e-6),
    ]
    for label, passed in checks:
        (st.success if passed else st.error)(
            f"{'OK' if passed else 'CHANGED from 2.9.1'} - {label}")
    if "moiety_error" in payload:
        st.error(f"steady state raised: {payload['moiety_error']}")

    st.subheader("4. Speed and footprint")
    left, right = st.columns(2)
    left.metric("simulation, 1000 points",
                f"{payload['sim_ms_median']:.1f} ms",
                help="6.6 ms on the development machine.")
    right.metric("peak resident memory", f"{payload['peak_rss_mb']:.0f} MB",
                 help="Community Cloud allows roughly 1 GB per app, shared "
                      "by every viewer.")

st.divider()
st.caption("This probe runs no agent and needs no API key. Hosting the agent "
           "additionally requires sandboxing run_python, which executes "
           "arbitrary Python.")
