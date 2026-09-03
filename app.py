"""GUI for the libRoadRunner agent proof of concept.

    .venv\\Scripts\\python.exe app.py

Opens on http://localhost:8080.

The agent loop is blocking and takes minutes, so it runs in a worker thread
(`run.io_bound`) while the model and parameter controls are disabled.  Agent
events are pushed onto a deque from that thread and drained by a ui.timer on
the event loop - UI objects are never touched from the worker (spec 3.2).
"""

from __future__ import annotations

import collections
import os
from typing import Any

import numpy as np
import plotly.graph_objects as go
from nicegui import run, ui

import agent as agent_config
import cases
from session import Session, apply_recommendation, coerce

PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed",
           "#0891b2", "#db2777", "#65a30d"]

DEFAULT_MODEL = cases.load("goodwin_damped").MODEL
DEFAULT_QUESTION = cases.load("goodwin_damped").QUESTION.strip()


class AppState:
    """Everything one browser session owns."""

    def __init__(self) -> None:
        self.session = Session()
        self.events: collections.deque = collections.deque()
        self.busy = False
        self.stop_requested = False
        self.handoff = None
        self.report_actions: list = []   # buttons that act on the live session
        self.value_fields: dict = {}     # ident -> ui.number in the Values panel
        self.sliders: dict = {}          # ident -> ui.slider
        self.pending: dict = {}          # slider moves not yet simulated
        self.y_range = None              # locked y-axis, or None to autoscale
        self.report_note = None          # "this report predates the model" line
        self.last_question = ""          # what was asked, to clear it safely
        self.error: str | None = None
        self.start, self.end, self.points = 0.0, 100.0, 1000
        self.selected: list[str] = []


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


