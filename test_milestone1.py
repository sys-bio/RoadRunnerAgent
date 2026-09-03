"""Checks for everything in milestone 1 that does not need the API.

    python test_milestone1.py
"""

from __future__ import annotations

import cases
from agent import PythonRunner, Handoff, Report
from session import Session

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


MODEL = """\
J1: -> S1; k1;
J2: S1 -> S2; k2*S1;
J3: S2 -> ; k3*S2;
k1 = 1; k2 = 0.4; k3 = 0.3;
S1 = 0; S2 = 0;
"""


def test_session_basics():
    print("\nsession basics")
    s = Session()
    s.load(MODEL)
    check("model loads", s.loaded)
    result = s.simulate(0, 20, 50)
    check("simulate returns rows", result.shape[0] == 50)
    check("last_sim recorded", s.last_sim is not None and s.last_sim.end == 20)

    # The trap from spec 4.1: current state must not leak into the model text.
    check("S1 advanced by the run", s.rr.S1 > 1.0, f"S1={s.rr.S1}")
    text = s.model_antimony()
    check("model_antimony gives initial conditions, not final state",
          "S1 = 0;" in text, text)
    check("model_antimony restored live state", s.rr.S1 > 1.0, f"S1={s.rr.S1}")


def test_diff_and_revert():
    print("\nchange log and revert")
    s = Session()
    s.load(MODEL)
    s.simulate(0, 20, 50)
    s.snapshot()

    s.rr.k1 = 5.0
    s.rr.integrator.setValue("relative_tolerance", 1e-9)
    s.rr.conservedMoietyAnalysis = True
    log = s.diff_since_snapshot()
    entries = {what: (before, after) for what, before, after in log}

    check("parameter change detected", "parameter k1" in entries, str(entries))
    check("parameter before/after correct",
          entries.get("parameter k1") == (1.0, 5.0), str(entries.get("parameter k1")))
    check("integrator setting change detected",
          "integrator relative_tolerance" in entries, str(entries))
    check("moiety analysis change detected",
          "conserved_moiety_analysis" in entries, str(entries))

    # A parameter change must not masquerade as a model rewrite: the emitted
    # Antimony embeds current values, so a naive text comparison flags one.
    s.snapshot()
    s.rr.k1 = 7.0
    log = s.diff_since_snapshot()
    check("parameter change is not reported as a model rewrite",
          not any(what == "model text" for what, _, _ in log), str(log))
    check("the parameter change itself is still reported",
          any(what == "parameter k1" for what, _, _ in log), str(log))

    # Net, not activity: change something and put it back.
    s.snapshot()
    s.rr.k2 = 99.0
    s.rr.k2 = 0.4
    check("transient change leaves no entry", s.diff_since_snapshot() == [])

    # Revert returns to the handoff state.
    s.snapshot()
    s.rr.k1 = 42.0
    s.rr.integrator.setValue("relative_tolerance", 1e-3)
    s.revert()
    check("revert restores parameter", s.rr.k1 == 1.0, f"k1={s.rr.k1}")
    check("revert restores integrator setting",
          s.rr.integrator.getValue("relative_tolerance") == 1e-9,
          str(s.rr.integrator.getValue("relative_tolerance")))
    check("revert clears the log", s.change_log == [])


def test_model_edit_rebinding():
    print("\nmodel edit rebinding (spec 3.1)")
    s = Session()
    s.load(MODEL)
    s.simulate(0, 20, 50)
    s.snapshot()
    before = id(s.rr)

    edited = MODEL.replace("k2*S1", "k2*S1*S2")
    s.apply_model_edit(edited)
    check("rr is a new object", id(s.rr) != before)
    check("source_antimony untouched by an agent edit",
          s.source_antimony == MODEL)
    log = s.diff_since_snapshot()
    check("model text change appears in the log",
          any(what == "model text" for what, _, _ in log), str(log))

    s.revert()
    check("revert restores the user's model, not the edit",
          "k2*S1*S2" not in s.model_antimony())

    # A runner holding the old rr must see the new one on its next call.
    s2 = Session()
    s2.load(MODEL)
    runner = PythonRunner(s2)
    runner.run("rr.getGlobalParameterIds()")
    first = runner.namespace["rr"]
    s2.apply_model_edit(MODEL.replace("k3 = 0.3", "k3 = 0.3; k4 = 7"))
    runner.run("1")
    check("runner rebinds rr after a model edit",
          runner.namespace["rr"] is not first)
    out, err = runner.run("'k4' in rr.getGlobalParameterIds()")
    check("runner sees the edited model", "True" in out and not err, out)


