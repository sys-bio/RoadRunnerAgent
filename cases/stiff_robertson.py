"""Numerical: a stiff model the solver cannot integrate as configured.

Verified against roadrunner 2.9.1: Robertson's problem integrates fine with
the default CVODE settings, and fails with CV_TOO_MUCH_WORK once `stiff` is
turned off.  The failure is therefore in the session's solver configuration,
not in the model text - which is the discrimination this case tests.
"""

NAME = "stiff_robertson"

MODEL = """\
// Robertson's problem
J1: A -> B;         k1*A;
J2: B + B -> C + B; k2*B*B;
J3: B + C -> A + C; k3*B*C;

k1 = 0.04;
k2 = 3e7;
k3 = 1e4;

A = 1;
B = 0;
C = 0;
"""

SIMULATION = (0, 1e5, 200)


def SETUP(session):
    """The user has, at some point, turned the stiff solver off."""
    session.rr.integrator.setValue("stiff", False)


QUESTION = """\
This won't run any more. I get

    CVODE Error: CV_TOO_MUCH_WORK: The solver took mxstep (20000) internal
    steps but could not reach tout.

I haven't touched the model. Raising maximum_num_steps just makes it take
longer before it fails. What's wrong?
"""

GROUND_TRUTH = {
    "classification": "numerical",
    "cause": (
        "The model is stiff (rate constants spanning 0.04 to 3e7). CVODE's "
        "`stiff` setting is False in the session, so it is running the "
        "non-stiff Adams method, which cannot make progress. The model text "
        "is correct and unchanged."
    ),
    "good_fix": (
        "rr.integrator.setValue('stiff', True). Raising maximum_num_steps is "
        "explicitly the wrong fix and the user has already said it does not "
        "help."
    ),
    "traps": (
        "Should not conclude the model is wrong, and should not 'fix' it by "
        "loosening tolerances - that may make it run but for the wrong "
        "reason. Should notice the timescale separation rather than only "
        "reading the error text."
    ),
}
