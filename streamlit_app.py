"""Deployment probe for Streamlit Community Cloud.

Answers, on the host itself, the questions that decide whether the agent can
be hosted there at all:

  1. Does the stack install, and how much memory does it take?
  2. Does the model load *without* tellurium? (tellurium adds ~114 MB of
     wheels for one function call.)
  3. Do the three RoadRunner behaviours this project depends on still hold on
     the version the host installs? They were verified against 2.9.1 locally;
     Streamlit Cloud installs 2.10.0.
  4. Is a simulation fast enough to be interactive?

Deploy this first, read the page, and only then decide whether to port the
agent. It is a probe, not the app: it runs no agent and needs no API key.
"""

from __future__ import annotations

import platform
import sys
import time

import numpy as np
import streamlit as st

st.set_page_config(page_title="RoadRunner stack probe", page_icon="🧪",
                   layout="centered")
st.title("RoadRunner stack probe")
st.caption("Does the simulation stack install and behave on this host?")

GOODWIN = """\
// Goodwin oscillator: S3 represses its own production
J1: -> S1;    v0/(1 + (S3/K)^n);
J2: S1 -> S2; k1*S1;
J3: S2 -> S3; k2*S2;
J4: S3 -> ;   k3*S3;

v0 = 8; K = 1; n = 8;
k1 = 1; k2 = 1; k3 = 1;
S1 = 0.1; S2 = 0.2; S3 = 0.3;
"""


def load_antimony(text: str):
    """te.loada without tellurium.

    tellurium's only role in this project is this one call, and it costs
    ~114 MB of wheels (rrplugins, scipy, pandas, phrasedml, libsbml).
    """
    import antimony
    import roadrunner

    antimony.clearPreviousLoads()
    if antimony.loadAntimonyString(text) < 0:
        raise ValueError(antimony.getLastError())
    sbml = antimony.getSBMLString(antimony.getMainModuleName())
    return roadrunner.RoadRunner(sbml)


def to_antimony(sbml: str) -> str:
    """SBML -> Antimony, the job tellurium's getCurrentAntimony() does."""
    import antimony

    antimony.clearPreviousLoads()
    if antimony.loadSBMLString(sbml) < 0:
        raise ValueError(antimony.getLastError())
    return antimony.getAntimonyString(antimony.getMainModuleName())


def species_ids(rr):
    """Without tellurium the id accessors live on rr.model, not rr."""
    return list(rr.model.getFloatingSpeciesIds())


def parameter_ids(rr):
    return list(rr.model.getGlobalParameterIds())


def rss_mb() -> float:
    """Resident memory, or nan where the platform cannot say."""
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        return peak / 1024 if sys.platform.startswith("linux") else peak / 2**20
    except Exception:
        return float("nan")


# ---------------------------------------------------------------- environment

st.subheader("1. Environment")
rows = {"python": platform.python_version(),
        "platform": platform.platform(),
        "libc": " ".join(platform.libc_ver()) or "n/a"}
try:
    import importlib.metadata as md
    for package in ("libroadrunner", "antimony", "numpy", "streamlit",
                    "plotly", "anthropic", "openai", "tellurium"):
        try:
            rows[package] = md.version(package)
        except md.PackageNotFoundError:
            rows[package] = "not installed"
except Exception as exc:            # pragma: no cover - defensive
    rows["metadata error"] = str(exc)
st.table({"": list(rows), "value": list(rows.values())})

if rows.get("tellurium", "").startswith("not"):
    st.success("tellurium is absent, as intended - ~114 MB of wheels saved.")
else:
    st.warning("tellurium is installed; requirements.txt does not ask for it.")

# ------------------------------------------------------------------- loading

st.subheader("2. Loading a model without tellurium")
try:
    started = time.perf_counter()
    rr = load_antimony(GOODWIN)
    load_ms = (time.perf_counter() - started) * 1000
    st.success(f"loaded in {load_ms:.0f} ms - "
               f"species {species_ids(rr)}, "
               f"parameters {parameter_ids(rr)}")
except Exception as exc:
    st.error(f"FAILED: {type(exc).__name__}: {exc}")
    st.stop()

# -------------------------------------------------------- documented hazards

