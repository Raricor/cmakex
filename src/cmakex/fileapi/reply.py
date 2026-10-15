"""Read CMake File API replies into CMakeInfo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import CacheEntry, CMakeInfo, ExecutableInfo, LibraryInfo, Toolchain

_LIBRARY_TYPES = {
    "STATIC_LIBRARY": "STATIC",
    "SHARED_LIBRARY": "SHARED",
    "MODULE_LIBRARY": "MODULE",
    "OBJECT_LIBRARY": "OBJECT",
    "INTERFACE_LIBRARY": "INTERFACE",
}


class FileApiError(Exception):
    """A File API reply is missing, unreadable, or cannot be produced."""


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileApiError(f"cannot read File API reply '{path}': {exc}") from exc


class Reply:
    """One File API reply, identified by its index file."""

    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.reply_dir = index_path.parent
        self.index = _load_json(index_path)

    @staticmethod
    def latest_index(build_dir: Path) -> Optional[Path]:
        """Return the newest reply index file in *build_dir*, if any.

        CMake names index files so that the newest sorts last.
        """
        reply_dir = build_dir / ".cmake" / "api" / "v1" / "reply"
        indexes = sorted(reply_dir.glob("index-*.json"))
        return indexes[-1] if indexes else None

    @classmethod
    def latest(cls, build_dir: Path) -> Optional[Reply]:
        """Return the newest reply in *build_dir*, or None if there is none."""
        index = cls.latest_index(build_dir)
        return cls(index) if index is not None else None

    def has(self, kind: str, major: int) -> bool:
        return self._object_file(kind, major) is not None

    def _object_file(self, kind: str, major: int) -> Optional[Path]:
        # "objects" lists replies for every client, so a codemodel requested
        # by an IDE (VS Code, CLion, ...) is usable too.
        for obj in self.index.get("objects", []):
            if obj.get("kind") == kind and obj.get("version", {}).get("major") == major:
                return self.reply_dir / obj["jsonFile"]
        return None

    def _object(self, kind: str, major: int) -> Dict[str, Any]:
        path = self._object_file(kind, major)
        if path is None:
            raise FileApiError(f"File API reply has no {kind}-v{major} object")
        return _load_json(path)

    def input_files(self) -> List[Path]:
        """Files CMake read while configuring, except generated files and
        CMake's own modules. Empty without a cmakeFiles reply."""
        if not self.has("cmakeFiles", 1):
            return []
        files = self._object("cmakeFiles", 1)
        source = Path(files["paths"]["source"])
        return [
            source / entry["path"]
            for entry in files.get("inputs", [])
            if not entry.get("isGenerated") and not entry.get("isCMake")
        ]

    def is_stale(self) -> bool:
        """True if an input file was modified after this reply was written.

        Timestamp-based; used when no content snapshot belongs to this reply.
        Without a cmakeFiles reply the inputs are unknown, so the reply is
        trusted.
        """
        written = self.index_path.stat().st_mtime
        for path in self.input_files():
            try:
                if path.stat().st_mtime > written:
                    return True
            except OSError:
                return True  # An input file was removed
        return False

    def read_info(self, config: Optional[str] = None) -> CMakeInfo:
        """Build CMakeInfo from the codemodel for *config* (default: the first).

        Raises ValueError if *config* is not one of the configured builds.
        """
        model = self._object("codemodel", 2)
        configs = model.get("configurations", [])
        if not configs:
            raise FileApiError("File API codemodel has no configurations")

        if config is None:
            cfg = configs[0]
        else:
            cfg = next(
                (c for c in configs if c["name"].lower() == config.lower()), None
            )
            if cfg is None:
                names = ", ".join(c["name"] or "(default)" for c in configs)
                raise ValueError(
                    f"configuration '{config}' not found; available: {names}"
                )

        source = Path(model["paths"]["source"])
        build = Path(model["paths"]["build"])
        projects = cfg.get("projects", [])
        info = CMakeInfo(
            source_dir=source,
            build_dir=build,
            backend="file-api",
            config=cfg["name"],
            project_name=projects[0]["name"] if projects else None,
        )
        refs = cfg.get("targets", [])
        names = {ref["id"]: ref["name"] for ref in refs}
        for ref in refs:
            target = _load_json(self.reply_dir / ref["jsonFile"])
            self._add_target(info, target, source, build, names)
        info.deduplicate()
        if self.has("cache", 2):
            info.cache = self._read_cache()
        if self.has("toolchains", 1):
            info.toolchains = self._read_toolchains()
        return info

    def _read_cache(self) -> List[CacheEntry]:
        entries = []
        for e in self._object("cache", 2).get("entries", []):
            props = {p["name"]: p["value"] for p in e.get("properties", [])}
            entries.append(
                CacheEntry(
                    name=e["name"],
                    type=e["type"],
                    value=e["value"],
                    help=props.get("HELPSTRING", ""),
                    advanced=props.get("ADVANCED", "0") not in ("0", ""),
                )
            )
        return entries

    def _read_toolchains(self) -> List[Toolchain]:
        toolchains = []
        for t in self._object("toolchains", 1).get("toolchains", []):
            compiler = t.get("compiler", {})
            toolchains.append(
                Toolchain(
                    language=t["language"],
                    compiler_id=compiler.get("id", ""),
                    version=compiler.get("version", ""),
                    path=compiler.get("path", ""),
                )
            )
        return toolchains

    @staticmethod
    def _defined_at(target: Dict[str, Any]) -> Optional[str]:
        """Return "file:line" of the command that created *target*."""
        graph = target.get("backtraceGraph")
        index = target.get("backtrace")
        if graph is None or index is None:
            return None
        node = graph["nodes"][index]
        location = graph["files"][node["file"]]
        if "line" in node:
            location += f":{node['line']}"
        return location

    @classmethod
    def _add_target(
        cls,
        info: CMakeInfo,
        target: Dict[str, Any],
        source: Path,
        build: Path,
        names: Dict[str, str],
    ) -> None:
        include_dirs = [
            inc["path"]
            for group in target.get("compileGroups", [])
            for inc in group.get("includes", [])
        ]
        info.include_dirs.extend(include_dirs)

        # Artifact paths are relative to the top-level build directory unless
        # they lie outside it; joining handles both. The first one is the
        # primary file (e.g. the .dll rather than its import library).
        artifacts = target.get("artifacts") or []
        common = dict(
            name=target["name"],
            source_dir=source / target.get("paths", {}).get("source", "."),
            artifact=build / artifacts[0]["path"] if artifacts else None,
            defined_at=cls._defined_at(target),
            sources=[
                s["path"] for s in target.get("sources", []) if not s.get("isGenerated")
            ],
            include_dirs=list(dict.fromkeys(include_dirs)),
            dependencies=[
                names[d["id"]] for d in target.get("dependencies", []) if d["id"] in names
            ],
        )

        ttype = target.get("type")
        if ttype == "EXECUTABLE":
            info.executables.append(ExecutableInfo(**common))
        elif ttype in _LIBRARY_TYPES:
            info.libraries.append(LibraryInfo(lib_type=_LIBRARY_TYPES[ttype], **common))
