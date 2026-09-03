"""Checks for the hosted Streamlit build. No API key needed, nothing spent.

    .venv/Scripts/python.exe test_streamlit_app.py

Streamlit's own `AppTest` runs the page headlessly and reports anything it
raised, so these are real page builds driven through real widgets - not
imports.

The one that matters is `a loaded model survives a rerun`. `streamlit run`
executes the whole script top to bottom on *every* interaction, so state kept
in a module-level global is silently re-created each time; the app then built
a fresh, unloaded Session on every click and no model ever stayed loaded.
That is what `@st.cache_resource` on the session registry fixes, and this is
the test that fails if anyone moves it back.
"""

from __future__ import annotations

import pathlib
import re
import sys

try:
    from streamlit.testing.v1 import AppTest
except ImportError:                                    # pragma: no cover
    print("streamlit is not installed - skipping (pip install streamlit)")
    sys.exit(0)

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}")


def fresh() -> AppTest:
    app = AppTest.from_file("streamlit_app.py", default_timeout=180)
    app.run()
    return app


def button(app: AppTest, label: str):
    return [b for b in app.button if b.label == label][0]


def slider_labels(app: AppTest) -> list[str]:
    return [s.label for s in app.slider]


print("the page builds")
at = fresh()
check("no exception on first run", not at.exception)
check("the title is there", [t.value for t in at.title] == ["RoadRunner Agent"])
check("a model is loaded without being asked", bool(slider_labels(at)))
check("one plot", len(at.get("plotly_chart")) == 1)
check("the default case's question is in the box",
      "Goodwin" in at.session_state["question"]
      or "oscillator" in at.session_state["question"])

print("a loaded model survives a rerun")
# The regression. Any interaction triggers a full re-execution of the script;
# the session and its model must outlive it.
before = slider_labels(at)
button(at, "Load").click().run()
check("no exception on Load", not at.exception)
check("the sliders are still there afterwards", slider_labels(at) == before)
button(at, "Simulate").click().run()
check("no exception on Simulate", not at.exception)
check("still loaded after a second interaction", slider_labels(at) == before)

print("the model can be explored")
n = [s for s in at.slider if s.label == "n"][0]
n.set_value(12.0).run()
check("dragging a parameter does not raise", not at.exception)
check("the new value sticks",
      [s for s in at.slider if s.label == "n"][0].value == 12.0)

print("a slider keeps its scale while it is dragged")
# The bug this pins down: deriving the span from the *current* value
# re-scales the slider on every rerun, so the scale shifts under the handle
# mid-drag - the value walks away from where it was put and the axis
# collapses towards zero. Dragging n was unusable.
at = fresh()
n = [s for s in at.slider if s.label == "n"][0]
span = (n.proto.min, n.proto.max)
check("n starts on 0..16 for a model with n = 8", span == (0.0, 16.0))
for target in (12.0, 4.0, 9.0):
    [s for s in at.slider if s.label == "n"][0].set_value(target).run()
    moved = [s for s in at.slider if s.label == "n"][0]
    check(f"n = {target:g} sticks", moved.value == target)
    check(f"and the scale is unchanged at n = {target:g}",
          (moved.proto.min, moved.proto.max) == span)

print("the y axis can be held still")
# Autoscaling on every drag makes the plot flicker and hides what you are
# dragging for: an oscillation that grows keeps the same apparent height
# while the axis quietly grows with it.
at = fresh()
check("it autoscales by default", at.session_state["y_range"] is None)
at.checkbox(key="y_lock").set_value(True).run()
locked = at.session_state["y_range"]
check("ticking the box captures a range", locked is not None)
[s for s in at.slider if s.label == "n"][0].set_value(12.0).run()
check("and dragging does not move it", at.session_state["y_range"] == locked)
at.checkbox(key="y_lock").set_value(False).run()
check("unticking returns to autoscale", at.session_state["y_range"] is None)

# Not covered here: that the *plot* reflects the drag rather than lagging a
# rerun behind it. AppTest does not expose a plotly_chart's figure, so the
# only honest check is the one in the browser. `sync_sliders` is what makes
# it true - it applies slider values before anything is drawn.

print("switching examples")
at = fresh()
[s for s in at.selectbox if s.label == "example"][0].set_value(
    "stiff_robertson").run()
button(at, "Load this example").click().run()
check("no exception", not at.exception)
check("the new model's parameters replace the old ones",
      slider_labels(at) == ["k1", "k2", "k3", "A₀", "B₀", "C₀"])
check("and its question comes with it",
      "CVODE" in at.session_state["question"])

print("bad input is reported, not raised")
at = fresh()
at.text_area(key="source").set_value("this is not antimony ///").run()
button(at, "Load").click().run()
check("the page survives", not at.exception)
check("and says what was wrong",
      "Antimony error" in (at.session_state["sim_error"] or ""))

