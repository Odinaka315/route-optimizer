"""One command that runs the API and the worker together (for hosts like Render where
you want a single service):

    python -m app.serve

It starts ``uvicorn`` (bound to $PORT) and ``python -m app.worker`` as child processes
and forces RUN_MODE=worker so the API only queues runs. If either child dies the other
is stopped and this process exits non-zero, so the host restarts the whole service
instead of leaving you with an API that quietly has no worker. SIGTERM/Ctrl+C stops both
gracefully (the worker gets time to finish or hand back its runs).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

POLL_S = 0.5


def build_commands(env: Mapping[str, str]) -> list[list[str]]:
    api = [
        sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
        "--host", env.get("HOST", "0.0.0.0"), "--port", env.get("PORT", "8000"),
    ]
    worker = [sys.executable, "-m", "app.worker"]
    return [api, worker]


def _stop(processes: Sequence[subprocess.Popen], grace_s: float) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + grace_s
    for proc in processes:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def supervise(
    commands: Sequence[Sequence[str]],
    *,
    env: Mapping[str, str] | None = None,
    grace_s: float = 40.0,
    poll_s: float = POLL_S,
) -> int:
    """Run the commands until a signal arrives or one exits. Returns the exit code
    for this process: 0 after a requested stop, otherwise non-zero."""
    stop_requested = False

    def request_stop(signum, frame):  # noqa: ARG001
        nonlocal stop_requested
        stop_requested = True

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    processes = [subprocess.Popen(list(cmd), env=dict(env) if env is not None else None) for cmd in commands]
    exit_code = 0
    try:
        while not stop_requested:
            for proc in processes:
                code = proc.poll()
                if code is not None:
                    print(f"[serve] {' '.join(proc.args[:3])} exited with code {code}; stopping the rest", flush=True)
                    exit_code = code or 1  # even a clean exit is unexpected for a server
                    return exit_code
            time.sleep(poll_s)
        print("[serve] stop requested, shutting down", flush=True)
        return 0
    finally:
        _stop(processes, grace_s)


def main() -> None:
    env = dict(os.environ)
    env["RUN_MODE"] = "worker"  # the API must only queue; the worker solves
    grace = float(env.get("WORKER_SHUTDOWN_GRACE_S", "30")) + 10
    sys.exit(supervise(build_commands(env), env=env, grace_s=grace))


if __name__ == "__main__":
    main()
