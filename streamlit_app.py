"""The RoadRunner agent, as a hosted Streamlit application.

    streamlit run streamlit_app.py

This is the deployed entry point. The deployment probe that proved the
simulation stack installs on Linux now lives at `pages/2_Deployment_probe.py`
and is reachable from the sidebar.

`app.py` (NiceGUI) remains the local application and is the better one to
develop against: it streams the agent's activity over a persistent page and
can interrupt a run. Streamlit re-runs this whole script on every widget
interaction, which costs two things and only two:

  * **No Stop button.** While `agent.ask` blocks there is no interaction to
    catch, so `stop_check` can never become true. The token, turn and wall
    clock budgets in the sidebar are the only brakes; keep them modest.
  * **A slower loop when dragging sliders.** Simulation is ~6 ms, so the
    model is never the limit - the script re-run is. Perfectly usable, just
    not the 60 fps of the NiceGUI build.

Everything else ports, including the live activity feed: during a run there
are no widget interactions to trigger a re-run, so `on_event` can paint into
a container as the events arrive.

Hosted-deployment notes, which are not optional:

  * **Bring your own key.** The deployment holds no credential. The key is
    read from a password box into `st.session_state` and passed to
    `agent.ask` as a constructed client - never written to `os.environ`,
    which is process-global and therefore shared with every other viewer.
  * **One container serves every viewer** on Streamlit Community Cloud.
    Agent code cannot reach credentials (see remote.py), but it can still
    read the container's filesystem and open sockets. Put this behind
    `APP_PASSWORD` in secrets and share it only with people you would give a
    shell to.
  * **Workers are reaped on idle.** `app.py` kills a tab's worker subprocess
    from `client.on_disconnect`; Streamlit has no reliable equivalent, so a
    closed tab would leak a process until the box ran out of memory. Every
    session records when it was last touched and stale ones are closed here.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import numpy as np
import plotly.graph_objects as go
import streamlit as st

import agent as agent_config
import cases
import providers
from remote import WorkerSession
from session import Session, apply_recommendation, coerce

#: Agent code runs in a subprocess with no credentials in its environment.
#: Never set this to 0 on a deployment other people can reach: `run_python`
#: would then read every key this process can.
SANDBOXED = os.environ.get("RRAGENT_SANDBOX", "1") != "0"

#: A session untouched for this long is assumed abandoned and its worker
#: closed. Streamlit gives no disconnect event, so this is the only thing
#: standing between a closed tab and a leaked subprocess.
IDLE_TIMEOUT_SECONDS = 30 * 60

PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed",
           "#0891b2", "#db2777", "#65a30d"]

DEFAULT_CASE = "goodwin_damped"

#: Every model the agent can drive. `providers.make_provider` accepts a
#: ready-made `client`, and the OpenAI-compatible provider uses it verbatim
#: when given, so a key pasted into the sidebar reaches DeepSeek without ever
#: going near `os.environ` - which is process-global and therefore shared
#: with every other viewer of this container.
MODEL_CHOICES = list(agent_config.MODEL_CHOICES)

#: The sidebar label and the session-state slot for each provider's key.
PROVIDER_NAMES = {"anthropic": "Anthropic", "deepseek": "DeepSeek"}


def key_slot(model: str) -> str:
    return f"api_key_{providers.provider_for(model)}"


def build_client(model: str):
    """A client carrying the viewer's own key, for whichever vendor it is.

    Mirrors `agent.make_client`, including the part that is easy to miss: an
    identity-linked Anthropic key is refused without an `anthropic-workspace-id`
    header, with a 400 that reads like an auth failure and is not.
    """
    provider = providers.provider_for(model)
    key = st.session_state.get(key_slot(model), "")
    if provider == "anthropic":
        import anthropic
        workspace = (st.session_state.get("workspace_id") or "").strip()
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        return anthropic.Anthropic(api_key=key, default_headers=headers)
    from openai import OpenAI
    base_url = providers.REGISTRY[provider]["kwargs"].get("base_url", "")
    return OpenAI(api_key=key, base_url=base_url)

st.set_page_config(page_title="RoadRunner Agent", page_icon="🧬",
                   layout="wide")


# --------------------------------------------------------------- lifecycle

@st.cache_resource
def _session_registry() -> dict:
    """Live sessions by id: `{key: (session, last_touched)}`.

    It must be `cache_resource` and not a module global. `streamlit run`
    executes this file top to bottom on *every* rerun, so a module-level dict
    is re-created each time - the app then builds a fresh, unloaded Session
    on every interaction and nothing ever stays loaded. `cache_resource` is
    the one store that outlives a rerun, and it is shared across viewers,
    which reaping needs anyway: it has to see other viewers' sessions, not
    just this one.
    """
    return {}


def _new_session():
    return WorkerSession() if SANDBOXED else Session()


def reap_idle_sessions(now: float) -> None:
    """Close workers whose tab has evidently gone away."""
    sessions = _session_registry()
    for key, (session, touched) in list(sessions.items()):
        if now - touched < IDLE_TIMEOUT_SECONDS:
            continue
        closer = getattr(session, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        sessions.pop(key, None)


def get_session():
    """This viewer's Session, created on first use and kept alive."""
    now = time.time()
    reap_idle_sessions(now)
    sessions = _session_registry()
    key = st.session_state.get("session_key")
    if key is None or key not in sessions:
        key = uuid.uuid4().hex
        st.session_state.session_key = key
        sessions[key] = (_new_session(), now)
    else:
        sessions[key] = (sessions[key][0], now)
    return sessions[key][0]


