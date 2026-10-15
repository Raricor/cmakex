"""Collect project information from the best available source.

The CMake File API gives exact results: CMake itself evaluates every if(),
function and find_package(). cmakex reads it from the user's build directory
when that is configured, and otherwise configures a private analysis tree
under ``<build_dir>/.cmakex/`` (configure only, nothing is compiled).
Projects that cannot use it (CMake < 3.14, or a configure that fails, e.g.
because of a missing dependency) fall back to statically parsing
CMakeLists.txt.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import process
from .fileapi import MIN_CMAKE_VERSION, FileApiError, Reply, supports_file_api, write_query
from .inputs import SNAPSHOT_NAME, InputSnapshot, describe_changes, file_state
from .models import CMakeInfo
from .parser import parse_cmake_file

BACKENDS = ("auto", "file-api", "static")
ANALYSIS_DIR_NAME = ".cmakex"
PRESET_FILES = ("CMakePresets.json", "CMakeUserPresets.json")

_PRESET_LINE_RE = re.compile(r'^\s+"([^"]+)"')
_REQUIRED_OBJECTS = (("codemodel", 2), ("cache", 2), ("cmakeFiles", 1))

Callback = Callable[[str], None]


class IntrospectError(Exception):
    """The requested backend cannot provide project information."""


@dataclass(frozen=True)
class Profile:
    """Which build tree an analysis reads.

    ``build``: the user's own, already configured build directory.
    ``preset``: an analysis tree configured with configure preset *name*.
    ``default``: an analysis tree configured with CMake defaults (Debug).
    """

    kind: str
    name: Optional[str] = None

    @property
    def label(self) -> str:
        if self.kind == "preset":
            return f"preset '{self.name}'"
        if self.kind == "build":
            return "configured build directory"
        return "default configuration"


def _ignore(_: str) -> None:
    pass


def is_configured(build_dir: Path) -> bool:
    return (build_dir / "CMakeCache.txt").is_file()


def list_configure_presets(cmake: str, source_dir: Path) -> List[str]:
    """Return the configure presets CMake reports for *source_dir*."""
    if not any((source_dir / f).is_file() for f in PRESET_FILES):
        return []
    result = subprocess.run(
        [cmake, "--list-presets"], cwd=source_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [
        m.group(1)
        for m in map(_PRESET_LINE_RE.match, result.stdout.splitlines())
        if m
    ]


def available_profiles(source_dir: Path, build_dir: Path) -> List[Profile]:
    """List the profiles that can be analyzed, the automatic choice first."""
    profiles = []
    if is_configured(build_dir):
        profiles.append(Profile("build"))
    cmake = shutil.which("cmake")
    if cmake is not None:
        profiles += [
            Profile("preset", name) for name in list_configure_presets(cmake, source_dir)
        ]
    profiles.append(Profile("default"))
    return profiles


def describe_backend(info: CMakeInfo) -> str:
    """One-line description of where *info* came from."""
    if info.backend != "file-api":
        return "static CMakeLists.txt parsing"
    details = [d for d in (info.config, info.preset and f"preset {info.preset}") if d]
    desc = "CMake File API"
    if details:
        desc += f" ({', '.join(details)})"
    if info.analysis_dir:
        try:
            where = info.analysis_dir.relative_to(Path.cwd())
        except ValueError:
            where = info.analysis_dir
        desc += f", analyzed in {where}"
    return desc


def analysis_dir(build_dir: Path, profile: Profile) -> Path:
    """Return the private tree used to analyze *profile*."""
    name = re.sub(r"[^\w.-]", "_", profile.name) if profile.name else "default"
    return build_dir / ANALYSIS_DIR_NAME / name


def inspect_project(
    source_dir: Path,
    build_dir: Path,
    backend: str = "auto",
    config: Optional[str] = None,
    profile: Optional[Profile] = None,
    note: Callback = _ignore,
    on_output: Callback = _ignore,
) -> CMakeInfo:
    """Return project information for *source_dir* built in *build_dir*.

    *backend* is one of BACKENDS. With "auto", File API failures are reported
    through *note* and the static parser is used instead. *profile* defaults
    to the first of available_profiles(). CMake's output while configuring
    is passed line by line to *on_output*.

    Raises IntrospectError, or ValueError for an unknown preset or config.
    """
    if backend != "static":
        try:
            return _from_file_api(
                source_dir, build_dir, config, profile, note, on_output
            )
        except FileApiError as exc:
            if backend == "file-api":
                raise IntrospectError(str(exc)) from exc
            note(
                f"{exc}\nFalling back to static CMakeLists.txt parsing; "
                "results may be incomplete."
            )
    return parse_cmake_file(source_dir / "CMakeLists.txt", build_dir)


def _from_file_api(
    source_dir: Path,
    build_dir: Path,
    config: Optional[str],
    profile: Optional[Profile],
    note: Callback,
    on_output: Callback,
) -> CMakeInfo:
    source_dir = source_dir.resolve()
    if profile is None:
        profile = available_profiles(source_dir, build_dir)[0]

    if profile.kind == "build":
        if not is_configured(build_dir):
            raise FileApiError(f"Build directory '{build_dir}' is not configured.")
        tree = build_dir
        # The build-tree form reuses the cached source directory and settings.
        configure_args = [str(build_dir.resolve())]
    else:
        tree = analysis_dir(build_dir, profile)
        configure_args = ["-S", str(source_dir), "-B", str(tree.resolve())]
        if profile.kind == "preset":
            configure_args += ["--preset", str(profile.name)]
        else:
            configure_args.append("-DCMAKE_BUILD_TYPE=Debug")

    snapshot_file = tree / SNAPSHOT_NAME
    reply = Reply.latest(tree) if is_configured(tree) else None
    previous = InputSnapshot.load(snapshot_file) if reply is not None else None
    if previous is not None and previous.reply != reply.index_path.name:
        # CMake re-ran without cmakex (e.g. during a build), so these hashes
        # do not describe the current reply.
        previous = None
    changes = previous.changes() if previous is not None else []
    known = previous.files if previous is not None else {}

    if changes or _needs_configure(reply, previous, source_dir, profile):
        cmake = _require_cmake(source_dir, profile)
        what = f"{describe_changes(changes, source_dir)}. " if changes else ""
        if tree == build_dir:
            why = "" if changes else " to refresh File API data"
            note(f"{what}Re-running CMake configure in '{tree}'{why}.")
        else:
            verb = "Re-analyzing" if changes else "Analyzing"
            note(
                f"{what}{verb} with {profile.label} in '{tree}' "
                "(configure only, nothing is compiled)."
            )
        # Hash before configuring: a file edited while CMake runs then shows
        # up as a change next time instead of being missed.
        before = set(known) | set(reply.input_files() if reply else [])
        known = {p: file_state(p, known.get(p)) for p in before}
        _configure(cmake, configure_args, source_dir, tree, on_output)
        reply = Reply.latest(tree)
        if reply is None:
            raise FileApiError("CMake did not write a File API reply.")

    info = reply.read_info(config)
    if info.source_dir.resolve() != source_dir:
        raise FileApiError(
            f"Build directory '{tree}' was configured for "
            f"'{info.source_dir}', not '{source_dir}'."
        )
    if profile.kind == "preset":
        info.preset = profile.name
    if tree != build_dir:
        info.analysis_dir = info.build_dir
        info.relocate(info.build_dir, build_dir.resolve())

    tracked = reply.input_files()
    if profile.kind == "preset":
        tracked += [source_dir / f for f in PRESET_FILES]
    snapshot = InputSnapshot.capture(dict.fromkeys(tracked), known, reply.index_path.name)
    if snapshot != previous:
        try:
            snapshot.save(snapshot_file)
        except OSError:
            pass  # Read-only build tree: change detection falls back to timestamps
    info.inputs = snapshot
    info.changes = changes
    return info


def _needs_configure(
    reply: Optional[Reply],
    previous: Optional[InputSnapshot],
    source_dir: Path,
    profile: Profile,
) -> bool:
    """Whether to configure for reasons other than changed file contents."""
    if reply is None or not all(reply.has(*obj) for obj in _REQUIRED_OBJECTS):
        return True
    if previous is not None:
        return False  # Content hashes are authoritative
    # No snapshot belongs to this reply (first run, or CMake regenerated it
    # outside cmakex): fall back to timestamps.
    if reply.is_stale():
        return True
    if profile.kind == "preset":
        written = reply.index_path.stat().st_mtime
        return any(
            (source_dir / f).is_file() and (source_dir / f).stat().st_mtime > written
            for f in PRESET_FILES
        )
    return False


def _require_cmake(source_dir: Path, profile: Profile) -> str:
    cmake = shutil.which("cmake")
    if cmake is None:
        raise FileApiError("cmake not found in PATH.")
    if not supports_file_api(cmake):
        version = ".".join(map(str, MIN_CMAKE_VERSION))
        raise FileApiError(f"The CMake File API requires CMake >= {version}.")
    if profile.kind == "preset":
        presets = list_configure_presets(cmake, source_dir)
        if profile.name not in presets:
            raise ValueError(
                f"configure preset '{profile.name}' not found; "
                f"available: {', '.join(presets) or '(none)'}"
            )
    return cmake


def _configure(
    cmake: str,
    args: List[str],
    source_dir: Path,
    tree: Path,
    on_output: Callback,
) -> None:
    """Run CMake configure for *tree*, streaming its output."""
    write_query(tree)
    scratch = tree.parent
    if scratch.name == ANALYSIS_DIR_NAME:
        # Keep analysis trees out of version control even if build/ is not ignored.
        (scratch / ".gitignore").write_text("*\n")

    tail: deque = deque(maxlen=10)
    # cwd matters for --preset, which reads the presets of the cwd.
    proc = process.spawn([cmake, *args], cwd=source_dir)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            on_output(line)
    except BaseException:
        # E.g. Ctrl+C during `cmakex info`: CMake runs in its own session,
        # so the terminal's SIGINT did not reach it.
        process.stop(proc)
        raise
    finally:
        rc = process.finish(proc)
    if rc != 0:
        lines = "\n".join(f"  {line}" for line in tail)
        raise FileApiError(f"CMake configure failed:\n{lines}")
