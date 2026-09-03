"""Control: the run is simply too short. The dull answer is the right one.

Verified: k1 = 0.02 gives a relaxation time of 50, so at t = 10 S1 is 9.06
against an analytic steady state of vo/k1 = 50. By t = 500 it is 49.998.
"""

NAME = "run_too_short"

MODEL = """\
J1: -> S1;   vo;
J2: S1 -> ;  k1*S1;

vo = 1;
k1 = 0.02;

S1 = 0;
"""

SIMULATION = (0, 10, 200)

QUESTION = """\
This should settle to a steady state but it's still climbing at the end of
the run and shows no sign of levelling off. I've tried tightening the
tolerances and it makes no difference. Is the integrator losing accuracy on
this?
"""

GROUND_TRUTH = {
    "classification": "expected",
    "cause": (
        "The run is too short. The relaxation time is 1/k1 = 50, and the user "
        "is simulating to t = 10 - a fifth of one time constant. S1 is at "
        "9.06 on its way to vo/k1 = 50. The integrator is fine."
    ),
    "good_fix": "Simulate to t = 300 or so. Nothing in the model changes.",
    "traps": (
        "This is the anti-overfitting control. The correct answer is dull - "
        "'run it for longer' - and an agent with a diagnostic repertoire is "
        "tempted to find something more interesting. Adjusting tolerances, "
        "switching integrator, or declaring stiffness are all failures here. "
        "The analytic steady state vo/k1 = 50 is one line of algebra and "
        "settles it immediately."
    ),
}