def queue(name: str, value: Any) -> None:
    """Set a widget-backed value on the *next* run.

    Streamlit raises if you write to a key whose widget has already been
    instantiated this run, and the report's buttons are rendered below the
    model text box they need to rewrite. So park the value and apply it at
    the top of the next run, before any widget is built.
    """
    st.session_state.setdefault("_pending", {})[name] = value


def apply_pending() -> None:
    for name, value in st.session_state.pop("_pending", {}).items():
        st.session_state[name] = value


def clear_slider_keys() -> None:
    """Forget slider positions belonging to a model that is no longer loaded."""
    for key in [k for k in st.session_state if k.startswith("slider_")]:
        del st.session_state[key]
    st.session_state.pop("slider_tops", None)


def init_state() -> None:
    defaults = {
        "source": cases.load(DEFAULT_CASE).MODEL,
        "question": cases.load(DEFAULT_CASE).QUESTION.strip(),
        "handoff": None,
        "report_stale": False,
        "outcome": "",
        "sim_error": None,
        "start": 0.0,
        "end": 100.0,
        "points": 1000,
        "selected": [],
        "api_key_anthropic": "",
        "api_key_deepseek": "",
        # An identity-linked key needs the workspace it acts in. Seeded from
        # the environment so a local run needs no typing; on a deployment the
        # viewer supplies their own.
        "workspace_id": os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip(),
        "loaded_once": False,
        "y_lock": False,
        "y_range": None,
        "feed": [],
    }
    for name, value in defaults.items():
        st.session_state.setdefault(name, value)


# ------------------------------------------------------------------- gate

def password_gate() -> bool:
    """A shared password, when one is configured.

    Absent `APP_PASSWORD` in secrets the app is open - which is a reasonable
    default locally and a bad one hosted, so say so rather than silently
    allowing it.
    """
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:      # no secrets.toml at all
        expected = ""
    if not expected:
        return True
    if st.session_state.get("authorised"):
        return True

    st.title("RoadRunner Agent")
    st.caption("This deployment is password protected.")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == expected:
            st.session_state.authorised = True
            st.rerun()
        else:
            st.error("Not that one.")
    return False


# -------------------------------------------------------------- simulate

def do_load(session, announce: bool = True) -> bool:
    try:
        session.load(st.session_state.source)
    except Exception as exc:
        st.session_state.sim_error = f"Antimony error: {exc}"
        return False
    clear_slider_keys()
    pin_slider_ranges(session)
    st.session_state.y_range = None
    st.session_state.selected = list(session.rr.getFloatingSpeciesIds())
    st.session_state.handoff = st.session_state.handoff  # kept, but disarmed
    if st.session_state.handoff is not None:
        st.session_state.report_stale = True
    st.session_state.loaded_once = True
    st.session_state.sim_error = None
    do_simulate(session)
    if announce:
        st.toast("Model loaded")
    return True


