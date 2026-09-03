"""Evaluation cases.

Each case module defines:

    NAME            short identifier
    MODEL           Antimony source, as a user would write it
    QUESTION        the question phrased as the user would phrase it
    SIMULATION      (start, end, points) the user ran before asking
    GROUND_TRUTH    what is actually wrong, the correct classification, and
                    what a good fix looks like.  Never shown to the agent.

All eight cases of spec section 9.1 are present, including the three
controls.  Each has been verified to reproduce its documented symptom and to
be repaired by its documented fix (see test_milestone1.py).
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def load(name: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{name}")


def available() -> list[str]:
    return sorted(
        m.name for m in pkgutil.iter_modules(__path__)
        if not m.name.startswith("_")
    )
