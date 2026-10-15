"""Tests for stopping child processes together with what they started."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from cmakex import process

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="checks process state in /proc"
)


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"  # Zombies are dead


def test_stop_all_stops_grandchildren():
    proc = process.spawn(["sh", "-c", "sleep 30 & echo $!; wait"])
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    assert alive(grandchild)

    start = time.monotonic()
    process.stop_all()
    assert process.finish(proc) != 0
    assert time.monotonic() - start < 5
    for _ in range(50):
        if not alive(grandchild):
            break
        time.sleep(0.05)
    assert not alive(grandchild)
    assert not process._running


def test_stop_all_kills_what_ignores_sigterm():
    proc = process.spawn(["sh", "-c", "trap '' TERM; echo ready; sleep 30"])
    assert proc.stdout is not None
    proc.stdout.readline()
    start = time.monotonic()
    process.stop_all(timeout=0.3)
    process.finish(proc)
    assert time.monotonic() - start < 5