def do_simulate(session) -> None:
    if not session.loaded:
        return
    selections = ["time"] + list(st.session_state.selected or [])
    try:
        session.simulate(float(st.session_state.start),
                         float(st.session_state.end),
                         int(st.session_state.points), selections)
        st.session_state.sim_error = None
    except Exception as exc:
        st.session_state.sim_error = f"{type(exc).__name__}: {exc}"
        session.last_sim = None


def y_axis_range(session):
    """The locked y range, or None to let Plotly autoscale.

    Autoscaling on every drag makes the plot flicker and, worse, hides the
    thing you are dragging *for*: an oscillation that grows keeps the same
    apparent height while the axis quietly grows with it. Locking captures
    the range at the moment the box is ticked and holds it.
    """
    if not st.session_state.get("y_lock"):
        st.session_state.y_range = None
        return None
    if st.session_state.get("y_range") is None:
        result = session.last_result if session.loaded else None
        if result is not None:
            data = np.asarray(result)[:, 1:]
            if data.size:
                low, high = float(data.min()), float(data.max())
                pad = 0.05 * (high - low) if high > low else 1.0
                st.session_state.y_range = [low - pad, high + pad]
    return st.session_state.get("y_range")


def figure_for(session) -> go.Figure:
    locked = y_axis_range(session)
    figure = go.Figure()
    result = session.last_result if session.loaded else None
    if result is not None and st.session_state.sim_error is None:
        data = np.asarray(result)
        names = list(result.colnames)
        for index, name in enumerate(names[1:], start=1):
            figure.add_trace(go.Scatter(
                x=data[:, 0], y=data[:, index], name=name, mode="lines",
                line=dict(width=2.2,
                          color=PALETTE[(index - 1) % len(PALETTE)])))
    figure.update_layout(
        margin=dict(l=55, r=20, t=28, b=45), height=420,
        paper_bgcolor="white", plot_bgcolor="#fbfcfe",
        xaxis=dict(title="time", gridcolor="#eef1f6", zeroline=False),
        yaxis=dict(title="concentration", gridcolor="#eef1f6", zeroline=False,
                   range=locked, autorange=locked is None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        # Keep Plotly's view state across reruns instead of re-initialising
        # the chart every time: without it each redraw is a fresh plot, which
        # reads as a flicker and throws away any zoom or pan.
        uirevision="roadrunner",
        font=dict(family="Inter, Segoe UI, sans-serif", size=12))
    if st.session_state.sim_error:
        figure.add_annotation(
            text=f"simulation failed<br><span style='font-size:11px'>"
                 f"{st.session_state.sim_error[:120]}</span>",
            showarrow=False, font=dict(color="#dc2626", size=14))
    return figure


def apply_change(session, change) -> str:
    """Apply a recommendation wherever the model actually lives.

    A WorkerSession does it inside the worker: `apply_recommendation` reaches
    solver objects that cannot cross the pipe.
    """
    if isinstance(session, WorkerSession):
        return session.apply_recommendation(change)
    return apply_recommendation(session, change)


def label_for(ident: str) -> str:
    return ident.replace("init([", "").replace("])", "₀")


# ------------------------------------------------------------------- panels

def model_panel(session) -> None:
    st.markdown("##### Model text")
    example = st.selectbox("example", cases.available(),
                           index=cases.available().index(DEFAULT_CASE),
                           label_visibility="collapsed")
    if st.button("Load this example", width="stretch"):
        module = cases.load(example)
        st.session_state.source = module.MODEL
        st.session_state.question = module.QUESTION.strip()
        do_load(session)
        st.rerun()

    st.text_area("Antimony", key="source", height=280,
                 label_visibility="collapsed")
    if st.button("Load", type="primary", width="stretch"):
        do_load(session)
        st.rerun()


def pin_slider_ranges(session) -> None:
    """Fix each slider's span once, when the model is loaded.

    A range derived from the *current* value re-scales on every rerun, so the
    scale shifts under the handle mid-drag: the value walks away from where
    it was put and the axis collapses towards zero. app.py avoids this by
    only letting a range grow while the handle moves and shrink once it is
    released; the same end is reached here, more simply, by deciding the span
    at load and leaving it alone.
    """
    if not session.loaded:
        return
    rr = session.rr
    tops: dict[str, float] = {}
    for identifiers in tunable_ids(session):
        for ident in identifiers:
            value = float(rr[ident])
            # Bracket the loaded value. Zero has no scale of its own, so give
            # it a unit range.
            tops[ident] = 2.0 * value if value > 0 else 1.0
    st.session_state.slider_tops = tops


def slider_top(session, ident: str, current: float) -> float:
    """The pinned span, widened if something has since exceeded it.

    A recommendation the agent applies can land outside the range the model
    was loaded with; the slider has to be able to show it.
    """
    tops = st.session_state.setdefault("slider_tops", {})
    top = tops.get(ident)
    if top is None:
        top = 2.0 * current if current > 0 else 1.0
    if current > top:
        top = 2.0 * current
    tops[ident] = top
    return max(top, 1e-12)


def tunable_ids(session) -> tuple[list[str], list[str]]:
    rr = session.rr
    return (list(rr.getGlobalParameterIds()),
            list(rr.getFloatingSpeciesInitialConcentrationIds()))


def sync_sliders(session) -> None:
    """Push slider positions onto the live model, before anything is drawn.

    This has to run ahead of the plot. The sliders sit to the *right* of the
    graph, so Streamlit renders them second - and a slider that applied its
    own value would do so after the figure had already been built, leaving
    the plot one drag behind the number above it. Reading the widget state
    up front instead keeps them in step.
    """
    if not session.loaded:
        return
    rr = session.rr
    changed = False
    for identifiers in tunable_ids(session):
        for ident in identifiers:
            value = st.session_state.get(f"slider_{ident}")
            if value is None:
                continue
            try:
                if abs(float(value) - float(rr[ident])) > 1e-12:
                    rr[ident] = float(value)
                    changed = True
            except Exception as exc:
                st.warning(f"{ident}: {exc}")
    if changed:
        do_simulate(session)


def values_panel(session) -> None:
    """A slider per parameter and initial value - `sync_sliders` applies them."""
    if not session.loaded:
        st.caption("Load a model to see its parameters.")
        return
    rr = session.rr
    for title, identifiers in zip(("parameters", "initial values"),
                                  tunable_ids(session)):
        if not identifiers:
            continue
        st.caption(title.upper())
        for ident in identifiers:
            current = float(rr[ident])
            top = slider_top(session, ident, current)
            st.slider(label_for(ident), min_value=0.0, max_value=float(top),
                      value=float(current), step=float(top) / 200.0,
                      key=f"slider_{ident}")


def simulation_panel(session) -> None:
    st.markdown("##### Simulation")
    columns = st.columns(3)
    columns[0].number_input("start", key="start", format="%g")
    columns[1].number_input("end", key="end", format="%g")
    columns[2].number_input("points", key="points", step=100)
    if session.loaded:
        st.multiselect("species", list(session.rr.getFloatingSpeciesIds()),
                       key="selected")
    if st.button("Simulate", width="stretch"):
        do_simulate(session)
        st.rerun()


@st.fragment
def explore_fragment(session) -> None:
    """The plot and its sliders, as one independently re-running block.

    Without the fragment, releasing a slider re-executes the whole script -
    sidebar, model text, report and all - and repainting the entire page is
    what reads as a flicker. A fragment re-runs only itself, so a drag
    touches the chart and the sliders and nothing else.

    `sync_sliders` has to run inside it, and before the chart is drawn: the
    sliders are rendered to the right of the graph, so applying their values
    where they are declared would leave the plot one drag behind.
    """
    sync_sliders(session)

    # Plot and sliders side by side, as in app.py. A slider you have to
    # scroll away from the graph to reach cannot be used to explore the
    # graph, which is the whole point of having it.
    chart_column, explore_column = st.columns([3, 2], gap="medium")
    with chart_column:
        # A stable key makes this one reused element rather than a new one
        # per rerun; `uirevision` in the layout keeps Plotly from
        # re-initialising the figure it already has.
        st.plotly_chart(figure_for(session), width="stretch", key="main_plot")
    with explore_column:
        with st.container(border=True, height=420):
            st.markdown("##### Explore")
            st.caption("drag to vary the live model - the plot updates when "
                       "you let go")
            # Say what this cannot do, rather than let a slider that only
            # reports on release read as a broken one. Streamlit has no
            # continuous drag event: the value arrives on mouse-up, and the
            # simulation is a server round trip away.
            st.info(
                "True interactive simulation is unavailable due to Streamlit "
                "limitations. For interactive simulation use "
                "[WebIridium](https://github.com/sys-bio/WebIridium) for the "
                "web, or [IridiumSimulator](https://github.com/sys-bio/"
                "IridiumSimulator) for the desktop.",
                icon=":material/info:")
            st.checkbox("lock y-axis", key="y_lock",
                        help="Hold the axis still while you drag, so a change "
                             "of shape is visible instead of being scaled "
                             "away.")
            values_panel(session)


# ------------------------------------------------------------------ report

def metrics_line(handoff) -> str:
    return (f"{handoff.model.replace('claude-', '')} "
            f"{handoff.effort}/{handoff.detail} · {handoff.turns} turns · "
            f"{handoff.seconds:.0f}s · {handoff.total_input_tokens:,} in / "
            f"{handoff.output_tokens:,} out · ≈ {handoff.cost_text()}")


def transcript_text(handoff) -> str:
    parts = []
    if handoff.report is not None:
        parts.append(handoff.report.as_markdown())
    parts.append("\n**Run**\n\n    " + metrics_line(handoff))
    parts.append("\n---\n")
    for entry in handoff.transcript:
        parts.append(f"```python\n{entry['code']}\n```\n\n{entry['output']}\n")
    return "\n".join(parts)


def show_report(session, handoff) -> None:
    if handoff is None:
        return
    report = handoff.report

    header = st.columns([3, 2])
    with header[0]:
        if report is not None:
            badge = {"numerical": "blue", "structural": "red",
                     "parametric": "violet", "expected": "green"}
            colour = badge.get(report.classification, "grey")
            st.markdown(f":{colour}-background[**{report.classification}**]"
                        f" &nbsp; session {report.session_state}")
    header[1].caption(metrics_line(handoff))

    if st.session_state.report_stale:
        st.info("A model has been loaded since this report was produced, so "
                "its buttons are disabled. Ask again for a current answer.")

    if report is None:
        st.error(f"No report: {handoff.stopped_because}")
        return

    st.markdown(report.finding)
    with st.expander("Evidence"):
        st.markdown(report.evidence)

    disabled = st.session_state.report_stale

    if report.recommended_changes:
        with st.container(border=True):
            st.markdown("**Recommended fix (not yet applied)**")
            for change in report.recommended_changes:
                kind = change.get("kind", "value")
                if kind == "model_text":
                    headline = "rewrite the model"
                elif kind == "simulation":
                    headline = f"simulate to t = {change.get('value')}"
                else:
                    headline = f"set {change['selector']} = {change.get('value')}"
                st.markdown(f"- **{headline}** — {change['why']}")

    st.caption("CHANGES")
    change_log = session.change_log
    if change_log:
        for what, before, after in change_log:
            st.markdown(f"- `{what}`  {before} → {after}")
    else:
        st.markdown("No net change to the session.")

    check = handoff.cross_check(change_log)
    if check["in_session_not_reported"] or check["in_report_not_session"]:
        with st.container(border=True):
            st.markdown(":red[**Cross-check discrepancies**]")
            for item in check["in_session_not_reported"]:
                st.caption(f"changed but not reported: {item}")
            for item in check["in_report_not_session"]:
                st.caption(f"reported but not applied: {item}")

    buttons = st.columns(4)

    if report.recommended_changes and buttons[0].button(
            "Try it", disabled=disabled,
            help="Set the recommended values on the live model. Your model "
                 "text is not changed."):
        applied, failed = [], []
        for change in report.recommended_changes:
            try:
                applied.append(apply_change(session, change))
                if change.get("kind") == "model_text":
                    queue("source", change["model_text"].strip())
                elif change.get("kind") == "simulation":
                    queue("end", float(coerce(change.get("value"))))
            except Exception as exc:
                failed.append(f"{change.get('selector') or change.get('kind')} ({exc})")
        # The sliders still hold the positions from before the fix. They are
        # read back onto the model by `sync_sliders`, which runs before this
        # panel is drawn, so leaving them would push the old values straight
        # back and quietly undo the recommendation. Forget them and let them
        # re-read what the agent just applied.
        clear_slider_keys()
        do_simulate(session)
        st.session_state.outcome = (
            "Live model now has " + ", ".join(applied) +
            ". Check the plot, then 'Write into model text' to keep it."
            if applied else "")
        if failed:
            st.session_state.outcome += "  Could not apply: " + ", ".join(failed)
        st.rerun()

    if buttons[1].button("Discard", disabled=disabled,
                         help="Put the live model back as it was before the "
                              "agent saw it."):
        try:
            session.revert()
            queue("source", session.source_antimony)
            clear_slider_keys()
            do_simulate(session)
            st.session_state.outcome = "Reverted to the state at handoff."
        except Exception as exc:
            st.session_state.outcome = f"Could not revert: {exc}"
        st.rerun()

    if buttons[2].button("Write into model text", disabled=disabled,
                         help="Write the live values into your Antimony "
                              "source, keeping comments and layout."):
        was_edit = session.model_edited
        if session.handoff_snapshot is None:
            # No handoff to measure against - the values on the model are
            # simply what the user arrived at. `write_values_to_source` is
            # the same substitution referenced against the source instead,
            # and works with no agent involved (session.py:416).
            _text, applied, missing = session.write_values_to_source()
        else:
            applied, missing = session.accept()
        queue("source", session.source_antimony)
        clear_slider_keys()
        if was_edit:
            st.session_state.outcome = ("Accepted - model text replaced. Your "
                                        "comments and layout were lost in the "
                                        "round trip.")
        elif applied:
            st.session_state.outcome = ("Accepted - written into your source: "
                                        + ", ".join(applied))
        else:
            st.session_state.outcome = ("Nothing to write back - the live model "
                                        "already matches your source. Use "
                                        "'Try it' first.")
        if missing:
            st.session_state.outcome += ("  Not found in your source: "
                                         + ", ".join(missing))
        st.rerun()

    buttons[3].download_button("Download transcript", transcript_text(handoff),
                               file_name="roadrunner-agent-report.md")

    if st.session_state.outcome:
        st.success(st.session_state.outcome)


# ------------------------------------------------------------------- agent

def render_event(container, kind: str, payload: Any) -> None:
    with container:
        if kind == "turn":
            st.caption(f"TURN {payload}")
        elif kind == "text":
            st.markdown(f"> {payload}")
        elif kind == "code":
            st.code(payload, language="python")
        elif kind == "output":
            text = payload if len(payload) < 2000 else payload[:2000] + " ..."
            st.code(text, language="text")
        elif kind == "limit":
            st.warning(str(payload))
        elif kind == "refusal":
            st.error(f"refused: {payload}")


def run_agent(session, settings: dict) -> None:
    """Ask the question, painting the agent's activity as it arrives.

    `agent.ask` blocks this script run to completion. That is exactly why the
    feed works: no widget can be touched meanwhile, so nothing re-runs the
    script out from under the container being painted.
    """
    question = st.session_state.question.strip()
    if not question:
        st.warning("Ask a question first.")
        return
    if not st.session_state.get(key_slot(settings["model"])):
        vendor = PROVIDER_NAMES.get(providers.provider_for(settings["model"]),
                                    "the provider's")
        st.warning(f"Paste a {vendor} API key in the sidebar first.")
        return

    # Edited the model text but did not press Load? Load it now. Asking about
    # a model the session is not running gives a confident answer about the
    # wrong model, which is worse than any error message.
    stale = (not session.loaded
             or st.session_state.source.strip() != session.source_antimony.strip())
    if stale:
        was_loaded = session.loaded
        if not do_load(session, announce=False):
            st.error(st.session_state.sim_error)
            return
        if was_loaded:
            st.info("The model text had changed, so it was loaded before "
                    "asking - the agent sees what is on screen.")

    import agent

    # The key is passed as a constructed client, never through os.environ:
    # the environment is process-global and this container is shared with
    # every other viewer.
    client = build_client(settings["model"])

    continuing = bool(settings["follow_up"] and st.session_state.handoff
                      and st.session_state.handoff.messages)

    st.session_state.feed = []
    feed = st.container(border=True, height=420)
    if continuing:
        with feed:
            st.caption("FOLLOW-UP")

    def on_event(kind, payload):
        st.session_state.feed.append((kind, payload))
        render_event(feed, kind, payload)

    handoff = None
    with st.spinner("The agent is working. This takes minutes, and the "
                    "budgets in the sidebar are the only brakes."):
        try:
            handoff = agent.ask(
                session, question, client=client,
                model=settings["model"], effort=settings["effort"],
                detail=settings["detail"], max_turns=settings["max_turns"],
                max_seconds=settings["max_seconds"],
                max_total_tokens=settings["max_tokens"],
                previous=st.session_state.handoff if continuing else None,
                on_event=on_event)
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")

    if handoff is not None:
        st.session_state.handoff = handoff
        st.session_state.report_stale = False
        st.session_state.outcome = ""
        do_simulate(session)


# -------------------------------------------------------------------- main

def sidebar() -> dict:
    with st.sidebar:
        st.markdown("### The agent")
        model = st.selectbox("model", MODEL_CHOICES,
                             index=MODEL_CHOICES.index("claude-sonnet-5")
                             if "claude-sonnet-5" in MODEL_CHOICES else 0)
        effort = st.selectbox("effort", agent_config.EFFORT_CHOICES, index=0)
        detail = st.selectbox("detail", agent_config.DETAIL_CHOICES, index=0)

        with st.expander("Budgets"):
            st.caption("There is no Stop button on this build - these are the "
                       "only brakes on a run.")
            max_turns = st.number_input("max turns", 1, 50,
                                        min(10, agent_config.MAX_TURNS))
            max_seconds = st.number_input("max seconds", 30, 1800,
                                          min(300, int(agent_config.MAX_SECONDS)))
            max_tokens = st.number_input("max total tokens", 10_000, 1_000_000,
                                         agent_config.MAX_TOTAL_TOKENS,
                                         step=10_000)

        st.divider()
        # The key box follows the chosen model: each vendor brings its own,
        # and both are remembered separately so switching model does not
        # discard the other.
        provider = providers.provider_for(model)
        vendor = PROVIDER_NAMES.get(provider, provider)
        st.markdown("### Your API key")
        st.text_input(f"{vendor} API key", type="password",
                      key=key_slot(model),
                      help="Kept in this browser session only. It is passed "
                           "straight to the client and is never written to "
                           "the server's environment or to disk.")
        if provider == "anthropic":
            st.text_input(
                "Anthropic workspace ID", key="workspace_id",
                help="Required for an identity-linked key: without it the "
                     "API returns 400, which reads like an auth failure but "
                     "is not. Leave blank for a workspace-scoped key.")
        if not st.session_state.get(key_slot(model)):
            where = ("console.anthropic.com" if provider == "anthropic"
                     else "platform.deepseek.com")
            st.caption(f"Get one at {where}. A question costs roughly $0.05 "
                       "at sonnet-5 / low; DeepSeek is cheaper per token but "
                       "measured dearer per answer, at 12 turns against 3.")

        follow_up = st.checkbox(
            "follow up on the last answer",
            disabled=st.session_state.handoff is None,
            help="Keeps the agent's whole investigation, so a follow-up costs "
                 "a fraction of a fresh question.")

        st.divider()
        st.caption("Agent code runs in a subprocess with no credentials in "
                   "its environment. It can still read this container's "
                   "filesystem and reach the network, and one container "
                   "serves every viewer - so treat this as a demonstration, "
                   "not a private workspace."
                   if SANDBOXED else
                   ":red[**Unsandboxed.** Agent code can read every key this "
                   "process can. Do not use this on a shared deployment.]")

    return {"model": model, "effort": effort, "detail": detail,
            "max_turns": int(max_turns), "max_seconds": float(max_seconds),
            "max_tokens": int(max_tokens), "follow_up": follow_up}


def main() -> None:
    init_state()
    apply_pending()
    if not password_gate():
        return
    session = get_session()
    settings = sidebar()

    st.title("RoadRunner Agent")
    st.caption("Load an Antimony model, simulate it, explore it - then hand "
               "what you cannot explain to an agent with full libRoadRunner "
               "access.")

    if not session.loaded:
        do_load(session, announce=False)

    left, right = st.columns([1, 3], gap="large")

    with left:
        model_panel(session)
        with st.container(border=True):
            simulation_panel(session)

    with right:
        if st.session_state.sim_error:
            st.error(st.session_state.sim_error)
        explore_fragment(session)

        with st.container(border=True):
            st.markdown("##### Ask the agent")
            st.text_area("question", key="question", height=110,
                         label_visibility="collapsed")
            if st.button("Answer my question", type="primary"):
                run_agent(session, settings)

        if st.session_state.handoff is not None:
            with st.container(border=True):
                show_report(session, st.session_state.handoff)


main()
