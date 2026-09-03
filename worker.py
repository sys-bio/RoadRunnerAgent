"""The child process that owns the model and runs the agent's code.

`run_python` executes whatever the model writes. In one process with the GUI
that is an unsandboxed `exec`: agent code can read `os.environ` and walk off
with every API key, and on a hosted deployment it would be a stranger's code
doing so. This module is the other half of that problem's answer - it holds
the Session and the exec namespace in a subprocess started with an empty
environment (see `remote.py`), so there are no credentials there to steal.

It closes credential theft only. Filesystem, network and CPU abuse need a
container per session, which no amount of Python arranges for itself.

Protocol: one JSON object per line in, one JSON object per line out.
Synchronous, one request at a time - the caller is an agent loop that has
nothing else to do while a simulation runs.

    -> {"op": "exec", "args": {"code": "print(rr.k1)"}}
    <- {"ok": true, "value": ["1.0\n", false]}

**fd 1 is not the protocol channel.** RoadRunner is a C++ library and writes
warnings straight to file descriptor 1, which would land in the middle of a
JSON line and desynchronise the stream. So the real stdout is duplicated to a
private descriptor for protocol use, and fd 1 is pointed at stderr, where
native chatter is harmless.
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from typing import Any


def _install_protocol_channel():
    """Return a writable stream on the real stdout, and free fd 1."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)  # native prints to fd 1 now go to stderr
    stream = os.fdopen(protocol_fd, "w", encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr  # and so does anything Python prints
    return stream


class Worker:
    """A Session plus the agent's persistent exec namespace."""

    def __init__(self) -> None:
        import matplotlib
        matplotlib.use("Agg")  # a blocking show() would hang this process
        import numpy as np
        import tellurium as te
        import antimony
        from session import Session

        np.set_printoptions(threshold=200, edgeitems=3, linewidth=100)
        self.session = Session()
        self.namespace: dict[str, Any] = {
            "__name__": "__agent__",
            "te": te,
            "antimony": antimony,
            "np": np,
            "session": self.session,
        }

    # ------------------------------------------------------------ the code

    def op_exec(self, code: str) -> tuple[str, bool]:
        """Run agent code; return (output, is_error). Never raises."""
        # Rebind rr every call: a model edit replaces the object (spec 3.1).
        if self.session.loaded:
            self.namespace["rr"] = self.session.rr

        buffer = io.StringIO()
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return traceback.format_exc(), True

        body, tail = tree.body, None
        if body and isinstance(body[-1], ast.Expr):
            body, tail = body[:-1], body[-1]

        is_error = False
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]),
                                 "<agent>", "exec"), self.namespace)
                if tail is not None:
                    value = eval(  # noqa: S307 - the whole point of this file
                        compile(ast.Expression(tail.value), "<agent>", "eval"),
                        self.namespace,
                    )
                    if value is not None:
                        print(repr(value), file=buffer)
        except BaseException:  # noqa: BLE001 - never raise into the loop
            buffer.write(traceback.format_exc())
            is_error = True

        output = buffer.getvalue()
        return (output if output.strip() else "(no output)"), is_error

    # ------------------------------------------- the session, over the wire

    def op_call(self, name: str, args: list, kwargs: dict) -> Any:
        """Invoke a Session method and return a JSON-safe result."""
        return _jsonable(getattr(self.session, name)(*args, **kwargs))

    def op_attr(self, name: str) -> Any:
        return _jsonable(getattr(self.session, name))

    def op_setattr(self, name: str, value: Any) -> None:
        setattr(self.session, name, value)

    def op_setup_case(self, name: str) -> bool:
        """Run an evaluation case's SETUP against this worker's session.

        Case setups reach `rr.setIntegrator` and `rr.integrator.setValue` -
        solver objects, which cannot cross the pipe - so the setup runs here.
        Cases are trusted project code, not agent input.
        """
        import cases
        case = cases.load(name)
        if hasattr(case, "SETUP"):
            case.SETUP(self.session)
            return True
        return False

    def op_recommend(self, change: dict) -> str:
        """Apply one recommendation here, where the live objects are.

        `apply_recommendation` reaches `rr.setIntegrator` and
        `rr.integrator.setValue` - solver objects that cannot cross a pipe -
        so the whole operation runs in this process rather than being taken
        apart into remote calls.
        """
        from session import apply_recommendation
        return apply_recommendation(self.session, change)

    def op_ids(self, getter: str) -> list[str]:
        return list(getattr(self.session.rr, getter)())

    def op_get(self, ident: str) -> float:
        return float(self.session.rr[ident])

    def op_set(self, ident: str, value: float) -> None:
        self.session.rr[ident] = float(value)

    def op_environment(self) -> dict[str, str]:
        """What this process can see. Used by the tests to prove it is bare."""
        return dict(os.environ)

    def op_ping(self) -> str:
        return "ok"


def _jsonable(value: Any) -> Any:
    """Convert RoadRunner and numpy results into something JSON can carry.

    Deliberately one-way and lossy. Nothing is ever pickled across this
    boundary: the worker runs untrusted code, and unpickling what it sends
    would hand that code the host process it was moved out of.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "colnames") and hasattr(value, "tolist"):  # NamedArray
        return {"__result__": True, "colnames": list(value.colnames),
                "data": value.tolist()}
    if hasattr(value, "tolist"):  # numpy scalar or array
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def main() -> int:
    protocol = _install_protocol_channel()

    def reply(payload: dict) -> None:
        protocol.write(json.dumps(payload) + "\n")
        protocol.flush()

    try:
        worker = Worker()
    except BaseException:
        reply({"ok": False, "error": "worker failed to start:\n"
                                     + traceback.format_exc()})
        return 1
    reply({"ok": True, "value": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            reply({"ok": False, "error": f"unreadable request: {exc}"})
            continue
        if request.get("op") == "shutdown":
            return 0
        handler = getattr(worker, "op_" + request.get("op", ""), None)
        if handler is None:
            reply({"ok": False, "error": f"no such op: {request.get('op')!r}"})
            continue
        try:
            reply({"ok": True, "value": handler(**request.get("args", {}))})
        except BaseException as exc:  # noqa: BLE001 - must not kill us
            # A one-line summary for the caller to show a person, and the
            # traceback alongside it for whoever is debugging. A UI toast
            # carrying forty lines of stack helps nobody.
            # The type travels separately from the message. Callers format
            # errors as f"{type(exc).__name__}: {exc}", and folding the type
            # into the message here would double it on the far side - which
            # then lands in the question the agent is asked.
            reply({"ok": False, "error": str(exc),
                   "error_type": type(exc).__name__,
                   "traceback": traceback.format_exc()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
