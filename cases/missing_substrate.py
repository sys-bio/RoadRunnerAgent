"""Structural: a bimolecular rate law missing one of its substrates.

Verified: B reaches -38 by t=50. Because J3 consumes B at a rate that does
not depend on B, nothing stops it going through zero.
"""

NAME = "missing_substrate"

MODEL = """\
// A and B combine to make C
J1: -> A;        v0;
J2: -> B;        v1;
J3: A + B -> C;  k1*A;

v0 = 1;
v1 = 0.2;
k1 = 0.5;

A = 1;
B = 1;
C = 0;
"""

SIMULATION = (0, 50, 500)

QUESTION = """\
B goes negative in this model, which obviously can't be right. I've checked
the stoichiometry of J3 and it looks fine to me. Is this a solver tolerance
problem?
"""

GROUND_TRUTH = {
    "classification": "structural",
    "cause": (
        "The rate law for J3 is `k1*A`, but the reaction consumes both A and "
        "B. Because the rate does not depend on B, consumption continues at "
        "the same rate once B reaches zero, and B goes negative (about -38 by "
        "t=50). Nothing to do with tolerances."
    ),
    "good_fix": "J3: A + B -> C; k1*A*B.",
    "traps": (
        "The user explicitly offers 'solver tolerance' as the explanation and "
        "says they have already checked the stoichiometry - which is indeed "
        "correct; it is the rate law that is wrong. Tightening tolerances "
        "changes nothing. A good report says why a mass-action rate law must "
        "contain every substrate."
    ),
}
