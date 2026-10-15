"""MD5 fingerprints of the CMake files an analysis was made from.

A snapshot records the content hash of every file CMake read while
configuring. Comparing it with the files on disk tells whether the analysis
is out of date and which files changed. Size and modification time are only
used to skip re-hashing files that were not touched; a file whose content is
unchanged (e.g. touched, or saved without edits) is never reported.

Like git's "racily clean" check, a file modified shortly before it was
hashed does not get its timestamp recorded: an edit within the same
timestamp tick could otherwise go unnoticed. Such files are re-hashed on
every check until they are older than RACY_WINDOW_NS.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

#: Snapshot file, stored at the root of the build tree it describes.
SNAPSHOT_NAME = ".cmakex-inputs.json"

#: Covers filesystems with coarse timestamps (FAT: 2 s).
RACY_WINDOW_NS = 2_000_000_000


@dataclass
class FileState:
    md5: Optional[str]  # None: the file did not exist
    mtime_ns: int = 0  # 0: not recorded, always re-hash
    size: int = 0


@dataclass
class Change:
    path: Path
    kind: str  # "modified" | "added" | "removed"
    md5: Optional[str]  # Current content hash; None if removed


def file_state(path: Path, previous: Optional[FileState] = None) -> FileState:
    """Fingerprint *path*, reusing *previous*'s hash if size and mtime match."""
    try:
        st = path.stat()
        if (
            previous is not None
            and previous.md5 is not None
            and (previous.mtime_ns, previous.size) == (st.st_mtime_ns, st.st_size)
        ):
            return previous
        digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    except OSError:
        return FileState(md5=None)
    racy = time.time_ns() - st.st_mtime_ns < RACY_WINDOW_NS
    return FileState(digest, 0 if racy else st.st_mtime_ns, st.st_size)


class InputSnapshot:
    """Content hashes of a set of files, optionally tied to a File API reply."""

    def __init__(self, files: Dict[Path, FileState], reply: Optional[str] = None):
        self.files = files
        #: Name of the reply index file these hashes correspond to.
        self.reply = reply

    @classmethod
    def capture(
        cls,
        paths: Iterable[Path],
        known: Optional[Dict[Path, FileState]] = None,
        reply: Optional[str] = None,
    ) -> InputSnapshot:
        """Fingerprint *paths*; states in *known* are taken as they are."""
        known = known or {}
        return cls({p: known[p] if p in known else file_state(p) for p in paths}, reply)

    def changes(self) -> List[Change]:
        """Compare with the files on disk.

        Files that were touched without changing content are not changes;
        their new size/mtime is remembered so they are not hashed again.
        """
        result = []
        for path, old in self.files.items():
            new = file_state(path, old)
            if new.md5 == old.md5:
                self.files[path] = new
                continue
            if new.md5 is None:
                kind = "removed"
            elif old.md5 is None:
                kind = "added"
            else:
                kind = "modified"
            result.append(Change(path, kind, new.md5))
        return result

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InputSnapshot):
            return NotImplemented
        return (self.files, self.reply) == (other.files, other.reply)

    def save(self, path: Path) -> None:
        data = {
            "version": 1,
            "reply": self.reply,
            "files": {
                str(p): [s.md5, s.mtime_ns, s.size] for p, s in self.files.items()
            },
        }
        path.write_text(json.dumps(data, indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> Optional[InputSnapshot]:
        """Read a saved snapshot; None if missing or unreadable."""
        try:
            data = json.loads(path.read_text())
            if data.get("version") != 1:
                return None
            files = {Path(p): FileState(*state) for p, state in data["files"].items()}
            return cls(files, data.get("reply"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None


def describe_changes(changes: List[Change], base: Path, limit: int = 3) -> str:
    """E.g. "2 CMake files changed: CMakeLists.txt, test/CMakeLists.txt"."""

    def name(change: Change) -> str:
        try:
            shown = str(change.path.relative_to(base))
        except ValueError:
            shown = str(change.path)
        return shown if change.kind == "modified" else f"{shown} ({change.kind})"

    names = ", ".join(name(c) for c in changes[:limit])
    if len(changes) > limit:
        names += f" and {len(changes) - limit} more"
    noun = "file" if len(changes) == 1 else "files"
    return f"{len(changes)} CMake {noun} changed: {names}"
