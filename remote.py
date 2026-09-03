"""Host-side handle on a `worker.py` subprocess.

`WorkerSession` presents the parts of `Session` the GUI and the agent loop
actually use, but every call crosses a pipe into a process started with an
allowlisted environment. Agent code therefore runs where no API key exists.

Why an allowlist and not a denylist: a denylist has to anticipate every name
worth stealing, and the one it forgets is the one that matters. The worker
gets what an interpreter needs to start and load a native library, and
nothing else - `test_milestone1.py` asserts a key set in the parent is not
visible in the child.

What this does NOT do: sandbox the filesystem, the network, or CPU and
memory. Agent code in the worker can still read files the host user can read
and open sockets. Per-session containers are the only real answer to those,
and Streamlit Community Cloud cannot provide them - one container serves
every viewer. See CLAUDE.md, "What blocks hosting the agent".
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent

#: Environment the worker is allowed to see. Enough to start an interpreter
#: and let a native extension find its libraries; no credentials, no user
#: paths beyond a scratch directory.
_ENVIRONMENT_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "LD_LIBRARY_PATH",
)

DEFAULT_TIMEOUT = 120.0

#: Starting the worker means importing tellurium, which takes seconds even on
#: a warm cache. It is deliberately not the per-call timeout: a caller that
#: wants a short leash on agent code must not thereby make the worker
#: unstartable.
STARTUP_TIMEOUT = 120.0


class WorkerCrashed(RuntimeError):
    """The worker died - a native crash, a kill, or a hang we gave up on."""


class WorkerSession:
    """A `Session` living in another process."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._source_antimony = ""
        self._home = ""
        self.last_traceback = ""
        # Deliberately not started here. Starting means importing tellurium,
        # which takes seconds; a GUI that builds one of these per browser
        # session should not pay for it until a model is actually loaded.

    # ------------------------------------------------------------ lifecycle

    def _environment(self) -> dict[str, str]:
        """The allowlist, plus a scratch home the worker cannot escape into.

        tellurium's import chain reaches IPython -> jedi -> parso, which
        insists on a home directory and raises "Could not determine home
        directory" without one. Handing over the real home would point agent
        code straight at `~/.aws`, `~/.ssh` and the rest, so it gets an empty
        directory of its own instead - enough for parso and matplotlib to
        write their caches, and nothing worth reading.
        """
        env = {name: os.environ[name] for name in _ENVIRONMENT_ALLOWLIST
               if name in os.environ}
        self._home = tempfile.mkdtemp(prefix="rr-worker-")
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                     "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "MPLCONFIGDIR"):
            env[name] = self._home
        env["MPLBACKEND"] = "Agg"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def start(self) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(HERE), env=self._environment(),
            text=True, encoding="utf-8", bufsize=1,
        )
        threading.Thread(target=self._pump,
                         args=(self._process.stdout, self._lines),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # The worker announces itself once its imports are done.
        ready = self._receive(timeout=STARTUP_TIMEOUT)
        if ready != "ready":
            raise WorkerCrashed("worker did not come up: " + repr(ready))

    @staticmethod
    def _pump(stream, sink: queue.Queue) -> None:
        for line in stream:
            sink.put(line)
        sink.put(None)  # EOF: the process is gone

    def _drain_stderr(self) -> None:
        """Keep the pipe empty; a full stderr buffer would deadlock the child."""
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-200]

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            self._discard_home()   # a worker that already died still left one
            return
        try:
            process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
            process.stdin.flush()
            process.wait(timeout=5)
        except Exception:
            process.kill()
        finally:
            self._discard_home()

    def _discard_home(self) -> None:
        if self._home:
            shutil.rmtree(self._home, ignore_errors=True)
            self._home = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def kill(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
        self._discard_home()

    # ------------------------------------------------------------- protocol

    def _diagnosis(self) -> str:
        """Explain a death in terms a person can act on."""
        code = None
        if self._process is not None:
            # Reap it first: straight after EOF the exit status is often not
            # collected yet, and poll() would report None instead of the
            # signal that is the whole point of this message.
            try:
                code = self._process.wait(timeout=5)
            except Exception:
                code = self._process.poll()
        signals = {-11: "SIGSEGV (segmentation fault)", -6: "SIGABRT",
                   -9: "SIGKILL (out of memory?)", -4: "SIGILL", -8: "SIGFPE"}
        how = signals.get(code, "exit code " + str(code))
        tail = "\n".join(self._stderr[-15:])
        return "the worker process died - " + how + ("\n" + tail if tail else "")

    def _receive(self, timeout: float | None = None) -> Any:
        wait = timeout or self.timeout
        try:
            line = self._lines.get(timeout=wait)
        except queue.Empty:
            self.kill()
            raise WorkerCrashed(
                "the worker did not answer within %.0fs and was killed" % wait
            ) from None
        if line is None:
            raise WorkerCrashed(self._diagnosis())
        reply = json.loads(line)
        if not reply.get("ok"):
            #: Kept for debugging; the exception carries only the summary.
            self.last_traceback = reply.get("traceback", "")
            raise RuntimeError(reply.get("error", "unknown worker error"))
        return reply.get("value")

    def _request(self, op: str, timeout: float | None = None,
                 **args: Any) -> Any:
        if self._process is None:
            self.start()
        elif self._process.poll() is not None:
            raise WorkerCrashed(self._diagnosis())
        try:
            self._process.stdin.write(
                json.dumps({"op": op, "args": args}) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise WorkerCrashed(self._diagnosis()) from None
        return self._receive(timeout)

    # ---------------------------------------------------------- the session

    def run(self, code: str) -> tuple[str, bool]:
        """Execute agent code. Matches `PythonRunner.run`'s contract.

        A crash is an ordinary error result, not an exception: the agent is
        told what happened and can try something else, exactly as it would
        after a traceback. The worker is restarted so the run can continue,
        which costs the agent its namespace - hence the warning text.
        """
        try:
            output, is_error = self._request("exec", code=code)
            return output, is_error
        except WorkerCrashed as crash:
            self.restart()
            return (str(crash) + "\n\nThe sandbox was restarted, so `rr` and "
                    "every variable you defined are gone. The model has been "
                    "loaded again from the user's source; redo anything "
                    "else you need."), True

    def restart(self) -> None:
        self.kill()
        self._lines = queue.Queue()
        self._stderr = []
        source = self._source_antimony
        self.start()
        if source:
            try:
                self._request("call", name="load", args=[source], kwargs={})
            except Exception:
                pass

    def load(self, antimony_text: str):
        self._request("call", name="load", args=[antimony_text], kwargs={})
        self._source_antimony = antimony_text
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        """Send Session's own attributes across; keep our own here.

        Without this, `session.last_sim = None` - which the GUI does when a
        simulation fails - would quietly create an attribute on the proxy
        that shadows the worker's, and the two would disagree from then on.
        """
        if name in _SESSION_ATTRIBUTES:
            self._request("setattr", name=name, value=value)
        else:
            object.__setattr__(self, name, value)

    def apply_recommendation(self, change: dict) -> str:
        """Apply one report recommendation inside the worker."""
        return self._request("recommend", change=change)

    def __getattr__(self, name: str) -> Any:
        """Forward Session's own methods and attributes.

        Only reached for names not defined above, so the explicit ones win.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _SESSION_ATTRIBUTES:
            return _rehydrate(self._request("attr", name=name))
        if name in _SESSION_METHODS:
            def call(*args, **kwargs):
                return _rehydrate(self._request(
                    "call", name=name, args=list(args), kwargs=kwargs))
            return call
        raise AttributeError(name)

    @property
    def rr(self):
        return RemoteModel(self)


#: Plain attributes the GUI and the agent loop read off a Session.
_SESSION_ATTRIBUTES = frozenset({
    "source_antimony", "loaded", "model_edited", "change_log",
    "handoff_snapshot", "last_sim", "last_result",
})

#: Methods worth exposing across the boundary. `rr` is deliberately absent:
#: a live RoadRunner object cannot cross a pipe, so callers get the facade
#: below instead.
_SESSION_METHODS = frozenset({
    "apply_model_edit", "model_antimony", "simulate", "capture_state",
    "snapshot", "diff_since_snapshot", "revert", "values_into_source",
    "source_values", "write_values_to_source", "accept",
})


def _rehydrate(value: Any) -> Any:
    """Turn a simulation result back into something with `.colnames`."""
    if isinstance(value, dict) and value.get("__result__"):
        return SimulationResult(value["colnames"], value["data"])
    return value


class SimulationResult(np.ndarray):
    """What `rr.simulate` returned, minus RoadRunner's own class.

    A NamedArray cannot cross a process boundary, and every caller here wants
    only the numbers and the column names.
    """

    def __new__(cls, colnames, data):
        obj = np.asarray(data, dtype=float).view(cls)
        obj.colnames = list(colnames)
        return obj

    def __array_finalize__(self, obj) -> None:
        if obj is not None:
            self.colnames = getattr(obj, "colnames", [])


class RemoteModel:
    """The slice of a RoadRunner instance callers touch from the host.

    Ids and single values, which is all the GUI needs to build its parameter
    fields and sliders. Anything richer belongs in agent code, which runs
    inside the worker where the real object lives.
    """

    _GETTERS = ("getFloatingSpeciesIds", "getGlobalParameterIds",
                "getBoundarySpeciesIds", "getCompartmentIds",
                "getFloatingSpeciesInitialConcentrationIds",
                "getReactionIds")

    def __init__(self, worker: WorkerSession) -> None:
        object.__setattr__(self, "_worker", worker)

    def __getattr__(self, name: str):
        if name in RemoteModel._GETTERS:
            return lambda: self._worker._request("ids", getter=name)
        if name.startswith("_"):
            raise AttributeError(name)
        return self._worker._request("get", ident=name)

    def __setattr__(self, name: str, value: Any) -> None:
        self._worker._request("set", ident=name, value=value)

    def __getitem__(self, ident: str) -> float:
        return self._worker._request("get", ident=ident)

    def __setitem__(self, ident: str, value: Any) -> None:
        self._worker._request("set", ident=ident, value=value)