st.subheader("3. Behaviours this project depends on")
st.caption("Verified locally against 2.9.1. If any of these differ on the "
           "version installed here, session.py needs revisiting before the "
           "agent is hosted.")

checks = []

# (a) The state trap belongs to getCurrentSBML, not to getSBML. Without
# tellurium the choice is explicit, and getSBML() gives the model rather than
# the state - so the reset/restore dance in model_antimony() is unnecessary
# on this path.
rr.reset()
rr.k1 = 99.0
rr.simulate(0, 40, 200)
plain = to_antimony(rr.getSBML())
current = to_antimony(rr.getCurrentSBML())
plain_is_model = "k1 = 1" in plain and "S1 = 0.1" in plain
current_is_state = "k1 = 99" in current and "S1 = 0.1" not in current
checks.append((
    "getSBML() gives the model; getCurrentSBML() gives the state",
    plain_is_model and current_is_state,
    "as on 2.9.1 - so model_antimony() can use getSBML() and skip the "
    "reset/restore dance"
    if plain_is_model and current_is_state
    else f"DIFFERENT - getSBML is model: {plain_is_model}, "
         f"getCurrentSBML is state: {current_is_state}"))

# (b) reset() keeps parameter changes; resetAll() discards them.
rr.reset()
rr.k1 = 99.0
rr.reset()
kept = rr.k1 == 99.0
rr.resetAll()
discarded = rr.k1 != 99.0
checks.append((
    "reset() keeps parameter changes, resetAll() discards them",
    kept and discarded,
    "as documented" if kept and discarded
    else f"DIFFERENT - reset kept={kept}, resetAll discarded={discarded}"))

# (c) nleq2 solves a conserved-moiety model without help. If this ever
# changes, the conserved_moiety evaluation case changes meaning.
try:
    cons = load_antimony("J1: S1 -> S2; k1*S1; J2: S2 -> S1; k2*S2;"
                         " k1 = 0.6; k2 = 0.15; S1 = 10; S2 = 0;")
    cons.steadyState()
    moiety_ok = abs(cons.S1 - 2.0) < 1e-6
    detail = f"S1 -> {cons.S1:.6f} (expected 2.0)"
except Exception as exc:
    moiety_ok, detail = False, f"raised {type(exc).__name__}: {exc}"
checks.append(("nleq2 solves a conserved moiety unaided", moiety_ok, detail))

for label, passed, detail in checks:
    (st.success if passed else st.error)(
        f"{'OK' if passed else 'CHANGED'} - {label}\n\n{detail}")

# ---------------------------------------------------------------- speed, size

st.subheader("4. Speed and footprint")
rr.reset()
rr.k1 = 1.0
rr.resetAll()
timings = []
for _ in range(20):
    rr.reset()
    started = time.perf_counter()
    rr.simulate(0, 100, 1000)
    timings.append((time.perf_counter() - started) * 1000)
median = float(np.median(timings))
st.metric("simulation, 1000 points", f"{median:.1f} ms",
          help="6.6 ms on the development machine. Interactive sliders need "
               "this well under ~50 ms.")
st.metric("peak resident memory", f"{rss_mb():.0f} MB",
          help="Streamlit Community Cloud allows about 1 GB per app, shared "
               "by every viewer.")

# --------------------------------------------------------------------- plot

st.subheader("5. Plotting")
try:
    import plotly.graph_objects as go
    rr.reset()
    data = np.asarray(rr.simulate(0, 100, 1000))
    figure = go.Figure()
    for index, name in enumerate(species_ids(rr), start=1):
        figure.add_trace(go.Scatter(x=data[:, 0], y=data[:, index], name=name))
    figure.update_layout(height=320, margin=dict(l=40, r=20, t=20, b=40),
                         xaxis_title="time", yaxis_title="concentration")
    st.plotly_chart(figure, use_container_width=True)
    st.success("Plotly renders.")
except Exception as exc:
    st.error(f"plotting FAILED: {type(exc).__name__}: {exc}")

st.divider()
st.caption("This probe runs no agent and needs no API key. Hosting the agent "
           "itself additionally requires sandboxing run_python - it executes "
           "arbitrary Python, and on a shared host that reaches every "
           "viewer's data and any key in the environment.")
