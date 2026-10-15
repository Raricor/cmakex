"""Ask CMake to write File API replies for cmakex."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

CLIENT_NAME = "client-cmakex"
QUERY_KINDS = ("codemodel-v2", "cache-v2", "toolchains-v1", "cmakeFiles-v1")
MIN_CMAKE_VERSION = (3, 14)

_VERSION_RE = re.compile(r"cmake version (\d+)\.(\d+)")


def write_query(build_dir: Path) -> None:
    """Create cmakex's stateless queries; CMake answers on its next configure."""
    query_dir = build_dir / ".cmake" / "api" / "v1" / "query" / CLIENT_NAME
    query_dir.mkdir(parents=True, exist_ok=True)
    for kind in QUERY_KINDS:
        (query_dir / kind).touch()


def cmake_version(cmake: str) -> Optional[Tuple[int, int]]:
    """Return (major, minor) of *cmake*, or None if it cannot be determined."""
    try:
        out = subprocess.run(
            [cmake, "--version"], capture_output=True, text=True
        ).stdout
    except OSError:
        return None
    m = _VERSION_RE.search(out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def supports_file_api(cmake: str) -> bool:
    """True if *cmake* is new enough to answer File API queries."""
    version = cmake_version(cmake)
    return version is not None and version >= MIN_CMAKE_VERSION
