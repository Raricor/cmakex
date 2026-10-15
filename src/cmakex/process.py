"""Child processes that can be stopped together with what they started.

Each child gets its own session (POSIX), so stopping it also stops the
processes below it: make/ninja and the compilers under ``cmake --build``.
Running children are registered so the interactive UI can stop them all
when it quits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Set

_running: Set[subprocess.Popen] = set()
_lock = threading.Lock()


def spawn(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.Popen:
    """Start *cmd* with stdout and stderr merged into one text pipe.

    Pass the result to finish() once its output has been read.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # Ignored on Windows
    )
    with _lock:
        _running.add(proc)
    return proc


def finish(proc: subprocess.Popen) -> int:
    """Wait for *proc* and return its exit code."""
    try:
        return proc.wait()
    finally:
        with _lock:
            _running.discard(proc)


def stop(proc: subprocess.Popen, force: bool = False) -> None:
    """Stop *proc* and the processes it started."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        pass  # Already gone


def stop_all(timeout: float = 3.0) -> None:
    """Stop every running child; kill those still running after *timeout*."""
    with _lock:
        procs = list(_running)
    for proc in procs:
        stop(proc)
    deadline = time.monotonic() + timeout
    for proc in procs:
        try:
            proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stop(proc, force=True)