def test_python_runner():
    print("\nrun_python tool")
    s = Session()
    s.load(MODEL)
    r = PythonRunner(s)

    out, err = r.run("2 + 2")
    check("last expression is repr'd", out.strip() == "4" and not err, out)

    out, err = r.run("print('hello')")
    check("stdout captured", out.strip() == "hello" and not err, out)

    out, err = r.run("x = 41\nx + 1")
    check("statements then expression", out.strip() == "42" and not err, out)

    out, err = r.run("x")
    check("namespace persists across calls", out.strip() == "41", out)

    out, err = r.run("y = 1")
    check("assignment produces no value", out.strip() == "(no output)", out)

    out, err = r.run("1/0")
    check("exception is returned, not raised", err and "ZeroDivisionError" in out)
    check("traceback tail survives", out.strip().endswith("division by zero"), out[-80:])

    out, err = r.run("def f(:")
    check("syntax error is returned", err and "SyntaxError" in out, out)

    out, err = r.run("import sys; sys.exit(1)")
    check("SystemExit does not kill the loop", err and "SystemExit" in out, out)

    out, err = r.run("print('a' * 40000)")
    check("long output truncated", len(out) < 9000, str(len(out)))
    check("truncation is marked", "truncated" in out)

    out, err = r.run("rr.simulate(0, 10, 10000)")
    check("big array not printed in full", len(out) < 9000, str(len(out)))

    out, err = r.run("session.model_antimony().splitlines()[0]")
    check("session is in scope", not err and "libAntimony" in out, out)

    out, err = r.run("import matplotlib.pyplot as plt\n"
                     "plt.plot([1,2,3]); plt.show()\n'plotted'")
    check("matplotlib show() does not hang", not err and "plotted" in out, out)


def test_accept_writes_values_back():
    print("\naccept writes values into the user's source")
    source = """\
// a model with comments worth keeping
J1: -> S1;    v0/(1 + (S3/K)^n);   // production
J2: S1 -> S2; k1*S1;
J3: S2 -> S3; k2*S2;
J4: S3 -> ;   k3*S3;

v0 = 8;
K  = 1;
n  = 8;
k1 = 1; k2 = 1; k3 = 1;

S1 = 0.1;
S2 = 0.2;
S3 = 0.3;
"""
    s = Session()
    s.load(source)
    s.simulate(0, 50, 100)
    s.snapshot()
    s.rr.n = 12.0
    s.rr.k2 = 2.5
    applied, missing = s.accept()

    check("reports what it wrote", sorted(applied) == ["k2 = 2.5", "n = 12"],
          str(applied))
    check("nothing reported missing", missing == [], str(missing))
    check("value substituted in place", "n  = 12;" in s.source_antimony,
          s.source_antimony)
    check("substitution on a shared line touches only its own value",
          "k1 = 1; k2 = 2.5; k3 = 1;" in s.source_antimony, s.source_antimony)
    check("comments preserved",
          "// a model with comments worth keeping" in s.source_antimony)
    check("inline comment preserved", "// production" in s.source_antimony)
    check("untouched values left alone", "v0 = 8;" in s.source_antimony)
    check("layout preserved (K padded)", "K  = 1;" in s.source_antimony)

    # Reloading the accepted text must give the model the agent actually left.
    s2 = Session()
    s2.load(s.source_antimony)
    check("reloading the accepted source reproduces the fix",
          s2.rr.n == 12.0 and s2.rr.k2 == 2.5,
          f"n={s2.rr.n} k2={s2.rr.k2}")


    # The GUI lets a no-op accept be followed by "Try it" and a second accept.
    # The snapshot must survive the first, or the second loses its reference
    # and warns about untouched implicit ids like default_compartment.
    s5 = Session()
    s5.load(source)
    s5.simulate(0, 50, 100)
    s5.snapshot()
    first_applied, first_missing = s5.accept()          # nothing changed yet
    check("no-op accept writes nothing", first_applied == [], str(first_applied))
    check("no-op accept warns about nothing", first_missing == [],
          str(first_missing))
    s5.rr["n"] = 12.0                                    # "Try it"
    second_applied, second_missing = s5.accept()
    check("accept after a no-op still writes", second_applied == ["n = 12"],
          str(second_applied))
    check("no spurious missing after a second accept", second_missing == [],
          str(second_missing))
    check("second accept reached the source", "n  = 12;" in s5.source_antimony)

    # Assignments inside events, assignment rules and rate rules are not
    # initial values and must never be rewritten.
    tricky = """\
J1: -> S1; k1;
J2: S1 -> ; k2*S1;
at (S1 > 10): k1 = 0;
Total := S1 + 1;
S9' = k2;
S9 = 0;
k1 = 5;
k2 = 1;
"""
    s3 = Session()
    s3.load(tricky)
    s3.snapshot()
    s3.rr.k1 = 99.0
    s3.accept()
    check("event body not rewritten", "at (S1 > 10): k1 = 0;" in s3.source_antimony,
          s3.source_antimony)
    check("the real initialisation was rewritten",
          "k1 = 99;" in s3.source_antimony, s3.source_antimony)

    # Accept after a structural rewrite replaces the text outright.
    s4 = Session()
    s4.load(source)
    s4.snapshot()
    s4.apply_model_edit(source.replace("k2*S2", "k2*S2*S1"))
    s4.accept()
    check("structural edit replaces the source",
          "k2*S2*S1" in s4.source_antimony, s4.source_antimony[:200])


