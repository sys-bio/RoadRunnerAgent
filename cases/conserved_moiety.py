"""Expected behaviour: a conserved moiety the modeller has forgotten about.

Verified: S1 + S2 = 10 exactly for all time; S1 plateaus at 2.0, S2 at 8.0,
which is the k2/k1 ratio applied to the conserved total.
"""

NAME = "conserved_moiety"

MODEL = """\
// interconversion between two forms
J1: S1 -> S2; k1*S1;
J2: S2 -> S1; k2*S2;

k1 = 0.6;
k2 = 0.15;

S1 = 10;
S2 = 0;
"""

SIMULATION = (0, 60, 300)

QUESTION = """\
S1 is supposed to be consumed but it sticks at 2.0 and never gets any lower,
no matter how long I run it. I've increased k1 and it still plateaus, just at
a different value. Why won't it go to zero?
"""

GROUND_TRUTH = {
    "classification": "expected",
    "cause": (
        "S1 and S2 form a conserved moiety: S1 + S2 = 10 for all time, since "
        "the only reactions interconvert them. The system reaches equilibrium "
        "where k1*S1 = k2*S2, giving S1 = 10*k2/(k1+k2) = 2.0. Raising k1 "
        "moves the ratio but can never drive S1 to zero, because there is no "
        "reaction that removes mass from the pair."
    ),
    "good_fix": (
        "None needed - the model is correct. If the modeller wants S1 "
        "depleted, the model needs a reaction that actually removes it, e.g. "
        "S2 -> or S1 ->."
    ),
    "traps": (
        "There is no bug. The temptation is to treat 'sticks at 2.0' as a "
        "convergence or tolerance problem. A good report states the "
        "conservation law, derives 2.0 from k2/(k1+k2), and says the model is "
        "behaving correctly."
    ),
}
