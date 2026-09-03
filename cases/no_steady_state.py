"""Parametric: input flux exceeds the capacity of a substrate-inhibited step.

The interesting property of this case is that the error message points at
the solver ("Jacobian matrix singular in NLEQ") while the real answer is that
no steady state exists.

Verified: J2's rate peaks at Vm*sqrt(Km*Ki)/(Km + sqrt(Km*Ki) + Ki) = 2.612
at S1 = 0.0707. With vo = 4.5 > 2.612, S1 grows without bound (224 at t=50,
899 at t=200). steadyState() converges for vo <= 2.6 and fails from 2.7.
"""

NAME = "no_steady_state"

MODEL = """\
// Substrate inhibition on the first step
J1: -> S1;      vo;
J2: S1 -> S2;   (Vm*S1)/(Km + S1 + (S1*S1)/Ki);
J3: S2 -> ;     k*S2;

vo = 4.5;
Vm = 10;
Km = 0.1;
Ki = 0.05;
k  = 1;

S1 = 0;
S2 = 0;
"""

SIMULATION = (0, 200, 500)

QUESTION = """\
steadyState() gives me

    Jacobian matrix singular in NLEQ. Failed to converge to steady state.

I've tried allow_presimulation and it fails there too. Is nleq2 just not up
to this model, or do I need to switch solver?
"""

GROUND_TRUTH = {
    "classification": "parametric",
    "cause": (
        "There is no steady state to find. J2 is substrate-inhibited, so its "
        "rate peaks at 2.612 (at S1 = 0.0707) and falls away again. With "
        "vo = 4.5 the input flux permanently exceeds the maximum the pathway "
        "can carry, so S1 grows without bound - 899 by t=200. The solver is "
        "reporting a real property of the model, not failing at its job."
    ),
    "good_fix": (
        "Lower vo below 2.61 (2.5 converges to S1 = 0.05, S2 = 2.5), or raise "
        "Ki / Vm so the inhibited step can carry the flux. No solver change "
        "helps."
    ),
    "traps": (
        "Both options the user offers - a better solver, a different method - "
        "are wrong, and the error message actively encourages them. The "
        "decisive evidence is simulating and seeing S1 diverge, or computing "
        "the maximum of the rate law and comparing it with vo. An agent that "
        "switches solvers and reports success has failed this case."
    ),
}