def test_cross_check_matching():
    """Real strings from the first evaluation run.

    The agent's prose and the session's log key describe the same change in
    different words; matching must not depend on the wording.
    """
    print("\ncross-check matching (from real runs)")
    from agent import cross_check_changes

    # stiff_robertson: correctly reported, but the first matcher flagged it
    # as both unreported and phantom.
    reported = [{"selector": "integrator.stiff",
                 "what": "Enabled stiff (BDF) mode on the CVODE integrator",
                 "before": "stiff = False", "after": "stiff = True"}]
    result = cross_check_changes(reported, [("integrator stiff", False, True)])
    check("prose description matches the log key",
          result["in_session_not_reported"] == [], str(result))
    check("and is not also reported as phantom",
          result["in_report_not_session"] == [], str(result))

    # stochastic_variation: genuinely left a change behind and reported none.
    result = cross_check_changes([], [("integrator seed", 937090477, 12345)])
    check("a real omission is still caught",
          result["in_session_not_reported"] == ["integrator seed"], str(result))

    # A claim with no corresponding change is still phantom.
    result = cross_check_changes(
        [{"selector": "k1", "what": "raised k1", "before": "1", "after": "5"}], [])
    check("unapplied claim still detected",
          result["in_report_not_session"] == ["k1"], str(result))

    # Wording that shares no identifier must not match by accident.
    result = cross_check_changes(
        [{"selector": "k1", "what": "raised k1", "before": "1", "after": "5"}],
        [("parameter k2", 1.0, 2.0)])
    check("different parameters do not match each other",
          result["in_session_not_reported"] == ["parameter k2"]
          and result["in_report_not_session"] == ["k1"], str(result))


def test_cross_check():
    print("\nreport cross-check")
    h = Handoff(report=Report(
        classification="numerical", finding="f", evidence="e",
        changes=[{"selector": "k1", "what": "parameter k1",
                  "before": "1", "after": "5"}],
        session_state="fix applied"))

    both = h.cross_check([("parameter k1", 1.0, 5.0)])
    check("matching change is clean",
          not both["in_session_not_reported"] and not both["in_report_not_session"])

    omitted = h.cross_check([("parameter k1", 1.0, 5.0),
                             ("integrator stiff", True, False)])
    check("omission detected",
          omitted["in_session_not_reported"] == ["integrator stiff"],
          str(omitted))

    claimed = h.cross_check([])
    check("unapplied claim detected",
          claimed["in_report_not_session"] == ["k1"], str(claimed))


def test_cases_reproduce():
    print("\ncases reproduce their failures")
    import run_case

    stiff = cases.load("stiff_robertson")
    s, err = run_case.build_session(stiff)
    check("stiff case fails as documented",
          err is not None and "CV_TOO_MUCH_WORK" in err, str(err))
    s2 = Session()
    s2.load(stiff.MODEL)
    try:
        s2.simulate(*stiff.SIMULATION)
        default_ok = True
    except Exception:
        default_ok = False
    check("stiff case succeeds with default settings (so the fix is real)",
          default_ok)

    goodwin = cases.load("goodwin_damped")
    s3, err3 = run_case.build_session(goodwin)
    check("goodwin case runs", err3 is None, str(err3))
    import numpy as np
    data = np.asarray(s3.last_result)

    def amplitude(lo, hi):
        window = data[(data[:, 0] > lo) & (data[:, 0] <= hi)]
        return window[:, 1].max() - window[:, 1].min()

    early, late = amplitude(10, 30), amplitude(80, 100)
    check("goodwin case is damped as documented (oscillation decays)",
          late < early / 10, f"early={early:.4g} late={late:.4g}")
    s3.rr.reset()
    s3.rr.n = 12
    data = np.asarray(s3.rr.simulate(0, 200, 2000))
    tail = data[data[:, 0] > 150]
    check("goodwin oscillates at n=12 (so the fix is real)",
          tail[:, 1].max() - tail[:, 1].min() > 1.0)


def test_handoff_payload():
    print("\nhandoff payload")
    import prompts
    s = Session()
    s.load(MODEL)
    s.simulate(0, 20, 50)
    s.snapshot()
    text = prompts.build_handoff(s, "why does S2 not settle?")
    check("question included verbatim", "why does S2 not settle?" in text)
    check("source antimony included", "k2*S1" in text)
    check("solver settings included", "relative_tolerance" in text)
    check("downsampled result included", "sampled_rows" in text)
    check("payload is a reasonable size", 1000 < len(text) < 20000, str(len(text)))

    s2 = Session()
    s2.load(MODEL)
    s2.snapshot()
    text2 = prompts.build_handoff(s2, "q")
    check("handles no simulation having been run",
          "has not run a simulation" in text2)


