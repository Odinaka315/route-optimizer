"""The single-command launcher (python -m app.serve)."""

import os
import signal
import subprocess
import sys
import textwrap
import time

from app.serve import build_commands

SLEEPER = "import time; time.sleep(60)"


def test_commands_bind_the_host_port_and_start_both_processes():
    api, worker = build_commands({"PORT": "10000"})
    assert "uvicorn" in api and api[api.index("--port") + 1] == "10000"
    assert api[api.index("--host") + 1] == "0.0.0.0" and "--factory" in api
    assert worker[-2:] == ["-m", "app.worker"]
    assert build_commands({})[0][build_commands({})[0].index("--port") + 1] == "8000"


def run_supervisor(commands_src: str) -> subprocess.Popen:
    code = textwrap.dedent(
        f"""
        import sys
        from app.serve import supervise
        sys.exit(supervise({commands_src}, grace_s=5, poll_s=0.05))
        """
    )
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def pids_alive(pids) -> list[int]:
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            # A zombie still answers kill(0); check /proc when available.
            state = open(f"/proc/{pid}/stat").read().split()[2] if os.path.exists(f"/proc/{pid}/stat") else "R"
            if state != "Z":
                alive.append(pid)
        except OSError:
            pass
    return alive


def child_pids(parent: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out]


def test_one_child_dying_stops_the_other_and_fails_the_service():
    proc = run_supervisor(f"[[sys.executable, '-c', 'import sys; sys.exit(3)'], [sys.executable, '-c', {SLEEPER!r}]]")
    time.sleep(0.5)
    assert proc.wait(timeout=15) == 3
    assert "exited with code 3" in proc.stdout.read()


def test_a_clean_exit_is_still_treated_as_a_failure():
    proc = run_supervisor(f"[[sys.executable, '-c', 'pass'], [sys.executable, '-c', {SLEEPER!r}]]")
    assert proc.wait(timeout=15) == 1


def test_sigterm_stops_both_children_and_exits_cleanly():
    proc = run_supervisor(f"[[sys.executable, '-c', {SLEEPER!r}], [sys.executable, '-c', {SLEEPER!r}]]")
    for _ in range(100):
        kids = child_pids(proc.pid)
        if len(kids) == 2:
            break
        time.sleep(0.05)
    assert len(kids) == 2
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0
    assert pids_alive(kids) == []
