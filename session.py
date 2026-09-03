"""Session state for the libRoadRunner agent proof of concept.

Owns the single RoadRunner instance, the user's Antimony source, the handoff
snapshot and the change log.  See roadrunner-agent-spec-v2.md sections 3.1,
4.1 and 4.2 for why the awkward parts here are the way they are:

  * `rr` is a property, never a stored reference elsewhere, because a model
    edit means `te.loada` returns a *new* object.
  * `model_antimony()` resets before serialising and restores afterwards,
    because `getCurrentAntimony()` writes *current* values as initialisations.
  * `reset()` is used throughout, never `resetAll()`, which would also throw
    away parameter changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import tellurium as te

# Fixed-point set of ids we consider "state" for diffing purposes.
_SELECTOR_GROUPS = (
    ("parameter", "getGlobalParameterIds"),
    ("initial concentration", "getFloatingSpeciesInitialConcentrationIds"),
    ("boundary species", "getBoundarySpeciesIds"),
    ("compartment", "getCompartmentIds"),
)


@dataclass
class SimulationCall:
    """The last simulation the user (or agent) asked for."""

    start: float
    end: float
    points: int
    selections: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plain(value: Any) -> Any:
    """Make a solver setting JSON-safe and comparable."""
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


# `x = 1.5` - a value assignment, not model structure. In Antimony the
# semicolon is a *separator*, not a terminator: it is required only between
# statements on the same line, so `n = 8` alone on a line is valid and
# `k1 = 1; k2 = 1` is two assignments with no trailing semicolon. The pattern
# therefore matches one assignment without its separator, and a line is
# values-only when every part between semicolons is one.
_ASSIGNMENT = re.compile(r"^\s*\w+\s*=\s*[-+0-9.eE]+\s*$")


def _is_values_only(code: str) -> bool:
    """True when a line contains nothing but value assignments."""
    parts = [part for part in code.split(";") if part.strip()]
    return bool(parts) and all(_ASSIGNMENT.match(part) for part in parts)


def structural_antimony(text: str) -> str:
    """The model text with value assignments and comments removed.

    `getCurrentAntimony()` embeds current parameter and species values, so two
    texts differing only in a parameter the agent tuned are not a structural
    change - and reporting one as "model rewritten" would fire the report
    cross-check on every parameter change.  Values are diffed separately.
    """
    lines = []
    for line in text.splitlines():
        line = line.split("//")[0].rstrip()
        if not line.strip() or _is_values_only(line):
            continue
        lines.append(" ".join(line.split()))
    return "\n".join(lines)


def coerce(text: str):
    """A recommendation's value arrives as text; make it what RoadRunner wants."""
    raw = str(text).strip()
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    # Integer-valued settings (a Gillespie seed, maximum_num_steps) reject a
    # float: RoadRunner raises "bad variant access". Try int before float.
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def apply_recommendation(session, change) -> str:
    """Apply one recommendation. Returns a description; raises on failure."""
    kind = change.get("kind", "value")
    selector = (change.get("selector") or "").strip()
    value = change.get("value", "")

    if kind == "model_text":
        text = (change.get("model_text") or "").strip()
        if not text:
            raise ValueError("no model text given")
        session.apply_model_edit(text)
        return "corrected model loaded"

    if kind == "simulation":
        end = coerce(value)
        if isinstance(end, bool) or not isinstance(end, (int, float)) or end <= 0:
            raise ValueError(f"not a usable end time: {value!r}")
        return f"simulate to t = {float(end):g}"

    if kind == "solver":
        if selector in ("integrator", "steadyStateSolver"):
            name = str(value).strip()
            if not name:
                raise ValueError("no solver name given")
            if selector == "integrator":
                session.rr.setIntegrator(name)
            else:
                session.rr.setSteadyStateSolver(name)
            return f"{selector} = {name}"
        target, _, setting = selector.partition(".")
        if not setting:
            raise ValueError(f"not a solver setting: {selector!r}")
        solver = (session.rr.integrator if target == "integrator"
                  else session.rr.steadyStateSolver)
        solver.setValue(setting, coerce(value))
        return f"{selector} = {value}"

    if not selector:
        raise ValueError("no selector given")
    session.rr[selector] = float(coerce(value))
    return f"{selector} = {value}"


class NoModelLoaded(RuntimeError):
    pass