def test_all_cases_reproduce():
    """Every case must load, and must actually show the symptom it claims."""
    print("\nall cases load and reproduce their symptom")
    import numpy as np
    import run_case

    expected = {"numerical", "structural", "parametric", "expected"}
    for name in cases.available():
        case = cases.load(name)
        for attr in ("NAME", "MODEL", "QUESTION", "SIMULATION", "GROUND_TRUTH"):
            if not hasattr(case, attr):
                check(f"{name}: has {attr}", False)
        check(f"{name}: classification is valid",
              case.GROUND_TRUTH["classification"] in expected,
              case.GROUND_TRUTH.get("classification"))
        try:
            session, sim_error = run_case.build_session(case)
            loaded = True
        except Exception as exc:
            loaded, sim_error, session = False, str(exc), None
        check(f"{name}: loads and runs", loaded, str(sim_error))

    # Symptom-specific checks: the point of a case is that it misbehaves.
    def data(name):
        session, err = run_case.build_session(cases.load(name))
        return session, (np.asarray(session.last_result)
                         if session.last_result is not None else None), err

    s, d, err = data("missing_substrate")
    names = list(s.last_result.colnames)
    check("missing_substrate: B actually goes negative",
          d[:, names.index("[B]")].min() < -1,
          str(d[:, names.index("[B]")].min()))

    s, d, err = data("no_steady_state")
    check("no_steady_state: S1 diverges", d[-1, 1] > 100, str(d[-1, 1]))
    try:
        s.rr.steadyState()
        converged = True
    except Exception:
        converged = False
    check("no_steady_state: steadyState really fails", not converged)
    s2 = Session(); s2.load(cases.load("no_steady_state").MODEL); s2.rr.vo = 2.5
    try:
        s2.rr.steadyState(); fixed = True
    except Exception:
        fixed = False
    check("no_steady_state: lowering vo really fixes it", fixed)

    s, d, err = data("conserved_moiety")
    total = d[:, 1] + d[:, 2]
    check("conserved_moiety: total is conserved",
          abs(total.max() - total.min()) < 1e-9, str(total.max() - total.min()))
    check("conserved_moiety: S1 plateaus at 2", abs(d[-1, 1] - 2.0) < 1e-3,
          str(d[-1, 1]))

    s, d, err = data("run_too_short")
    check("run_too_short: nowhere near steady state", d[-1, 1] < 0.25 * 50,
          str(d[-1, 1]))

    s, d, err = data("presupposed_bug")
    total = d[:, 1:].sum(axis=1)
    check("presupposed_bug: premise is false (no drift)",
          abs(total - 10).max() < 1e-10, f"max deviation {abs(total-10).max()}")

    s, d, err = data("stochastic_variation")
    check("stochastic_variation: gillespie is actually selected",
          s.rr.integrator.getName() == "gillespie", s.rr.integrator.getName())
    finals = []
    for seed in (1, 2, 3):
        s.rr.integrator.setValue("seed", seed)
        s.rr.reset()
        finals.append(float(np.asarray(s.rr.simulate(0, 20, 200))[-1, 1]))
    check("stochastic_variation: runs really differ", len(set(finals)) > 1,
          str(finals))


def test_request_shape_per_model():
    """Each model gets the parameters it actually accepts.

    Haiku 4.5 predates adaptive thinking and `output_config.effort`; sending
    either returns 400 "adaptive thinking is not supported on this model".
    Verified against the live API for all three models.
    """
    print("\nper-model request shape")
    import agent

    for model in ("claude-opus-5", "claude-sonnet-5"):
        shape = agent.request_shape(model, "low")
        check(f"{model}: adaptive thinking",
              shape["thinking"] == {"type": "adaptive"}, str(shape))
        check(f"{model}: effort passed through",
              shape["output_config"]["effort"] == "low", str(shape))

    shape = agent.request_shape("claude-haiku-4-5", "low")
    check("haiku: no adaptive thinking", shape["thinking"]["type"] == "enabled",
          str(shape))
    check("haiku: no output_config at all", "output_config" not in shape,
          str(shape))
    check("haiku: budget below max_tokens",
          shape["thinking"]["budget_tokens"] < agent.MAX_TOKENS, str(shape))
    check("haiku: budget above the 1024 minimum",
          shape["thinking"]["budget_tokens"] >= 1024, str(shape))
    check("haiku: effort still scales the budget",
          agent.request_shape("claude-haiku-4-5", "max")["thinking"]["budget_tokens"]
          > shape["thinking"]["budget_tokens"])