print("the report's buttons act on the live model")
# The bug this pins down: `sync_sliders` runs before the report panel is
# drawn, so a recommendation applied by "Try it" was overwritten by the
# slider positions from before the fix - the click appeared to do nothing.
import agent as _agent

at = fresh()
at.session_state["handoff"] = _agent.Handoff(
    report=_agent.Report(
        classification="parametric",
        finding="n is below the Hopf bifurcation.",
        evidence="Raising n to 10 gives a sustained limit cycle.",
        changes=[], session_state="restored",
        recommended_changes=[{"kind": "value", "selector": "n", "value": "10",
                              "why": "crosses the bifurcation"}]),
    model="claude-sonnet-5", effort="low", detail="brief", turns=3)
at.run()
check("the report renders", not at.exception)
check("n is still 8 before the button is pressed",
      [s for s in at.slider if s.label == "n"][0].value == 8.0)

button(at, "Try it").click().run()
check("no exception applying it", not at.exception)
check("Try it puts the recommended value on the live model",
      [s for s in at.slider if s.label == "n"][0].value == 10.0)

button(at, "Write into model text").click().run()
check("no exception writing it back", not at.exception)
# Matched loosely on purpose: the substitution keeps the user's own
# spacing, so the file that had "n  = 8;" aligned gets "n  = 10;" back.
check("and the model text now carries it",
      re.search(r"^n\s*=\s*10\b", at.session_state["source"], re.M)
      is not None)
check("while the user's comments and layout survive",
      at.session_state["source"].lstrip().startswith("// Goodwin oscillator"))

print("each provider brings its own key")
# The 400 this pins down: an identity-linked Anthropic key is refused
# without an `anthropic-workspace-id` header, and the error reads like an
# auth failure. `agent.make_client` sends it; the hosted build has to too.
at = fresh()
labels = [t.label for t in at.sidebar.text_input]
check("the Anthropic key box is shown for a claude model",
      "Anthropic API key" in labels)
check("and so is the workspace id an identity-linked key needs",
      "Anthropic workspace ID" in labels)
check("both anthropic and deepseek models are offered",
      any(m.startswith("deepseek") for m in
          [s for s in at.sidebar.selectbox if s.label == "model"][0].options))

[s for s in at.sidebar.selectbox if s.label == "model"][0].set_value(
    "deepseek-v4-pro").run()
labels = [t.label for t in at.sidebar.text_input]
check("switching to DeepSeek asks for a DeepSeek key",
      "DeepSeek API key" in labels)
check("and drops the Anthropic-only workspace box",
      "Anthropic workspace ID" not in labels)
button(at, "Answer my question").click().run()
check("it names the vendor whose key is missing",
      any("DeepSeek API key" in w.value for w in at.warning))
check("keys are held per provider, so switching loses neither",
      "api_key_anthropic" in at.session_state
      and "api_key_deepseek" in at.session_state)

print("the agent refuses to run without a key")
at = fresh()
button(at, "Answer my question").click().run()
check("no exception", not at.exception)
check("it asks for a key instead of failing mid-run",
      any("API key" in w.value for w in at.warning))
check("and nothing was spent", at.session_state["handoff"] is None)

print("behind a password, the page is still fully populated")
# The fault this pins down reached the deployment because no test set a
# password. `init_state()` ran before the gate, seeding source/question/
# start/end/points as plain values on a run that rendered none of their
# widgets - and Streamlit discards state for widgets a run did not build.
# The boxes came up empty and the point count fell under the solver's
# minimum. The plot still drew, which made it look like a display bug.
secrets = pathlib.Path(".streamlit/secrets.toml")
if secrets.exists():
    print("  skipped - .streamlit/secrets.toml already exists, leaving it alone")
else:
    secrets.parent.mkdir(exist_ok=True)
    secrets.write_text('APP_PASSWORD = "test-only-password"\n',
                       encoding="utf-8")
    try:
        at = fresh()
        check("the gate is shown when a password is configured",
              any(t.label == "Password" for t in at.text_input))
        check("and nothing else is", not at.text_area)

        at.text_input[0].set_value("wrong").run()
        check("a wrong password does not let you in",
              any(t.label == "Password" for t in at.text_input))

        at.text_input[0].set_value("test-only-password").run()
        check("the right one does", not at.exception and bool(at.text_area))
        values = {n.label: n.value for n in at.number_input}
        check("start, end and points survive the gate",
              (values.get("start"), values.get("end"), values.get("points"))
              == (0.0, 100.0, 1000))
        check("the point count is never below the solver's minimum",
              values.get("points", 0) >= 2)
        check("the model text survives it too",
              len(at.session_state["source"]) > 100)
        check("and so does the sample question",
              "oscillator" in at.session_state["question"])
        check("the model is loaded and its sliders built",
              "n" in [s.label for s in at.slider])
    finally:
        secrets.unlink()

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print(f"all {PASSED} checks passed")
