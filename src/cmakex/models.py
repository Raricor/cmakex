"""Data models for CMake project information."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Union

from .inputs import Change, InputSnapshot


@dataclass
class LibraryInfo:
    """A library target defined with add_library()."""

    name: str
    lib_type: str  # SHARED | STATIC | MODULE | INTERFACE | OBJECT
    output_dir: Optional[str] = None
    output_name: Optional[str] = None
    source_dir: Optional[Path] = None  # Directory where this library was defined
    artifact: Optional[Path] = None  # Path of the built file on disk
    defined_at: Optional[str] = None  # "file:line" of the add_library() call
    sources: List[str] = field(default_factory=list)
    include_dirs: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)  # Project targets

    def filename(self) -> str:
        """Best-effort output filename for this library."""
        base = self.output_name or self.name
        if self.lib_type == "SHARED":
            return f"lib{base}.so"
        if self.lib_type == "STATIC":
            return f"lib{base}.a"
        if self.lib_type == "MODULE":
            return f"lib{base}.so"
        return base


@dataclass
class ExecutableInfo:
    """An executable target defined with add_executable()."""

    name: str
    output_dir: Optional[str] = None
    output_name: Optional[str] = None
    source_dir: Optional[Path] = None  # Directory where this executable was defined
    artifact: Optional[Path] = None  # Path of the built file on disk
    defined_at: Optional[str] = None  # "file:line" of the add_executable() call
    sources: List[str] = field(default_factory=list)
    include_dirs: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)  # Project targets

    def filename(self) -> str:
        return self.output_name or self.name


Target = Union[LibraryInfo, ExecutableInfo]


@dataclass
class CacheEntry:
    """One CMakeCache.txt entry."""

    name: str
    type: str  # BOOL | STRING | PATH | FILEPATH | INTERNAL | STATIC | UNINITIALIZED
    value: str
    help: str = ""
    advanced: bool = False


@dataclass
class Toolchain:
    """The compiler CMake selected for one language."""

    language: str
    compiler_id: str
    version: str
    path: str


@dataclass
class CMakeInfo:
    """Aggregated information about a CMake project."""

    source_dir: Path = field(default_factory=Path)
    build_dir: Optional[Path] = None
    backend: str = "static"  # "file-api" | "static"
    config: Optional[str] = None  # Build configuration reported by the File API
    project_name: Optional[str] = None
    preset: Optional[str] = None  # Configure preset the analysis used
    analysis_dir: Optional[Path] = None  # Scratch tree configured for analysis
    include_dirs: List[str] = field(default_factory=list)
    libraries: List[LibraryInfo] = field(default_factory=list)
    executables: List[ExecutableInfo] = field(default_factory=list)
    cache: List[CacheEntry] = field(default_factory=list)
    toolchains: List[Toolchain] = field(default_factory=list)
    #: Fingerprints of the CMake files this information was derived from.
    inputs: Optional[InputSnapshot] = None
    #: Changed files that made this analysis re-run configure.
    changes: List[Change] = field(default_factory=list)

    @property
    def shared_libs(self) -> List[LibraryInfo]:
        """Get list of shared and module libraries."""
        return [lib for lib in self.libraries if lib.lib_type in ("SHARED", "MODULE")]

    @property
    def static_libs(self) -> List[LibraryInfo]:
        """Get list of static libraries."""
        return [lib for lib in self.libraries if lib.lib_type == "STATIC"]

    def deduplicate(self) -> None:
        """Remove duplicate include directories while preserving order."""
        seen: Set[str] = set()
        unique: List[str] = []
        for d in self.include_dirs:
            if d not in seen:
                seen.add(d)
                unique.append(d)
        self.include_dirs = unique

    def relocate(self, old: Path, new: Path) -> None:
        """Rewrite paths under build tree *old* as if built in *new*.

        Used to present results from the analysis tree as paths in the
        user's build directory.
        """

        def move(path: str) -> str:
            try:
                return str(new / Path(path).relative_to(old))
            except ValueError:
                return path

        self.include_dirs = [move(d) for d in self.include_dirs]
        targets: List[Target] = [*self.libraries, *self.executables]
        for t in targets:
            t.include_dirs = [move(d) for d in t.include_dirs]
            if t.artifact is not None:
                t.artifact = Path(move(str(t.artifact)))
        self.build_dir = new