def test_recommendation_kinds_and_cleaning():
    """Recommendations must express non-numeric fixes, and fields stay clean.

    The first evaluation produced three unusable recommendations because the
    schema only allowed numbers: a rate-law fix became `set J3 = 0`, "run it
    longer" became `set end_time = 300`, and a correct model was handed the
    degenerate `set k2 = 0`. Two findings also carried leaked markup.
    """
    print("\nrecommendation kinds and field cleaning")
    from agent import Report, clean_field, SUBMIT_REPORT_TOOL

    schema = SUBMIT_REPORT_TOOL["input_schema"]["properties"]
    item = schema["recommended_changes"]["items"]
    check("recommendation has a kind",
          set(item["properties"]["kind"]["enum"])
          == {"value", "solver", "model_text", "simulation"},
          str(item["properties"]["kind"].get("enum")))
    check("every field is required (strict mode)",
          set(item["required"]) == set(item["properties"]), str(item["required"]))
    check("changes entries carry a selector",
          "selector" in schema["changes"]["items"]["properties"])

    def report(rec):
        return Report(classification="structural", finding="f", evidence="e",
                      changes=[], session_state="restored",
                      recommended_changes=[rec])

    md = report({"kind": "value", "selector": "k1", "value": 5.0,
                 "model_text": "", "end_time": 0, "why": "raise it"}).as_markdown()
    check("value recommendation renders", "set `k1` to `5`" in md, md)

    md = report({"kind": "model_text", "selector": "", "value": 0,
                 "model_text": "J3: A + B -> C; k1*A*B;", "end_time": 0,
                 "why": "rate law must contain B"}).as_markdown()
    check("model_text recommendation renders", "rewrite the model" in md, md)
    check("corrected model is included", "k1*A*B" in md, md)

    md = report({"kind": "simulation", "selector": "", "value": 0,
                 "model_text": "", "end_time": 300.0,
                 "why": "0.2 of a time constant"}).as_markdown()
    check("simulation recommendation renders", "simulate to t = 300" in md, md)

    md = Report(classification="expected", finding="f", evidence="e",
                changes=[], session_state="restored",
                recommended_changes=[]).as_markdown()
    check("no recommendation section when none is warranted",
          "Recommended" not in md, md)

    leaked = ('There is no steady state for this parameter set, so nleq2 '
              'cannot converge.</finding> '
              '<parameter name="evidence">Computed analytically: 0.0707')
    check("leaked close tag truncates the field",
          clean_field(leaked).endswith("cannot converge."),
          repr(clean_field(leaked)))
    # A field that merely *opens* with a stray tag must not be emptied.
    check("a short field is not destroyed by a leading tag",
          clean_field("</antml>ok") == "ok", repr(clean_field("</antml>ok")))
    check("ordinary maths is untouched",
          clean_field("k1 < k2 and S1 > 0") == "k1 < k2 and S1 > 0",
          repr(clean_field("k1 < k2 and S1 > 0")))
    check("empty stays empty", clean_field("") == "")


def test_apply_recommendation_kinds():
    """Every kind must be applicable, including the ones a run exposed.

    The second evaluation produced `kind=simulation, end_time=0` because the
    agent wanted to recommend an integrator setting and the schema had no way
    to say it. A schema that cannot express the right answer gets filled with
    a wrong one.
    """
    print("\napplying recommendations")
    from session import apply_recommendation, coerce

    check("coerce reads integers as int", coerce("12") == 12 and isinstance(coerce("12"), int))
    check("coerce reads exponents", coerce("1e-9") == 1e-9)
    check("coerce reads booleans", coerce("true") is True and coerce("False") is False)
    check("coerce leaves names alone", coerce("cvode") == "cvode")

    def fresh():
        s = Session(); s.load(MODEL); s.simulate(0, 20, 50); s.snapshot()
        return s

    s = fresh()
    out = apply_recommendation(s, {"kind": "value", "selector": "k1",
                                   "value": "5", "model_text": "", "why": ""})
    check("value applies", s.rr.k1 == 5.0 and "k1" in out, out)

    s = fresh()
    out = apply_recommendation(s, {"kind": "solver", "selector": "integrator",
                                   "value": "gillespie", "model_text": "",
                                   "why": ""})
    check("solver switch applies", s.rr.integrator.getName() == "gillespie", out)

    # `seed` exists only on gillespie - this is the stochastic_variation case.
    s = fresh()
    s.rr.setIntegrator("gillespie")
    out = apply_recommendation(s, {"kind": "solver",
                                   "selector": "integrator.seed",
                                   "value": "12345", "model_text": "", "why": ""})
    check("solver setting applies",
          s.rr.integrator.getValue("seed") == 12345, out)

    # A setting the current solver does not have must fail loudly.
    s = fresh()
    try:
        apply_recommendation(s, {"kind": "solver", "selector": "integrator.seed",
                                 "value": "1", "model_text": "", "why": ""})
        rejected = False
    except Exception:
        rejected = True
    check("a setting the solver lacks is reported, not swallowed", rejected)

    s = fresh()
    out = apply_recommendation(s, {"kind": "solver",
                                   "selector": "integrator.stiff",
                                   "value": "false", "model_text": "", "why": ""})
    check("boolean solver setting applies",
          s.rr.integrator.getValue("stiff") is False, out)

    s = fresh()
    out = apply_recommendation(s, {"kind": "model_text", "selector": "",
                                   "value": "",
                                   "model_text": MODEL.replace("k2*S1", "k2*S1*S2"),
                                   "why": ""})
    check("model_text applies", "k2*S1*S2" in s.model_antimony(), out)

    s = fresh()
    out = apply_recommendation(s, {"kind": "simulation", "selector": "end_time",
                                   "value": "300", "model_text": "", "why": ""})
    check("simulation returns the new end time", "300" in out, out)

    # The failure the run produced must now be rejected loudly, not applied.
    s = fresh()
    try:
        apply_recommendation(s, {"kind": "simulation", "selector": "integrator",
                                 "value": "0", "model_text": "", "why": ""})
        rejected = False
    except ValueError:
        rejected = True
    check("a zero end time is rejected, not silently applied", rejected)


