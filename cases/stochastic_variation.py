"""Control: run-to-run variation in a stochastic simulation is the point.

Verified with the Gillespie integrator: final S1 across seeds 1, 2, 3 is
20, 22, 25 for the same model and the same settings.
"""

NAME = "stochastic_variation"

MODEL = """\
J1: -> S1;   vo;
J2: S1 -> ;  k1*S1;

vo = 10;
k1 = 0.5;

S1 = 20;
"""

SIMULATION = (0, 20, 200)


def SETUP(session):
    """The user has switched to stochastic simulation."""
    session.rr.setIntegrator("gillespie")


QUESTION = """\
Every time I run this I get a different answer - 20, then 22, then 25. The
model hasn't changed between runs. Something must be uninitialised, or the
solver is picking up garbage. How do I make it reproducible?
"""

GROUND_TRUTH = {
    "classification": "expected",
    "cause": (
        "The session is using the Gillespie integrator. Run-to-run variation "
        "is what a stochastic simulation is for - the trajectory is a sample "
        "path, not a solution. Nothing is uninitialised. The mean is "
        "vo/k1 = 20, and the observed values scatter around it."
    ),
    "good_fix": (
        "For a reproducible trajectory, fix the seed: "
        "rr.integrator.setValue('seed', n). For a reproducible *answer*, "
        "average many runs, or use CVODE if the deterministic solution is "
        "what is actually wanted."
    ),
    "traps": (
        "The user's framing ('uninitialised', 'garbage') invites the agent to "
        "hunt for a bug. A good report notices the integrator is gillespie "
        "before anything else, and distinguishes seeding one trajectory from "
        "averaging an ensemble."
    ),
}
