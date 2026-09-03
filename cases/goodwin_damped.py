"""Parametric: a Goodwin oscillator sitting below its Hopf bifurcation.

Verified against roadrunner 2.9.1 for this parameterisation (v0=8, K=1,
k1=k2=k3=1), sweeping the Hill coefficient:

    n=8    tail amplitude 4.9e-04    max Re(eig) -0.0544
    n=9    tail amplitude 9.8e-02    max Re(eig) -0.0153
    n=10   tail amplitude 1.08       max Re(eig) +0.0209
    n=12   tail amplitude 2.05       max Re(eig) +0.0865

The Hopf bifurcation is at n* = 9.414 (located by brentq on the
dominant eigenvalue's real part - found by the agent itself on the first
real run, sharper than the integer bracket this case originally claimed).  At the shipped n=8 the
oscillation is damped, which is exactly what the user is seeing.
"""

NAME = "goodwin_damped"

MODEL = """\
// Goodwin oscillator: S3 represses its own production
J1: -> S1;    v0/(1 + (S3/K)^n);
J2: S1 -> S2; k1*S1;
J3: S2 -> S3; k2*S2;
J4: S3 -> ;   k3*S3;

v0 = 8;
K  = 1;
n  = 8;
k1 = 1;
k2 = 1;
k3 = 1;

S1 = 0.1;
S2 = 0.2;
S3 = 0.3;
"""

SIMULATION = (0, 100, 1000)

QUESTION = """\
This is meant to be a Goodwin oscillator and it just decays to a steady
state. I get a couple of wobbles at the start and then it flatlines. Is
something wrong with how I've written the negative feedback, or is the
integrator damping it out?
"""

GROUND_TRUTH = {
    "classification": "parametric",
    "cause": (
        "The model is written correctly and the integrator is fine. At n=8 "
        "the fixed point is stable - the Jacobian's dominant eigenvalue pair "
        "is -0.054 +/- 1.03i, so the system spirals in. The Hopf bifurcation "
        "for this parameterisation is between n=9 and n=10."
    ),
    "good_fix": (
        "Raise n to 10 or above (12 gives a robust limit cycle of amplitude "
        "~2). Alternatively move any other parameter across the same "
        "bifurcation, but n is what the user controls here."
    ),
    "traps": (
        "The user offers two wrong hypotheses - a mis-written rate law and "
        "integrator damping - and a good report should dismiss both, with "
        "evidence, before giving the real answer. Tightening tolerances is "
        "the tempting wrong fix; it changes nothing. An eigenvalue argument "
        "is the convincing evidence; a parameter scan alone is weaker."
    ),
}