def test_solver_switch_diff_is_not_noisy():
    """Switching integrator must log one change, not a dozen.

    Different solvers have different setting lists, so diffing the union
    reports every non-shared setting as "<absent> -> value". One real run
    produced eleven such entries for a single switch.
    """
    print("\nsolver switch produces a clean diff")
    s = Session()
    s.load(MODEL)
    s.rr.setIntegrator("gillespie")
    s.simulate(0, 20, 50)
    s.snapshot()
    s.rr.setIntegrator("cvode")
    log = s.diff_since_snapshot()
    keys = [what for what, _, _ in log]
    check("the switch itself is logged", "integrator" in keys, str(keys))
    check("no phantom <absent> settings",
          not any(b == "<absent>" or a == "<absent>" for _, b, a in log),
          str(log))
    check("the switch is the only entry", len(log) == 1, str(keys))


def test_copy_explored_values_to_source():
    """The Explore panel's counterpart to Accept, with no agent involved."""
    print("\ncopy explored values into the model text")
    source = """\
// worth keeping
J1: -> S1;    v0/(1 + (S3/K)^n);
J2: S1 -> S2; k1*S1;
J3: S2 -> S3; k2*S2;
J4: S3 -> ;   k3*S3;

v0 = 8;
n  = 8;
K  = 1;
k1 = 1; k2 = 1; k3 = 1;
S1 = 0.1; S2 = 0.2; S3 = 0.3;
"""
    s = Session()
    s.load(source)
    s.rr.n = 12.32                      # dragged the slider
    text, applied, missing = s.write_values_to_source()

    check("writes the dragged value", applied == ["n = 12.32"], str(applied))
    check("nothing spurious reported missing", missing == [], str(missing))
    check("value is in the text", "n  = 12.32;" in text, text)
    check("comment survives", "// worth keeping" in text)
    check("untouched values not reformatted", "k1 = 1; k2 = 1; k3 = 1;" in text)
    check("source_antimony updated", s.source_antimony == text)

    # Reloading must reproduce what the user was looking at.
    s2 = Session(); s2.load(text)
    check("reload reproduces the explored model", s2.rr.n == 12.32,
          f"n={s2.rr.n}")

    # A second copy with nothing changed writes nothing.
    _, applied2, _ = s.write_values_to_source()
    check("copying twice is a no-op", applied2 == [], str(applied2))


def test_follow_up_message_assembly():
    """A follow-up must continue the conversation without malforming it.

    A finished Anthropic run ends with the user message carrying the tool
    results, so appending another user message would put two back to back -
    which the API rejects. The follow-up text merges into that message
    instead.
    """
    print("\nfollow-up conversation assembly")
    import providers

    tools = []
    prov = providers.AnthropicProvider.__new__(providers.AnthropicProvider)
    prov.messages = []

    # Shape of a finished run: user handoff, assistant report, tool results.
    finished = [
        {"role": "user", "content": "the handoff"},
        {"role": "assistant", "content": [{"type": "text", "text": "report"}]},
        {"role": "user", "content": [{"type": "tool_result",
                                      "tool_use_id": "t1",
                                      "content": "Report received.",
                                      "is_error": False}]},
    ]
    prov.resume(finished, "and what about the period?")
    roles = [m["role"] for m in prov.messages]
    check("no two user messages in a row",
          not any(a == b == "user" for a, b in zip(roles, roles[1:])), str(roles))
    check("follow-up merged into the trailing user message",
          prov.messages[-1]["content"][-1]["text"] == "and what about the period?",
          str(prov.messages[-1]))
    check("the original conversation is preserved",
          len(prov.messages) == 3 and prov.messages[0]["content"] == "the handoff",
          str(len(prov.messages)))
    check("resume does not mutate the caller's list", len(finished[2]["content"]) == 2
          or finished is not prov.messages)

    # The OpenAI dialect ends on `tool` messages, so a plain append is right.
    op = providers.OpenAICompatibleProvider.__new__(
        providers.OpenAICompatibleProvider)
    op.messages = []
    op.resume([{"role": "system", "content": "s"},
               {"role": "user", "content": "q"},
               {"role": "assistant", "content": None},
               {"role": "tool", "tool_call_id": "t1", "content": "ok"}],
              "follow up")
    check("openai appends a user turn after tool results",
          op.messages[-1] == {"role": "user", "content": "follow up"},
          str(op.messages[-1]))

    # Mixing providers must be refused, not silently mangled.
    import agent
    from session import Session
    s = Session(); s.load(MODEL)
    prior = agent.Handoff(model="claude-sonnet-5",
                          messages=[{"role": "user", "content": "x"}])
    try:
        agent.ask(s, "follow up", model="deepseek-v4-pro", previous=prior)
        refused = False
    except ValueError as exc:
        refused = "message formats differ" in str(exc)
    check("continuing on a different provider is refused", refused)