@ui.page("/")
def main_page() -> None:
    state = AppState()
    ui.dark_mode(False)

    ui.add_head_html("""
    <style>
      body { font-family: Inter, "Segoe UI", system-ui, sans-serif; }
      .mono textarea, .mono input { font-family: "Cascadia Code", Consolas,
        "SF Mono", monospace !important; font-size: 12.5px !important; }
      .feed { font-family: "Cascadia Code", Consolas, monospace;
        font-size: 12px; white-space: pre-wrap; word-break: break-word; }
      .card { border: 1px solid #e5e7eb; border-radius: 10px;
        background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
    </style>
    """)

    # ---------------------------------------------------------------- header

    with ui.header().classes(
            "items-center justify-between px-5 py-3 shadow-sm"
    ).style("background: #0f172a"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("biotech", size="28px").style("color:#38bdf8")
            with ui.column().classes("gap-0"):
                ui.label("RoadRunner Agent").classes(
                    "text-white text-lg font-semibold leading-tight")
                ui.label("model diagnosis proof of concept").classes(
                    "text-xs").style("color:#94a3b8")
        status = ui.badge("no model", color="grey-7").classes("px-3 py-1")

    # A missing key fails only when the user clicks - say so up front instead.
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        with ui.element("div").classes("w-full px-5 py-2").style(
                "background:#fef3c7; border-bottom:1px solid #fcd34d"):
            ui.label(
                "ANTHROPIC_API_KEY is not set in this process. If you used "
                "setx, close this terminal and start the app from a new one - "
                "setx only affects processes started afterwards."
            ).classes("text-sm").style("color:#92400e")

    # --------------------------------------------------------------- widgets
    # Declared before the handlers that reference them.

    plot = None
    param_container = None
    species_select = None
    feed_column = None
    report_column = None
    slider_container = None

    def refresh_status() -> None:
        if state.busy:
            status.text = "agent running"
            status.props("color=amber-8")
        elif not state.session.loaded:
            status.text = "no model"
            status.props("color=grey-7")
        else:
            n = len(state.session.rr.getFloatingSpeciesIds())
            status.text = f"{n} species loaded"
            status.props("color=green-7")

    # ------------------------------------------------------------- callbacks

    def do_load(announce: bool = True) -> bool:
        try:
            state.session.load(source.value)
        except Exception as exc:
            ui.notify(f"Antimony error: {exc}", type="negative",
                      multi_line=True, timeout=8000)
            return False
        state.selected = list(state.session.rr.getFloatingSpeciesIds())
        state.handoff = None
        build_parameters()
        build_sliders()
        species_select.options = state.selected
        species_select.value = list(state.selected)
        # Keep the report and activity on screen - they are the record of the
        # last question. Disarm their buttons: they would act on the session
        # that has just been replaced.
        for button in state.report_actions:
            button.set_enabled(False)
        if state.report_note is not None:
            state.report_note.text = (
                "A model has been loaded since this report was produced, so "
                "its buttons are disabled. Ask again to get a current answer.")
        if announce:
            ui.notify("Model loaded", type="positive")
        refresh_status()
        do_simulate()
        return True

    def build_parameters() -> None:
        param_container.clear()
        state.value_fields = {}
        rr = state.session.rr
        ids = list(rr.getGlobalParameterIds())
        inits = list(rr.getFloatingSpeciesInitialConcentrationIds())
        with param_container:
            if not ids and not inits:
                ui.label("no parameters").classes("text-xs text-gray-500")
            for group, identifiers in (("parameters", ids),
                                       ("initial values", inits)):
                if not identifiers:
                    continue
                ui.label(group).classes(
                    "text-xs uppercase tracking-wide text-gray-500 mt-2")
                with ui.grid(columns=2).classes("gap-x-3 gap-y-1 w-full"):
                    for ident in identifiers:
                        label = ident.replace("init([", "").replace("])", "₀")
                        ui.label(label).classes("text-sm self-center")
                        field = ui.number(value=float(rr[ident]), format="%.6g") \
                            .props("dense outlined").classes("mono w-full")
                        field.on("blur", lambda _, i=ident, f=field:
                                 set_value(i, f.value))
                        state.value_fields[ident] = field

    def set_value(ident: str, value: float | None) -> None:
        if value is None or state.busy:
            return
        try:
            state.session.rr[ident] = float(value)
        except Exception as exc:
            ui.notify(f"{ident}: {exc}", type="negative")
            return
        do_simulate()

    def do_simulate() -> None:
        if not state.session.loaded:
            return
        state.start, state.end = float(start_in.value), float(end_in.value)
        state.points = int(points_in.value)
        selections = ["time"] + list(species_select.value or [])
        try:
            state.session.simulate(state.start, state.end, state.points,
                                   selections)
            state.error = None
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            state.session.last_sim = None
            ui.notify(state.error, type="negative", multi_line=True,
                      timeout=8000)
        draw_plot()

    def draw_plot() -> None:
        figure = go.Figure()
        result = state.session.last_result
        if result is not None and state.error is None:
            data = np.asarray(result)
            names = list(result.colnames)
            for index, name in enumerate(names[1:], start=1):
                figure.add_trace(go.Scatter(
                    x=data[:, 0], y=data[:, index], name=name, mode="lines",
                    line=dict(width=2.2,
                              color=PALETTE[(index - 1) % len(PALETTE)])))
        figure.update_layout(
            margin=dict(l=55, r=20, t=28, b=45),
            height=380,
            paper_bgcolor="white", plot_bgcolor="#fbfcfe",
            xaxis=dict(title="time", gridcolor="#eef1f6", zeroline=False),
            yaxis=dict(title="concentration", gridcolor="#eef1f6",
                       zeroline=False,
                       range=state.y_range, autorange=state.y_range is None),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            font=dict(family="Inter, Segoe UI, sans-serif", size=12),
        )
        if state.error:
            figure.add_annotation(
                text=f"simulation failed<br><span style='font-size:11px'>"
                     f"{state.error[:120]}</span>",
                showarrow=False, font=dict(color="#dc2626", size=14))
        plot.figure = figure
        plot.update()

    # ---------------------------------------------------------------- sliders

    def build_sliders() -> None:
        """One slider per parameter and initial value.

        Simulation is ~6 ms for a 1000-point run, so the model is not the
        limit; the round trip and the redraw are. Slider moves are therefore
        coalesced (see apply_pending) rather than simulated one for one.
        """
        slider_container.clear()
        state.sliders = {}
        rr = state.session.rr
        groups = (("parameters", list(rr.getGlobalParameterIds())),
                  ("initial values",
                   list(rr.getFloatingSpeciesInitialConcentrationIds())))
        with slider_container:
            for title, identifiers in groups:
                if not identifiers:
                    continue
                ui.label(title).classes(
                    "text-xs uppercase tracking-wide text-gray-500 mt-2")
                for ident in identifiers:
                    current = float(rr[ident])
                    # A sensible span with no knowledge of the model: bracket
                    # the current value. Zero has no scale of its own, so give
                    # it a unit range.
                    top = 2.0 * current if current > 0 else 1.0
                    label = ident.replace("init([", "").replace("])", "₀")
                    with ui.row().classes("items-center w-full gap-2 no-wrap"):
                        ui.label(label).classes(
                            "text-sm w-16 shrink-0 mono")
                        slider = ui.slider(
                            min=0.0, max=top, step=top / 200.0, value=current,
                            on_change=lambda e, i=ident: queue_change(i, e.value),
                        ).props("dense label").classes("grow")
                        # 'change' fires on release; on_change fires
                        # continuously. The range may only grow while the
                        # handle is moving, and may only shrink once it has
                        # been let go (see grow_if_pinned / refit_on_release).
                        slider.on("change", lambda _, i=ident: refit_on_release(i))
                        readout = ui.label(f"{current:.4g}").classes(
                            "text-sm w-20 text-right mono text-gray-700")
                    state.sliders[ident] = (slider, readout)
            if not state.sliders:
                ui.label("no parameters to vary").classes(
                    "text-xs text-gray-500")

    def set_range(ident: str, top: float) -> None:
        """Rescale one slider, keeping roughly 200 steps of resolution."""
        entry = state.sliders.get(ident)
        if entry is None:
            return
        slider, _ = entry
        top = max(top, 1e-12)
        slider._props["max"] = top
        slider._props["step"] = top / 200.0
        slider.update()

    def queue_change(ident: str, value) -> None:
        """Record a slider move; the timer applies the latest one."""
        if state.busy or value is None:
            return
        value = float(value)
        state.pending[ident] = value
        grow_if_pinned(ident, value)

    def grow_if_pinned(ident: str, value: float) -> None:
        """Give more room when the handle reaches the top of its range.

        Growth is the half of adaptive ranging that has an unambiguous
        trigger: arriving at the end *is* the request for more. It happens
        during the drag, because that is when the user wants it.
        """
        entry = state.sliders.get(ident)
        if entry is None:
            return
        slider, _ = entry
        top = float(slider._props["max"])
        if value >= top - float(slider._props["step"]) and top < 1e9:
            set_range(ident, top * 2.0)

    def refit_on_release(ident: str) -> None:
        """Take room back, but only once the handle has been let go.

        Shrinking has no natural trigger: sitting mid-range does not mean the
        range is too wide. The one unambiguous case is a handle jammed against
        zero, where every useful value is in the first few pixels - so refit
        only when the value has ended up in the bottom quarter, and only after
        the drag, so the handle never moves under the pointer.
        """
        entry = state.sliders.get(ident)
        if entry is None or state.busy:
            return
        slider, _ = entry
        value = float(slider.value or 0.0)
        top = float(slider._props["max"])
        if value > 0 and top > 4.0 * value:
            set_range(ident, 2.0 * value)

    def apply_pending() -> None:
        """Apply coalesced slider moves at a fixed rate, and redraw once.

        Dragging emits far more events than the plot can be redrawn for.
        Applying only the newest value per identifier keeps the simulation in
        step with the slider instead of queueing behind it.
        """
        if not state.pending or state.busy:
            return
        moves, state.pending = state.pending, {}
        rr = state.session.rr
        for ident, value in moves.items():
            try:
                rr[ident] = value
            except Exception:
                continue
            field = state.value_fields.get(ident)
            if field is not None:
                field.value = value
            entry = state.sliders.get(ident)
            if entry is not None:
                entry[1].text = f"{value:.4g}"
        do_simulate()

    def toggle_y_lock(locked: bool) -> None:
        result = state.session.last_result
        if locked and result is not None:
            data = np.asarray(result)[:, 1:]
            low, high = float(data.min()), float(data.max())
            pad = 0.05 * (high - low) if high > low else 1.0
            state.y_range = [low - pad, high + pad]
        else:
            state.y_range = None
        draw_plot()

    def copy_values_to_source() -> None:
        """Make what the user found by dragging permanent, in their own text."""
        try:
            text, applied, missing = state.session.write_values_to_source()
        except Exception as exc:
            ui.notify(f"{type(exc).__name__}: {exc}", type="negative")
            return
        source.value = text
        source.update()
        if applied:
            ui.notify("Written into your model text: " + ", ".join(applied),
                      type="positive", multi_line=True, timeout=8000)
        else:
            ui.notify("Nothing to write - the model text already matches "
                      "the live values.", type="info")
        if missing:
            ui.notify("Not found in your source, so left unwritten: "
                      + ", ".join(missing), type="warning", multi_line=True,
                      timeout=10000)

    def reset_sliders() -> None:
        """Back to the values the model text specifies."""
        try:
            state.session.load(source.value)
        except Exception as exc:
            ui.notify(f"Antimony error: {exc}", type="negative")
            return
        build_parameters()
        build_sliders()
        do_simulate()

    # ------------------------------------------------------------ the agent

    def push(kind: str, payload: Any) -> None:
        """Called from the worker thread - append only, never touch the UI."""
        state.events.append((kind, payload))

    def drain_events() -> None:
        while state.events:
            kind, payload = state.events.popleft()
            render_event(kind, payload)

    def render_event(kind: str, payload: Any) -> None:
        with feed_column:
            if kind == "turn":
                ui.label(f"turn {payload}").classes(
                    "text-xs uppercase tracking-wider text-gray-400 mt-3")
            elif kind == "text":
                ui.markdown(payload).classes(
                    "text-sm text-gray-700 border-l-2 border-gray-300 pl-3")
            elif kind == "code":
                with ui.element("div").classes(
                        "w-full rounded-md p-2 feed").style(
                        "background:#0f172a; color:#e2e8f0"):
                    ui.html(f"<span style='color:#38bdf8'>&gt;&gt;&gt;</span> "
                            f"{_escape(payload)}")
            elif kind == "output":
                text = payload if len(payload) < 2000 else payload[:2000] + " ..."
                with ui.element("div").classes(
                        "w-full rounded-md p-2 feed").style(
                        "background:#f8fafc; color:#334155; "
                        "border:1px solid #e2e8f0"):
                    ui.html(_escape(text))
            elif kind == "limit":
                ui.label(str(payload)).classes(
                    "text-sm text-amber-700 font-medium")
            elif kind == "refusal":
                ui.label(f"refused: {payload}").classes("text-sm text-red-700")
        feed_scroll.scroll_to(percent=1.0)

    def _escape(text: str) -> str:
        return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    async def do_ask() -> None:
        if not question.value.strip():
            ui.notify("Ask a question first", type="warning")
            return

        # Edited the model text but did not press Load? Load it now. Asking
        # about a model the session is not running gives a confident answer
        # about the wrong model, which is worse than any error message. Only
        # when the text actually differs, though - reloading otherwise would
        # discard the values being explored with the sliders.
        stale = (not state.session.loaded
                 or source.value.strip() != state.session.source_antimony.strip())
        if stale:
            was_loaded = state.session.loaded
            if not do_load(announce=False):
                return
            if was_loaded:
                ui.notify("The model text had changed, so it was loaded "
                          "before asking - the agent sees what is on screen.",
                          type="info", multi_line=True, timeout=7000)

        continuing = bool(follow_up.value and state.handoff
                          and state.handoff.messages)
        state.last_question = question.value
        state.busy = True
        state.stop_requested = False
        state.events.clear()
        report_column.clear()
        state.report_actions = []
        state.report_note = None
        if continuing:
            # Keep the earlier activity: the follow-up builds on it, and the
            # user should be able to see what it is building on.
            with feed_column:
                ui.separator()
                ui.label("FOLLOW-UP").classes(
                    "text-xs uppercase tracking-wider text-indigo-600 mt-2")
        else:
            feed_column.clear()
        set_enabled(False)
        refresh_status()
        timer.activate()

        import agent  # late import: keeps startup fast and key-free

        try:
            handoff = await run.io_bound(
                agent.ask, state.session, question.value,
                model=model_select.value,
                effort=effort_select.value,
                detail=detail_select.value,
                max_turns=int(turns_in.value or agent_config.MAX_TURNS),
                max_seconds=float(seconds_in.value
                                  or agent_config.MAX_SECONDS),
                max_total_tokens=int(tokens_in.value
                                     or agent_config.MAX_TOTAL_TOKENS),
                previous=state.handoff if continuing else None,
                on_event=push,
                stop_check=lambda: state.stop_requested,
            )
        except Exception as exc:
            ui.notify(f"{type(exc).__name__}: {exc}", type="negative",
                      multi_line=True, timeout=12000)
            handoff = None
        finally:
            state.busy = False
            set_enabled(True)
            timer.deactivate()
            drain_events()
            refresh_status()

        state.handoff = handoff
        if handoff is not None:
            # There is now something to follow up on, on this provider.
            follow_up.value = False
            ask_button.text = "Answer my question"
            refresh_follow_up()
            show_report(handoff)
            build_parameters()
            build_sliders()
            do_simulate()

    def set_enabled(enabled: bool) -> None:
        for widget in (load_button, ask_button, source, question,
                       start_in, end_in, points_in, species_select,
                       model_select, effort_select, detail_select,
                       turns_in, seconds_in, tokens_in):
            widget.set_enabled(enabled)
        stop_button.set_visibility(not enabled)

    def metrics_lines(handoff) -> list[str]:
        """What the run cost, in one place - shown and copied."""
        return [
            f"model            {handoff.model} (effort {handoff.effort}, "
            f"{handoff.detail})",
            f"turns            {handoff.turns}",
            f"wall clock       {handoff.seconds:.0f} s",
            f"input tokens     {handoff.total_input_tokens:,} "
            f"({handoff.input_tokens:,} fresh + "
            f"{handoff.cache_write_tokens:,} cache write + "
            f"{handoff.cache_read_tokens:,} cache read)",
            f"output tokens    {handoff.output_tokens:,}",
            f"estimated cost   {handoff.cost_text()}"
            + ("" if handoff.priced else "  (no published rates in PRICING)"),
            f"stopped          {handoff.stopped_because}",
        ]

    def transcript_text(handoff) -> str:
        parts = []
        if handoff.report is not None:
            parts.append(handoff.report.as_markdown())
        parts.append("\n**Run**\n")
        parts.append("\n".join(f"    {line}" for line in metrics_lines(handoff)))
        parts.append("\n---\n")
        for entry in handoff.transcript:
            parts.append(f"```python\n{entry['code']}\n```\n\n{entry['output']}\n")
        return "\n".join(parts)

    def show_report(handoff) -> None:
        report_column.clear()
        change_log = state.session.change_log
        with report_column:
            # Cost first: it was previously buried under the buttons, where
            # nobody found it.
            cost = (f"{handoff.model.replace('claude-', '')} "
                    f"{handoff.effort}/{handoff.detail} · "
                    f"{handoff.turns} turns · {handoff.seconds:.0f}s · "
                    f"{handoff.total_input_tokens:,} in / "
                    f"{handoff.output_tokens:,} out · "
                    f"≈ {handoff.cost_text()}")
            detail = (
                f"input {handoff.total_input_tokens:,} tokens = "
                f"{handoff.input_tokens:,} fresh + "
                f"{handoff.cache_write_tokens:,} written to cache + "
                f"{handoff.cache_read_tokens:,} read from cache "
                f"(cache reads cost a tenth of fresh input).\n"
                f"output {handoff.output_tokens:,} tokens - the agent's "
                f"reasoning, code and report.\n"
                f"Each turn resends the whole conversation, so input grows "
                f"with the square of the turn count.")
            with ui.row().classes("items-center gap-2 w-full"):
                if handoff.report is not None:
                    colours = {"numerical": "blue", "structural": "red",
                               "parametric": "purple", "expected": "green"}
                    ui.badge(handoff.report.classification,
                             color=colours.get(handoff.report.classification,
                                               "grey")).classes("px-3 py-1 text-sm")
                    ui.label(f"session {handoff.report.session_state}").classes(
                        "text-xs text-gray-500")
                ui.space()
                ui.label(cost).classes("text-xs px-2 py-1 rounded").style(
                    "background:#eef2ff; color:#3730a3; font-variant-numeric:"
                    "tabular-nums").tooltip(detail)
                ui.button(icon="content_copy",
                          on_click=lambda: (
                              ui.clipboard.write(transcript_text(handoff)),
                              ui.notify("Report and transcript copied",
                                        type="positive"))) \
                    .props("flat dense round size=sm") \
                    .tooltip("Copy the report and full transcript")

            if handoff.report is None:
                ui.label(f"No report: {handoff.stopped_because}").classes(
                    "text-red-700")
            else:
                report = handoff.report
                ui.markdown(report.finding).classes("text-[15px] leading-relaxed")
                with ui.expansion("Evidence", icon="science").classes(
                        "w-full").props("dense"):
                    ui.markdown(report.evidence).classes("text-sm")

            if handoff.report is not None and handoff.report.recommended_changes:
                with ui.element("div").classes("w-full rounded-md p-3").style(
                        "background:#eef2ff; border:1px solid #c7d2fe"):
                    ui.label("Recommended fix (not yet applied)").classes(
                        "text-sm font-semibold").style("color:#3730a3")
                    for change in handoff.report.recommended_changes:
                        kind = change.get("kind", "value")
                        if kind == "model_text":
                            headline = "rewrite the model"
                        elif kind == "simulation":
                            headline = f"simulate to t = {change.get('value')}"
                        else:
                            headline = (f"set {change['selector']} = "
                                        f"{change.get('value')}")
                        ui.label(f"{headline} — {change['why']}") \
                            .classes("text-sm").style("color:#3730a3")

            ui.separator()
            ui.label("Changes").classes(
                "text-xs uppercase tracking-wide text-gray-500")
            if change_log:
                for what, before, after in change_log:
                    with ui.row().classes("items-center gap-2 text-sm"):
                        ui.label(what).classes("font-medium")
                        ui.label(f"{_fmt(before)} → {_fmt(after)}").classes(
                            "text-gray-600 mono")
            else:
                ui.label("No net change to the session.").classes(
                    "text-sm text-gray-600")

            check = handoff.cross_check(change_log)
            if check["in_session_not_reported"] or check["in_report_not_session"]:
                with ui.element("div").classes(
                        "w-full rounded-md p-3 mt-1").style(
                        "background:#fef2f2; border:1px solid #fecaca"):
                    ui.label("Cross-check discrepancies").classes(
                        "text-sm font-semibold text-red-800")
                    for item in check["in_session_not_reported"]:
                        ui.label(f"changed but not reported: {item}").classes(
                            "text-xs text-red-700")
                    for item in check["in_report_not_session"]:
                        ui.label(f"reported but not applied: {item}").classes(
                            "text-xs text-red-700")

            # The report is the artefact of the run - accepting or reverting
            # must not throw it away.  Disable the buttons and say what
            # happened instead.
            with ui.row().classes("gap-2 mt-2 items-center"):

                def settle(message: str, *, final: bool = True) -> None:
                    # Only a change that actually happened ends the exchange -
                    # a no-op must leave the buttons usable.
                    revert_button.set_enabled(not final)
                    accept_button.set_enabled(not final)
                    outcome.text = message
                    outcome.classes(replace="text-sm text-green-800 font-medium"
                                    if final else "text-sm text-gray-600")

                def on_revert() -> None:
                    try:
                        state.session.revert()
                    except Exception as exc:
                        ui.notify(str(exc), type="negative")
                        return
                    source.value = state.session.source_antimony
                    source.update()
                    build_parameters()
                    do_simulate()
                    settle("Reverted to the state at handoff.")

                def on_accept() -> None:
                    was_edit = state.session.model_edited
                    applied, missing = state.session.accept()
                    source.value = state.session.source_antimony
                    source.update()   # push the new text to the browser
                    if was_edit:
                        settle("Accepted - model text replaced. Your comments "
                               "and layout were lost in the round trip.")
                    elif applied:
                        settle("Accepted - written into your source: "
                               + ", ".join(applied))
                    else:
                        settle("Nothing to write back - the live model already "
                               "matches your source. Use 'Try it' to apply the "
                               "recommendation first.", final=False)
                    if missing:
                        ui.notify(
                            "Not found in your source, so left unwritten: "
                            + ", ".join(missing),
                            type="warning", multi_line=True, timeout=10000)

                def on_apply_recommendation() -> None:
                    applied, failed = [], []
                    for change in handoff.report.recommended_changes:
                        kind = change.get("kind", "value")
                        try:
                            described = apply_recommendation(
                                state.session, change)
                            if kind == "model_text":
                                source.value = change["model_text"].strip()
                                source.update()
                            elif kind == "simulation":
                                end_in.value = coerce(change.get("value"))
                            applied.append(described)
                            build_sliders()
                        except Exception as exc:
                            failed.append(
                                f"{change.get('selector') or kind} ({exc})")
                    build_parameters()
                    do_simulate()
                    if applied:
                        # The live model changed, so writing it back is
                        # available again even after an earlier no-op.
                        accept_button.set_enabled(True)
                        revert_button.set_enabled(True)
                        outcome.text = ("Live model now has " + ", ".join(applied)
                                        + ". Check the plot, then "
                                        "'Write into model text' to keep it.")
                        outcome.classes(replace="text-sm text-indigo-800")
                    if failed:
                        ui.notify("Could not apply: " + ", ".join(failed),
                                  type="negative", multi_line=True)

                try_button = None
                if (handoff.report is not None
                        and handoff.report.recommended_changes):
                    try_button = ui.button("Try it", icon="auto_fix_high",
                              on_click=on_apply_recommendation) \
                        .props("dense color=indigo-8") \
                        .tooltip("Set the recommended values on the live "
                                 "model. Updates the Values boxes and the "
                                 "plot; your model text is not changed.")

                revert_button = ui.button("Discard", icon="undo",
                                          on_click=on_revert) \
                    .props("outline dense") \
                    .tooltip("Put the live model back exactly as it was "
                             "before you handed it to the agent.")
                accept_button = ui.button("Write into model text",
                                          icon="check", on_click=on_accept) \
                    .props("dense color=green-7") \
                    .tooltip("Copy the live values into your Antimony source "
                             "above, so they survive a reload.")
                outcome = ui.label("").classes("text-sm")
                # Registered so a later Load can disarm them: they act on the
                # live session, which a Load replaces.
                state.report_actions = [revert_button, accept_button]
                if try_button is not None:
                    state.report_actions.append(try_button)

            ui.label(
                "\"Try it\" changes the live model only - the Values boxes "
                "and the plot. \"Write into model text\" copies those values "
                "into your Antimony source above, so a reload keeps them."
            ).classes("text-xs text-gray-500")
            state.report_note = ui.label("").classes(
                "text-xs text-amber-800")

            with ui.expansion("Run details", icon="receipt_long")                     .classes("w-full").props("dense"):
                with ui.element("div").classes("w-full p-2 rounded").style(
                        "background:#f8fafc; border:1px solid #e2e8f0"):
                    for line in metrics_lines(handoff):
                        ui.label(line).classes("feed text-xs")
                    ui.label(
                        "Cost is dominated by turns: every turn resends the "
                        "whole conversation, so a model that needs more of "
                        "them can cost more per answer despite a lower "
                        "per-token price."
                    ).classes("text-xs text-gray-500 mt-1")

    def refresh_budget_cost() -> None:
        """A token budget buys very different amounts of work per model.

        400,000 tokens is about $2.80 on Opus and $0.32 on DeepSeek, so the
        number alone is misleading; show what it is worth.
        """
        try:
            total = int(tokens_in.value or 0)
        except (TypeError, ValueError):
            return
        cost = agent_config.budget_cost(model_select.value, total)
        budget_cost_label.text = (
            f"≈ ${cost:.2f} at most" if cost == cost
            else "cost unknown for this model")
        refresh_follow_up()

    def on_follow_up_toggle(checked: bool) -> None:
        """Make room for the new question, and say what the button will do.

        The box still holds the question just asked. Clear it so the user can
        type - but only if they have not already started editing it, so their
        own typing is never thrown away.
        """
        ask_button.text = "Ask follow-up" if checked else "Answer my question"
        if checked and question.value.strip() == state.last_question.strip():
            question.value = ""
            question.update()

    def on_detail_change(detail: str) -> None:
        """Thorough reporting wants thinking to match, so raise effort with it.

        Done visibly rather than behind the scenes: the dropdown moves, so the
        user can see what changed and put it back if they disagree.
        """
        if detail == "thorough" and effort_select.value == "low":
            effort_select.value = "high"
            ui.notify("Effort raised to high to match thorough reporting - "
                      "set it back if you would rather not.",
                      type="info", multi_line=True, timeout=6000)
        refresh_budget_cost()

    def refresh_follow_up() -> None:
        """A conversation cannot be continued on another vendor's API.

        Rather than let the user tick the box and get an exception, hide it
        when the selected model belongs to a different provider than the run
        it would continue.
        """
        import providers

        prior = state.handoff
        if prior is None or not prior.messages:
            follow_up.set_visibility(False)
            return
        try:
            same = (providers.provider_for(prior.model)
                    == providers.provider_for(model_select.value))
        except ValueError:
            same = False
        follow_up.set_visibility(same)
        if not same:
            follow_up.value = False

    def request_stop() -> None:
        state.stop_requested = True
        ui.notify("Stopping after this turn - the agent will report what it has",
                  type="warning")

    def load_example(name: str) -> None:
        case = cases.load(name)
        source.value = case.MODEL
        question.value = case.QUESTION.strip()
        start_in.value, end_in.value, points_in.value = case.SIMULATION
        do_load()
        if hasattr(case, "SETUP"):
            case.SETUP(state.session)
            ui.notify(f"applied {name} solver setup", type="info")
            do_simulate()

    # ---------------------------------------------------------------- layout

    with ui.row().classes("w-full no-wrap gap-4 p-4").style(
            "background:#f6f7f9; min-height:calc(100vh - 64px)"):

        # left: the model
        with ui.column().classes("gap-4").style("width:380px; flex:none"):
            with ui.card().classes("card w-full p-4 gap-2"):
                with ui.row().classes("items-center w-full"):
                    with ui.column().classes("gap-0"):
                        ui.label("Model text").classes("text-sm font-semibold")
                        ui.label("saved and reloaded").classes(
                            "text-xs text-gray-400")
                    ui.space()
                    ui.select(cases.available(), label="example",
                              on_change=lambda e: load_example(e.value)) \
                        .props("dense outlined").classes("w-40 text-xs")
                source = ui.textarea(value=DEFAULT_MODEL) \
                    .props("outlined autogrow input-style=height:260px") \
                    .classes("mono w-full")
                load_button = ui.button("Load", icon="play_arrow",
                                        on_click=do_load) \
                    .props("dense").classes("w-full")

            with ui.card().classes("card w-full p-4 gap-2"):
                with ui.column().classes("gap-0"):
                    ui.label("Live values").classes("text-sm font-semibold")
                    ui.label("what is simulating now - edit to explore").classes(
                        "text-xs text-gray-400")
                param_container = ui.column().classes("w-full gap-1")

            with ui.card().classes("card w-full p-4 gap-2"):
                ui.label("Simulation").classes("text-sm font-semibold")
                with ui.row().classes("gap-2 w-full no-wrap"):
                    start_in = ui.number("start", value=0, format="%g") \
                        .props("dense outlined").classes("w-full")
                    end_in = ui.number("end", value=100, format="%g") \
                        .props("dense outlined").classes("w-full")
                    points_in = ui.number("points", value=1000, format="%d") \
                        .props("dense outlined").classes("w-full")
                species_select = ui.select([], multiple=True, label="species") \
                    .props("dense outlined use-chips").classes("w-full")
                ui.button("Simulate", icon="show_chart", on_click=do_simulate) \
                    .props("dense outline").classes("w-full")

        # right: plot and agent
        with ui.column().classes("gap-4 flex-1 min-w-0"):
            # Plot and sliders side by side: the plot no longer needs the
            # whole window width, and the freed space makes the model
            # explorable by hand.
            with ui.row().classes("w-full no-wrap gap-4 items-stretch"):
                with ui.card().classes("card p-3 flex-1 min-w-0"):
                    plot = ui.plotly(go.Figure()).classes("w-full")

                with ui.card().classes("card p-4 gap-1").style(
                        "width:550px; flex:none"):
                    with ui.row().classes("items-center w-full"):
                        with ui.column().classes("gap-0"):
                            ui.label("Explore").classes(
                                "text-sm font-semibold")
                            ui.label("drag to vary the live model").classes(
                                "text-xs text-gray-400")
                        ui.space()
                        ui.button("Copy to model text", icon="save_alt",
                                  on_click=copy_values_to_source) \
                            .props("flat dense size=sm") \
                            .classes("text-xs") \
                            .tooltip("Write the current values into your "
                                     "Antimony source above, keeping your "
                                     "comments and layout")
                        ui.button(icon="restart_alt", on_click=reset_sliders) \
                            .props("flat dense round size=sm") \
                            .tooltip("Back to the values in the model text")
                    ui.checkbox(
                        "lock y-axis",
                        on_change=lambda e: toggle_y_lock(e.value)) \
                        .props("dense").classes("text-xs") \
                        .tooltip("Stop the axis rescaling while you drag, so "
                                 "you can see the shape change")
                    slider_scroll = ui.scroll_area().classes("w-full").style(
                        "height:330px")
                    with slider_scroll:
                        slider_container = ui.column().classes("w-full gap-1")

            with ui.card().classes("card w-full p-4 gap-3"):
                ui.label("Ask the agent").classes("text-sm font-semibold")
                question = ui.textarea(value=DEFAULT_QUESTION) \
                    .props("outlined autogrow").classes("w-full text-sm")
                with ui.row().classes("items-center gap-2 w-full"):
                    ask_button = ui.button("Answer my question", icon="psychology",
                                           on_click=do_ask) \
                        .props("dense color=indigo-8")
                    stop_button = ui.button("Stop", icon="stop",
                                            on_click=request_stop) \
                        .props("dense outline color=red")
                    stop_button.set_visibility(False)
                    follow_up = ui.checkbox(
                        "follow up",
                        on_change=lambda e: on_follow_up_toggle(e.value))                         .props("dense").classes("text-xs")
                    follow_up.set_visibility(False)
                    follow_up.tooltip(
                        "Continue the last exchange instead of starting "
                        "again: the agent keeps everything it already worked "
                        "out, so a follow-up is much cheaper than a fresh "
                        "question.")
                    ui.space()
                    model_select = ui.select(
                        agent_config.MODEL_CHOICES,
                        value=agent_config.MODEL, label="model",
                        on_change=lambda _: refresh_budget_cost()) \
                        .props("dense outlined").classes("w-52 text-xs") \
                        .tooltip("Sonnet costs roughly a third of Opus per "
                                 "token; Haiku a fifth. Compare report quality "
                                 "before settling on one.")
                    detail_select = ui.select(
                        agent_config.DETAIL_CHOICES,
                        value=agent_config.DETAIL, label="detail",
                        on_change=lambda e: on_detail_change(e.value)) \
                        .props("dense outlined").classes("w-32 text-xs") \
                        .tooltip("brief: the finding, for a modeller who "
                                 "wants the answer. thorough: the mechanism, "
                                 "the alternatives ruled out, and thresholds "
                                 "computed for this model rather than quoted "
                                 "from the literature - longer, and dearer.")
                    effort_select = ui.select(
                        agent_config.EFFORT_CHOICES,
                        value=agent_config.EFFORT, label="effort",
                        on_change=lambda _: refresh_budget_cost()) \
                        .props("dense outlined").classes("w-28 text-xs") \
                        .tooltip("How hard the model works before answering. "
                                 "Lower effort means fewer thinking tokens and "
                                 "usually fewer turns.")

                with ui.expansion("Limits", icon="speed") \
                        .classes("w-full").props("dense"):
                    ui.label(
                        "Reaching a limit does not discard the run: the agent "
                        "is asked to report what it has established, and the "
                        "report says it was cut short."
                    ).classes("text-xs text-gray-500")
                    with ui.row().classes("items-center gap-3 w-full mt-1"):
                        turns_in = ui.number(
                            "max turns", value=agent_config.MAX_TURNS,
                            min=1, step=1, format="%d") \
                            .props("dense outlined").classes("w-28")
                        seconds_in = ui.number(
                            "max seconds", value=agent_config.MAX_SECONDS,
                            min=10, step=30, format="%d") \
                            .props("dense outlined").classes("w-32")
                        tokens_in = ui.number(
                            "token budget",
                            value=agent_config.MAX_TOTAL_TOKENS,
                            min=10000, step=50000, format="%d",
                            on_change=lambda _: refresh_budget_cost()) \
                            .props("dense outlined").classes("w-40")
                        budget_cost_label = ui.label("").classes(
                            "text-xs text-gray-600")

            with ui.card().classes("card w-full p-4 gap-2"):
                with ui.row().classes("items-center w-full"):
                    ui.label("Activity").classes("text-sm font-semibold")
                    ui.space()
                    ui.button(
                        icon="content_copy",
                        on_click=lambda: (
                            ui.clipboard.write(
                                transcript_text(state.handoff)
                                if state.handoff else ""),
                            ui.notify("Transcript copied" if state.handoff
                                      else "Nothing to copy yet",
                                      type="positive" if state.handoff
                                      else "warning"))) \
                        .props("flat dense round size=sm") \
                        .tooltip("Copy the transcript")
                feed_scroll = ui.scroll_area().classes("w-full").style(
                    "height:300px")
                with feed_scroll:
                    feed_column = ui.column().classes("w-full gap-2")

            with ui.card().classes("card w-full p-4 gap-2"):
                ui.label("Report").classes("text-sm font-semibold")
                report_column = ui.column().classes("w-full gap-2")

    refresh_budget_cost()
    timer = ui.timer(0.25, drain_events, active=False)
    # 20 Hz: comfortably faster than a 6 ms simulation, and slower than the
    # event rate of a dragged slider, which is the point.
    ui.timer(0.05, apply_pending)
    do_load()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="RoadRunner Agent", port=8080, reload=False,
           favicon="🧬", show=True)
