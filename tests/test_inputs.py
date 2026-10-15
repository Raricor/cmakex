"""Tests for MD5 input snapshots."""

from __future__ import annotations

import os

from cmakex.inputs import InputSnapshot, describe_changes


def test_content_changes_are_reported(tmp_path):
    a, b, c = tmp_path / "CMakeLists.txt", tmp_path / "sub.cmake", tmp_path / "new.cmake"
    a.write_text("project(x)\n")
    b.write_text("set(A 1)\n")
    snap = InputSnapshot.capture([a, b, c])  # c does not exist yet

    assert snap.changes() == []

    a.write_text("project(y)\n")
    b.unlink()
    c.write_text("set(B 2)\n")

    assert [(ch.path.name, ch.kind) for ch in snap.changes()] == [
        ("CMakeLists.txt", "modified"),
        ("sub.cmake", "removed"),
        ("new.cmake", "added"),
    ]


def test_touch_or_identical_save_is_not_a_change(tmp_path):
    f = tmp_path / "CMakeLists.txt"
    f.write_text("project(x)\n")
    os.utime(f, (1000, 1000))
    snap = InputSnapshot.capture([f])
    assert snap.files[f].mtime_ns == 1000 * 10**9

    os.utime(f, (2000, 2000))  # Touched, content unchanged

    assert snap.changes() == []
    # The new timestamp is remembered, so the file is not hashed again.
    assert snap.files[f].mtime_ns == 2000 * 10**9


def test_same_size_edit_within_one_timestamp_tick_is_detected(tmp_path):
    f = tmp_path / "CMakeLists.txt"
    f.write_text("option(A ON)\n")
    snap = InputSnapshot.capture([f])
    # Just written: the timestamp is not trusted.
    assert snap.files[f].mtime_ns == 0

    f.write_text("option(A NO)\n")  # Same size, possibly the same mtime

    assert [ch.kind for ch in snap.changes()] == ["modified"]


def test_save_and_load_round_trip(tmp_path):
    f = tmp_path / "CMakeLists.txt"
    f.write_text("project(x)\n")
    snap = InputSnapshot.capture([f, tmp_path / "missing.cmake"], reply="index-1.json")
    path = tmp_path / "snap.json"

    snap.save(path)

    assert InputSnapshot.load(path) == snap


def test_load_rejects_garbage(tmp_path):
    path = tmp_path / "snap.json"
    assert InputSnapshot.load(path) is None
    path.write_text("not json")
    assert InputSnapshot.load(path) is None
    path.write_text('{"version": 1, "files": {"a": "oops"}}')
    assert InputSnapshot.load(path) is None


def test_describe_changes(tmp_path):
    files = [tmp_path / n for n in ("CMakeLists.txt", "a.cmake", "b.cmake", "c.cmake")]
    for f in files:
        f.write_text("x\n")
    snap = InputSnapshot.capture(files)
    files[0].write_text("y\n")
    files[1].unlink()
    for f in files[2:]:
        f.write_text("z\n")

    message = describe_changes(snap.changes(), tmp_path)

    assert message == (
        "4 CMake files changed: CMakeLists.txt, a.cmake (removed), b.cmake and 1 more"
    )