def test_wind_up_forces_a_report():
    """Hitting a limit must still produce a report.

    A real run hit the 25-turn cap, was asked to report, called run_python
    again instead, and ended with "no report after final request" - the whole
    budget spent and nothing to show. The wind-up turn now offers only
    submit_report, so there is no other tool to reach for.
    """
    print("\nwind-up turn forces a report")
    import agent
    import providers

    class FakeProvider:
        """Answers run_python forever, unless only submit_report is offered."""

        def __init__(self):
            self.messages = []
            self.tools_seen = []

        def start(self, text):
            self.messages = [{"role": "user", "content": text}]

        def turn(self, tools=None):
            names = [t["name"] for t in (tools or [
                agent.RUN_PYTHON_TOOL, agent.SUBMIT_REPORT_TOOL])]
            self.tools_seen.append(names)
            if names == ["submit_report"]:
                return providers.TurnResult(tool_calls=[providers.ToolCall(
                    id="r", name="submit_report", input={
                        "classification": "expected", "finding": "f",
                        "evidence": "e", "changes": [],
                        "recommended_changes": [],
                        "session_state": "restored"})])
            return providers.TurnResult(tool_calls=[providers.ToolCall(
                id="c", name="run_python", input={"code": "1"})])

        def append_results(self, results, extra_text=""):
            self.messages.append({"role": "user", "content": results})

    fake = FakeProvider()
    original = providers.make_provider
    providers.make_provider = lambda **kw: fake
    try:
        s = Session(); s.load(MODEL); s.simulate(0, 10, 20)
        handoff = agent.ask(s, "why?", max_turns=3)
    finally:
        providers.make_provider = original

    check("a report is produced despite hitting the cap",
          handoff.report is not None, str(handoff.stopped_because))
    check("the stop reason names the cap",
          "3-turn cap" in handoff.stopped_because, handoff.stopped_because)
    check("no 'no report after final request'",
          "no report" not in handoff.stopped_because, handoff.stopped_because)
    check("run_python was offered while investigating",
          fake.tools_seen[0] == ["run_python", "submit_report"],
          str(fake.tools_seen[0]))
    check("only submit_report was offered on the wind-up turn",
          fake.tools_seen[-1] == ["submit_report"], str(fake.tools_seen[-1]))


def test_semicolon_is_a_separator_not_a_terminator():
    """Antimony needs a semicolon only *between* statements on a line.

    `n = 8` alone on a line is valid, and so is `k1 = 1; k2 = 1` with no
    trailing one. Verified against libAntimony: only two statements on one
    line with no separator between them fails. `structural_antimony` must
    follow the same rule, or a parameter change in such a file reads as a
    model rewrite and fires the report cross-check falsely.
    """
    print("\nsemicolons: separator, not terminator")
    import tellurium as te
    from session import structural_antimony

    def loads(src):
        try:
            te.loada(src)
            return True
        except Exception:
            return False

    check("no trailing semicolon is valid",
          loads("J1: -> S1; k1\nk1 = 1\nS1 = 0\n"))
    check("semicolon between, none at end, is valid",
          loads("J1: -> S1; k1;\nk1 = 1; S1 = 0\n"))
    check("two statements on a line with no separator fails",
          not loads("J1: -> S1; k1;\nk1 = 1 S1 = 0\n"))

    values = ["n = 8;", "n = 8", "k1 = 1; k2 = 1; k3 = 1;", "k1 = 1; k2 = 1",
              "S1 = 0.1", "v0 = 8e-3;"]
    for line in values:
        check(f"stripped as a value: {line!r}", structural_antimony(line) == "")

    structure = ["J1: -> S1; k1", "J1: -> S1; k1;", "S2' = k1*S1 - k2*S2",
                 "at (S1 > 10): k1 = 0;", "species S1, S2;", "T := S1 + S2",
                 "const Xo;", "compartment cell = 2.0;"]
    for line in structure:
        check(f"kept as structure: {line!r}", structural_antimony(line) != "")

    # The property that matters: changing a value in a semicolon-light file
    # must not read as a structural rewrite.
    source = "J1: -> S1; k1\nJ2: S1 -> ; k2*S1\nk1 = 1; k2 = 0.4\nS1 = 0\n"
    edited = source.replace("k1 = 1", "k1 = 5")
    check("a value change is not a structural change",
          structural_antimony(source) == structural_antimony(edited))
    rewritten = source.replace("k2*S1", "k2*S1*S1")
    check("a rate-law change still is",
          structural_antimony(source) != structural_antimony(rewritten))