class Session:
    """One model, one RoadRunner instance, shared by the GUI and the agent."""

    def __init__(self) -> None:
        self._rr = None
        self.source_antimony: str = ""
        self.last_sim: SimulationCall | None = None
        self.last_result = None  # NamedArray from rr.simulate
        self.handoff_snapshot: dict[str, Any] | None = None
        self.change_log: list[tuple[str, Any, Any]] = []
        self.model_edited: bool = False

    # ---------------------------------------------------------------- model

    @property
    def rr(self):
        if self._rr is None:
            raise NoModelLoaded("no model has been loaded into the session")
        return self._rr

    @property
    def loaded(self) -> bool:
        return self._rr is not None

    def load(self, antimony_text: str):
        """User-facing load.  Resets the model of record and the change log."""
        self._rr = te.loada(antimony_text)
        self.source_antimony = antimony_text
        self.last_sim = None
        self.last_result = None
        self.change_log = []
        self.handoff_snapshot = None
        self.model_edited = False
        return self._rr

    def apply_model_edit(self, antimony_text: str):
        """Agent-facing model rewrite.

        Rebinds `rr` from new text but deliberately leaves `source_antimony`
        alone, so `revert()` still returns to what the user had.  This is the
        only supported way for agent code to change the model structure -
        calling `te.loada` directly leaves the session pointing at the old
        object (spec section 3.1).
        """
        self._rr = te.loada(antimony_text)
        self.model_edited = True
        return self._rr

    def model_antimony(self) -> str:
        """Serialise the *model*, not the current state.

        `getCurrentAntimony()` emits current species values as the
        initialisations, so reset first and put the live values back after.
        """
        rr = self.rr
        live = {s: rr[s] for s in rr.getFloatingSpeciesIds()}
        rr.reset()
        try:
            text = rr.getCurrentAntimony()
        finally:
            for species, value in live.items():
                rr[species] = value
        return text

    # ----------------------------------------------------------- simulation

    def simulate(self, start: float, end: float, points: int,
                 selections: list[str] | None = None):
        rr = self.rr
        rr.reset()
        if selections:
            rr.timeCourseSelections = selections
        result = rr.simulate(start, end, points)
        self.last_sim = SimulationCall(start, end, points,
                                       list(result.colnames))
        self.last_result = result
        return result

    # ---------------------------------------------------------------- state

    def capture_state(self) -> dict[str, Any]:
        """Everything the change log diffs over."""
        rr = self.rr
        state: dict[str, Any] = {"values": {}, "value_labels": {}}
        for label, getter in _SELECTOR_GROUPS:
            for ident in getattr(rr, getter)():
                state["values"][ident] = float(rr[ident])
                state["value_labels"][ident] = label

        integrator = rr.integrator
        state["integrator"] = integrator.getName()
        state["integrator_settings"] = {
            k: _plain(integrator.getValue(k)) for k in integrator.getSettings()
        }
        solver = rr.steadyStateSolver
        state["steady_state_solver"] = solver.getName()
        state["steady_state_settings"] = {
            k: _plain(solver.getValue(k)) for k in solver.getSettings()
        }
        state["conserved_moiety_analysis"] = bool(rr.conservedMoietyAnalysis)
        state["antimony"] = self.model_antimony()
        return state

    def snapshot(self) -> dict[str, Any]:
        """Capture the handoff state.  Called immediately before the agent runs."""
        snap = self.capture_state()
        snap["source_antimony"] = self.source_antimony
        snap["last_sim"] = self.last_sim.as_dict() if self.last_sim else None
        self.handoff_snapshot = snap
        self.model_edited = False
        return snap

    def diff_since_snapshot(self) -> list[tuple[str, Any, Any]]:
        """Net change between the handoff snapshot and now.

        Deliberately net, not a record of activity: an agent that changes a
        tolerance, tests it and puts it back leaves nothing here.  The
        transcript is where the user sees what was tried.
        """
        if self.handoff_snapshot is None:
            raise RuntimeError("no snapshot to diff against")
        before, after = self.handoff_snapshot, self.capture_state()
        log: list[tuple[str, Any, Any]] = []

        # Scalar values, tolerant of the id set changing under a model edit.
        b_vals, a_vals = before["values"], after["values"]
        labels = {**before["value_labels"], **after["value_labels"]}
        for ident in sorted(set(b_vals) | set(a_vals)):
            old, new = b_vals.get(ident, "<absent>"), a_vals.get(ident, "<absent>")
            if old != new:
                log.append((f"{labels[ident]} {ident}", old, new))

        for key in ("integrator", "steady_state_solver",
                    "conserved_moiety_analysis"):
            if before[key] != after[key]:
                log.append((key, before[key], after[key]))

        for group, label, name_key in (
                ("integrator_settings", "integrator", "integrator"),
                ("steady_state_settings", "steady state solver",
                 "steady_state_solver")):
            b_set, a_set = before[group], after[group]
            # A different solver has a different setting list, and different
            # defaults for the settings it shares. Diffing across a switch
            # reported a dozen entries for one change. The switch itself is
            # already logged; its settings are a consequence, not a separate
            # decision the user needs to see.
            keys = set() if before[name_key] != after[name_key]                 else set(b_set) | set(a_set)
            for key in sorted(keys):
                old, new = b_set.get(key, "<absent>"), a_set.get(key, "<absent>")
                if old != new:
                    log.append((f"{label} {key}", old, new))

        # Structure only - a changed parameter value is already logged above.
        if (structural_antimony(before["antimony"])
                != structural_antimony(after["antimony"])):
            log.append(("model text", "<see handoff snapshot>", "<rewritten>"))

        self.change_log = log
        return log

    def revert(self) -> None:
        """Back to the state at handoff: the user's own text plus solver settings."""
        if self.handoff_snapshot is None:
            raise RuntimeError("no snapshot to revert to")
        snap = self.handoff_snapshot
        self._rr = te.loada(snap["source_antimony"])
        rr = self._rr

        rr.setIntegrator(snap["integrator"])
        for key, value in snap["integrator_settings"].items():
            try:
                rr.integrator.setValue(key, value)
            except Exception:  # a setting the rebuilt integrator does not take
                pass
        rr.setSteadyStateSolver(snap["steady_state_solver"])
        for key, value in snap["steady_state_settings"].items():
            try:
                rr.steadyStateSolver.setValue(key, value)
            except Exception:
                pass
        rr.conservedMoietyAnalysis = snap["conserved_moiety_analysis"]

        for ident, value in snap["values"].items():
            try:
                rr[ident] = value
            except Exception:  # id no longer exists in the reloaded model
                pass

        self.change_log = []
        self.model_edited = False

    def values_into_source(
            self, reference: dict[str, float] | None = None
    ) -> tuple[str, list[str], list[str]]:
        """Write the live parameter and initial values back into the user's text.

        Returns (new_text, applied, missing).  Substituting in place is ugly
        but it is the only way to keep the user's comments and layout - a
        round trip through `getCurrentAntimony()` discards both (spec 4.1).

        Lines carrying events (`at (...)`), assignment rules (`:=`) and rate
        rules (`S'=`) are skipped: an assignment inside an event body is not
        an initial value and must not be rewritten.
        """
        rr = self.rr
        wanted: dict[str, float] = {}
        for _label, getter in _SELECTOR_GROUPS:
            for ident in getattr(rr, getter)():
                value = float(rr[ident])
                # Only write back what the agent actually changed - otherwise
                # accepting would reformat every untouched number in the file,
                # and implicit ids (default_compartment) would be reported
                # missing despite being irrelevant.
                if reference is not None and reference.get(ident) == value:
                    continue
                # 'init([S1])' is written as 'S1 = ...' in the user's source.
                name = (ident[len("init(["):-len("])")]
                        if ident.startswith("init([") else ident)
                wanted[name] = value

        applied: list[str] = []
        out_lines = list(self.source_antimony.splitlines())
        for name, value in wanted.items():
            pattern = re.compile(rf"(\b{re.escape(name)}\s*=\s*)([-+0-9.eE]+)")
            for index, line in enumerate(out_lines):
                code = line.split("//")[0]
                if "at (" in code or ":=" in code or "'" in code:
                    continue
                match = pattern.search(code)
                if not match:
                    continue
                if float(match.group(2)) == value:
                    break  # already correct
                out_lines[index] = (
                    line[:match.start(2)] + f"{value:g}" + line[match.end(2):])
                applied.append(f"{name} = {value:g}")
                break

        applied_names = {entry.split(" =")[0] for entry in applied}
        missing = [
            f"{name} = {value:g}" for name, value in wanted.items()
            if name not in applied_names
            and not re.search(rf"(\b{re.escape(name)}\s*=\s*)([-+0-9.eE]+)",
                              self.source_antimony)
        ]
        return "\n".join(out_lines), applied, missing

    def source_values(self) -> dict[str, float]:
        """The values a fresh load of `source_antimony` would give.

        Used as the reference when writing explored values back: without it,
        every untouched number in the file gets reformatted and RoadRunner's
        implicit ids are reported as unwritable.
        """
        if not self.source_antimony.strip():
            return {}
        probe = te.loada(self.source_antimony)
        values: dict[str, float] = {}
        for _label, getter in _SELECTOR_GROUPS:
            for ident in getattr(probe, getter)():
                values[ident] = float(probe[ident])
        return values

    def write_values_to_source(self) -> tuple[str, list[str], list[str]]:
        """Copy the live values into the user's model text.

        The Explore sliders change the live model only. This makes what the
        user arrived at by dragging permanent, in their own text, with their
        comments and layout intact - the same substitution `accept()` uses,
        but referenced against the source rather than a handoff snapshot, so
        it works with no agent involved.
        """
        text, applied, missing = self.values_into_source(self.source_values())
        self.source_antimony = text
        return text, applied, missing

    def accept(self) -> tuple[list[str], list[str]]:
        """Adopt the agent's version as the new model of record.

        A structural rewrite replaces the source outright (losing the user's
        comments - the caller should say so).  Otherwise the live values are
        substituted into the user's own text, so what the editor shows and
        what RoadRunner holds cannot drift apart.

        Returns (applied, missing) describing the value substitutions.
        """
        applied: list[str] = []
        missing: list[str] = []
        if self.model_edited:
            self.source_antimony = self.model_antimony()
        else:
            reference = (self.handoff_snapshot or {}).get("values")
            self.source_antimony, applied, missing = \
                self.values_into_source(reference)
        # Keep the snapshot: accepting may be a no-op the user follows with
        # "Try it" and a second accept, which still needs the reference to
        # tell a real change from an untouched implicit id. It is replaced at
        # the next handoff and cleared on load().
        self.change_log = []
        self.model_edited = False
        return applied, missing