def test_detail_levels():
    """Two report styles, and the brief one must stay exactly as measured.

    The eight-case results were obtained with the brief prompt. Changing it
    would invalidate them, so `brief` is asserted identical to the prompt
    those runs used.
    """
    print("\nreport detail levels")
    import prompts
    import agent

    brief = prompts.system_prompt("brief")
    thorough = prompts.system_prompt("thorough")

    check("brief is the historical default",
          brief == prompts.SYSTEM_PROMPT)
    check("an unknown level falls back to brief",
          prompts.system_prompt("rubbish") == brief)
    check("thorough is longer", len(thorough) > len(brief))
    check("no placeholder survives either",
          "{audience}" not in brief + thorough
          and "{report_fields}" not in brief + thorough)

    # Both must keep the parts that make the agent correct, not just readable.
    for name, text in (("brief", brief), ("thorough", thorough)):
        check(f"{name} keeps the RoadRunner hazards", "resetAll" in text)
        check(f"{name} keeps the Antimony reference", "separator" in text)
        check(f"{name} keeps 'do not manufacture a fix'",
              "Do not manufacture a fix" in text)
        check(f"{name} keeps the change-reporting rule",
              "must appear in your report" in text)

    check("brief asks for a few sentences", "in a few sentences" in brief)
    check("thorough does not", "in a few sentences" not in thorough)
    check("thorough asks for excluded alternatives",
          "alternatives" in thorough and "ruled" in thorough)
    check("thorough asks it to compute thresholds, not quote them",
          "compute it for *this* parameterisation" in thorough)

    check("agent exposes both levels",
          agent.DETAIL_CHOICES == ["brief", "thorough"])
    check("agent defaults to brief", agent.DETAIL == "brief")
    check("handoff records the level",
          agent.Handoff(detail="thorough").detail == "thorough")


def test_worker_sandbox():
    """The subprocess sandbox: no credentials, state that persists, crash
    recovery.

    This is the security boundary, so the checks are about what agent code
    *cannot* reach as much as what it can. It costs a few seconds - the
    worker imports tellurium - but a silent regression here is the kind that
    hands a stranger an API key.
    """
    import os
    from remote import WorkerSession, WorkerCrashed

    canary = "sk-canary-do-not-leak"
    os.environ["ANTHROPIC_API_KEY"] = canary
    os.environ["A_PRIVATE_SETTING"] = canary

    with WorkerSession(timeout=20) as w:
        env = w._request("environment")
        check("worker cannot see ANTHROPIC_API_KEY",
              "ANTHROPIC_API_KEY" not in env)
        check("worker cannot see unrelated host variables",
              "A_PRIVATE_SETTING" not in env)
        check("no allowlisted variable carries the canary",
              not any(canary in v for v in env.values()))
        check("worker gets a scratch home, not the user's",
              env.get("HOME", "") not in ("", os.path.expanduser("~")))

        out, _ = w.run("import os; print(os.environ.get('ANTHROPIC_API_KEY'))")
        check("agent code reads no key out of the environment",
              "None" in out and canary not in out)

        w.load(MODEL)
        check("model loads in the worker",
              sorted(w.rr.getFloatingSpeciesIds()) == ["S1", "S2"])
        check("values read through the facade", w.rr.k1 == 1.0)
        w.rr.k1 = 3.5
        check("values write through the facade", w.rr["k1"] == 3.5)

        out, is_error = w.run("kept = 21")
        check("assignment produces no output", out == "(no output)" and not is_error)
        out, _ = w.run("kept * 2")
        check("the namespace persists between calls", out.strip() == "42")
        out, _ = w.run("rr.k1")
        check("agent code sees the host's parameter change",
              out.strip() == "3.5")

        result = w.simulate(0, 10, 5)
        check("simulate returns rows across the pipe", result.shape[0] == 5)
        check("simulate keeps its column names",
              result.colnames[0] == "time"
              and result.shape[1] == len(result.colnames))

        out, is_error = w.run("1/0")
        check("an exception is an error result, not a crash",
              is_error and "ZeroDivisionError" in out)

        out, is_error = w.run("import os; os._exit(1)")
        check("a process death is reported, not raised",
              is_error and "died" in out)
        check("the agent is told its namespace is gone", "restarted" in out)
        out, is_error = w.run("print(rr.getFloatingSpeciesIds())")
        check("the worker restarts with the model reloaded",
              not is_error and "S1" in out)
        out, is_error = w.run("kept")
        check("variables really are gone after a restart",
              is_error and "NameError" in out)

    w2 = WorkerSession(timeout=3)
    try:
        out, is_error = w2.run("while True: pass")
        check("a hang is killed and reported", is_error and "did not answer" in out)
        out, is_error = w2.run("21 + 21")
        check("the worker is usable after a hang",
              not is_error and out.strip() == "42")
    finally:
        w2.close()

    for name in ("ANTHROPIC_API_KEY", "A_PRIVATE_SETTING"):
        os.environ.pop(name, None)


if __name__ == "__main__":
    test_session_basics()
    test_diff_and_revert()
    test_model_edit_rebinding()
    test_python_runner()
    test_accept_writes_values_back()
    test_copy_explored_values_to_source()
    test_cross_check_matching()
    test_cross_check()
    test_cases_reproduce()
    test_handoff_payload()
    test_all_cases_reproduce()
    test_request_shape_per_model()
    test_recommendation_kinds_and_cleaning()
    test_apply_recommendation_kinds()
    test_solver_switch_diff_is_not_noisy()
    test_follow_up_message_assembly()
    test_wind_up_forces_a_report()
    test_semicolon_is_a_separator_not_a_terminator()
    test_detail_levels()
    test_worker_sandbox()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("all checks passed")
