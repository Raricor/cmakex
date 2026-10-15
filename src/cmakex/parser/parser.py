"""Static CMake parser — orchestrates lexing, variable resolution, and command handling.

Used when the CMake File API is unavailable. It does not evaluate if()
conditions or run functions/macros, so results are best-effort.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..inputs import InputSnapshot
from ..models import CMakeInfo, ExecutableInfo, LibraryInfo
from .lexer import CMakeLexer
from .resolver import VariableResolver

_BUILD_GENEX_RE = re.compile(r"^\$<BUILD(?:_LOCAL)?_INTERFACE:(.*)>$")
_INSTALL_GENEX_RE = re.compile(r"^\$<INSTALL_INTERFACE:.*>$")
_CACHE_ENTRY_RE = re.compile(r"^([A-Za-z_][\w.+-]*):[A-Z]+=(.*)$")


def _is_true(value: str) -> bool:
    """CMake truthiness for the common constant forms."""
    v = value.upper()
    if v in ("1", "ON", "YES", "TRUE", "Y"):
        return True
    try:
        return float(v) != 0
    except ValueError:
        return False


def load_cache(build_dir: Path) -> Dict[str, str]:
    """Read cache entries from *build_dir*/CMakeCache.txt, if it exists."""
    cache = build_dir / "CMakeCache.txt"
    if not cache.is_file():
        return {}
    entries: Dict[str, str] = {}
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _CACHE_ENTRY_RE.match(line)
        if m:
            entries[m.group(1)] = m.group(2)
    return entries


class CMakeParser:
    """Parse one CMake directory; add_subdirectory() creates child parsers."""

    LIB_TYPE_KEYWORDS = {
        "SHARED",
        "STATIC",
        "MODULE",
        "INTERFACE",
        "OBJECT",
    }

    INCLUDE_SKIP_KEYWORDS = {
        "BEFORE",
        "AFTER",
        "SYSTEM",
        "INTERFACE",
        "PUBLIC",
        "PRIVATE",
    }

    def __init__(
        self,
        cmake_file: Path,
        binary_dir: Path,
        parent: Optional[CMakeParser] = None,
    ):
        """Initialize parser for *cmake_file* whose build tree is *binary_dir*."""
        self.cmake_file = cmake_file.resolve()
        self.source_dir = self.cmake_file.parent
        self.binary_dir = binary_dir.resolve()
        self.parent = parent
        self.is_top_level = parent is None

        if parent is None:
            self.info = CMakeInfo(source_dir=self.source_dir, build_dir=self.binary_dir)
            # Target names are global, so properties are collected project-wide
            # and applied once at the end.
            self.target_props: Dict[str, Dict[str, str]] = {}
            self.visited: Set[Path] = set()
            # Cache entries are visible as variables unless shadowed.
            variables = load_cache(self.binary_dir)
            variables.update(
                {
                    "CMAKE_SOURCE_DIR": str(self.source_dir),
                    "CMAKE_BINARY_DIR": str(self.binary_dir),
                    "PROJECT_SOURCE_DIR": str(self.source_dir),
                    "PROJECT_BINARY_DIR": str(self.binary_dir),
                }
            )
        else:
            self.info = parent.info
            self.target_props = parent.target_props
            self.visited = parent.visited
            # A subdirectory starts with a copy of its parent's variables.
            variables = dict(parent.resolver.variables)

        variables["CMAKE_CURRENT_SOURCE_DIR"] = str(self.source_dir)
        variables["CMAKE_CURRENT_BINARY_DIR"] = str(self.binary_dir)
        self.resolver = VariableResolver(variables)

    def parse(self) -> CMakeInfo:
        """Parse the top-level CMakeLists.txt and every directory it adds."""
        self._parse_file(self.cmake_file)
        self._apply_target_properties()
        self.info.deduplicate()
        self.info.inputs = InputSnapshot.capture(sorted(self.visited))
        return self.info

    def _parse_file(self, path: Path) -> None:
        """Run the commands of *path* in this directory's scope."""
        path = path.resolve()
        if path in self.visited or not path.is_file():
            return
        self.visited.add(path)

        saved_list_dir = self.resolver.get("CMAKE_CURRENT_LIST_DIR", None)
        saved_list_file = self.resolver.get("CMAKE_CURRENT_LIST_FILE", None)
        self.resolver.set("CMAKE_CURRENT_LIST_DIR", str(path.parent))
        self.resolver.set("CMAKE_CURRENT_LIST_FILE", str(path))

        lexer = CMakeLexer(path.read_text(encoding="utf-8", errors="replace"))
        # Bodies of function()/macro() definitions only run when called.
        definition_depth = 0
        for cmd_name, raw_args in lexer.extract_commands():
            cmd = cmd_name.upper()
            if cmd in ("FUNCTION", "MACRO"):
                definition_depth += 1
            elif cmd in ("ENDFUNCTION", "ENDMACRO"):
                definition_depth = max(definition_depth - 1, 0)
            elif definition_depth == 0:
                args = [self.resolver.resolve(a) for a in lexer.parse_args(raw_args)]
                self._handle_command(cmd, args)

        for name, value in (
            ("CMAKE_CURRENT_LIST_DIR", saved_list_dir),
            ("CMAKE_CURRENT_LIST_FILE", saved_list_file),
        ):
            if value is None:
                self.resolver.unset(name)
            else:
                self.resolver.set(name, value)

    def _handle_command(self, cmd: str, args: List[str]) -> None:
        """Dispatch command to appropriate handler."""
        handlers = {
            "PROJECT": self._handle_project,
            "SET": self._handle_set,
            "OPTION": self._handle_option,
            "INCLUDE": self._handle_include,
            "INCLUDE_DIRECTORIES": self._handle_include_directories,
            "TARGET_INCLUDE_DIRECTORIES": self._handle_target_include_directories,
            "ADD_LIBRARY": self._handle_add_library,
            "ADD_EXECUTABLE": self._handle_add_executable,
            "SET_TARGET_PROPERTIES": self._handle_set_target_properties,
            "ADD_SUBDIRECTORY": self._handle_add_subdirectory,
        }

        handler = handlers.get(cmd)
        if handler:
            handler(args)

    def _handle_project(self, args: List[str]) -> None:
        """Handle PROJECT() command."""
        if not args:
            return
        name = args[0]
        r = self.resolver
        r.set("PROJECT_NAME", name)
        r.set("PROJECT_SOURCE_DIR", str(self.source_dir))
        r.set("PROJECT_BINARY_DIR", str(self.binary_dir))
        r.set(f"{name}_SOURCE_DIR", str(self.source_dir))
        r.set(f"{name}_BINARY_DIR", str(self.binary_dir))
        r.set("PROJECT_IS_TOP_LEVEL", "ON" if self.is_top_level else "OFF")
        if self.is_top_level:
            r.set("CMAKE_PROJECT_NAME", name)
            self.info.project_name = name
        if "VERSION" in args[1:]:
            i = args.index("VERSION", 1)
            if i + 1 < len(args):
                r.set("PROJECT_VERSION", args[i + 1])
                r.set(f"{name}_VERSION", args[i + 1])

    def _handle_set(self, args: List[str]) -> None:
        """Handle SET() command."""
        if not args:
            return
        resolver = self.resolver
        if "PARENT_SCOPE" in args[1:]:
            if self.parent is None:
                return
            resolver = self.parent.resolver
            args = [a for a in args if a != "PARENT_SCOPE"]

        val_parts: List[str] = []
        for a in args[1:]:
            if a == "CACHE":
                break
            val_parts.append(a)
        if val_parts:
            resolver.set(args[0], ";".join(val_parts))
        else:
            resolver.unset(args[0])

    def _handle_option(self, args: List[str]) -> None:
        """Handle OPTION() command — never overrides an existing value."""
        if args and args[0] not in self.resolver.variables:
            self.resolver.set(args[0], args[2] if len(args) > 2 else "OFF")

    def _handle_include(self, args: List[str]) -> None:
        """Handle INCLUDE() command for project files; built-in modules are skipped."""
        if not args:
            return
        target = args[0]
        if target.endswith(".cmake") or "/" in target:
            candidates = [self.source_dir / target]
        else:
            module_path = self.resolver.get("CMAKE_MODULE_PATH")
            candidates = [
                self.source_dir / d / f"{target}.cmake"
                for d in module_path.split(";")
                if d
            ]
        for candidate in candidates:
            if candidate.is_file():
                self._parse_file(candidate)
                return

    def _handle_include_directories(self, args: List[str]) -> None:
        """Handle INCLUDE_DIRECTORIES() command."""
        self._add_include_dirs(args)

    def _handle_target_include_directories(self, args: List[str]) -> None:
        """Handle TARGET_INCLUDE_DIRECTORIES() command."""
        if not args:
            return
        added = self._add_include_dirs(args[1:])
        for target in [*self.info.libraries, *self.info.executables]:
            if target.name == args[0]:
                target.include_dirs.extend(added)

    def _add_include_dirs(self, args: List[str]) -> List[str]:
        added: List[str] = []
        for arg in args:
            # A variable may have expanded to a ;-list.
            for item in arg.split(";"):
                if not item or item in self.INCLUDE_SKIP_KEYWORDS:
                    continue
                if _INSTALL_GENEX_RE.match(item):
                    continue  # Install-tree path, not used when building
                m = _BUILD_GENEX_RE.match(item)
                if m:
                    item = m.group(1)
                    if not item:
                        continue
                if not item.startswith("$<") and not os.path.isabs(item):
                    item = os.path.normpath(self.source_dir / item)
                added.append(item)
        self.info.include_dirs.extend(added)
        return added

    def _output_dir(self, value: str) -> str:
        """Resolve an output directory; relative paths are under the binary dir."""
        if not value:
            return str(self.binary_dir)
        if os.path.isabs(value):
            return value
        return os.path.normpath(self.binary_dir / value)

    def _handle_add_library(self, args: List[str]) -> None:
        """Handle ADD_LIBRARY() command."""
        if not args or "ALIAS" in args[1:] or "IMPORTED" in args[1:]:
            return

        lib_name = args[0]
        lib_type = next((a for a in args[1:] if a in self.LIB_TYPE_KEYWORDS), None)
        if lib_type is None:
            lib_type = (
                "SHARED" if _is_true(self.resolver.get("BUILD_SHARED_LIBS")) else "STATIC"
            )

        lib = LibraryInfo(name=lib_name, lib_type=lib_type, source_dir=self.source_dir)

        # Set default output directory from CMAKE_* variables
        out_var = (
            "CMAKE_LIBRARY_OUTPUT_DIRECTORY"
            if lib_type in ("SHARED", "MODULE")
            else "CMAKE_ARCHIVE_OUTPUT_DIRECTORY"
        )
        lib.output_dir = self._output_dir(self.resolver.get(out_var))

        self.target_props.setdefault(lib_name, {})
        self.info.libraries.append(lib)

    def _handle_add_executable(self, args: List[str]) -> None:
        """Handle ADD_EXECUTABLE() command."""
        if not args or "ALIAS" in args[1:] or "IMPORTED" in args[1:]:
            return

        exe_name = args[0]
        exe = ExecutableInfo(name=exe_name, source_dir=self.source_dir)
        exe.output_dir = self._output_dir(
            self.resolver.get("CMAKE_RUNTIME_OUTPUT_DIRECTORY")
        )

        self.target_props.setdefault(exe_name, {})
        self.info.executables.append(exe)

    def _handle_set_target_properties(self, args: List[str]) -> None:
        """Handle SET_TARGET_PROPERTIES() command."""
        if "PROPERTIES" not in args:
            return

        sep = args.index("PROPERTIES")
        targets = args[:sep]
        pairs = args[sep + 1 :]

        props: Dict[str, str] = {}
        for i in range(0, len(pairs) - 1, 2):
            key, value = pairs[i], pairs[i + 1]
            if key.endswith("_OUTPUT_DIRECTORY"):
                value = self._output_dir(value)
            props[key] = value

        for t in targets:
            self.target_props.setdefault(t, {}).update(props)

    def _handle_add_subdirectory(self, args: List[str]) -> None:
        """Handle ADD_SUBDIRECTORY() command — recursively parse subdirectory."""
        if not args:
            return
        source = args[0]
        rest = [a for a in args[1:] if a not in ("EXCLUDE_FROM_ALL", "SYSTEM")]
        if rest:
            binary = rest[0]
        else:
            binary = Path(source).name if os.path.isabs(source) else source

        sub_cmake = self.source_dir / source / "CMakeLists.txt"
        if sub_cmake.is_file():
            child = CMakeParser(sub_cmake, self.binary_dir / binary, parent=self)
            child._parse_file(child.cmake_file)

    def _apply_target_properties(self) -> None:
        """Apply set_target_properties() and compute artifact paths."""
        for lib in self.info.libraries:
            props = self.target_props.get(lib.name, {})
            if "OUTPUT_NAME" in props:
                lib.output_name = props["OUTPUT_NAME"]

            dir_key = (
                "LIBRARY_OUTPUT_DIRECTORY"
                if lib.lib_type in ("SHARED", "MODULE")
                else "ARCHIVE_OUTPUT_DIRECTORY"
            )
            if dir_key in props:
                lib.output_dir = props[dir_key]
            if lib.lib_type in ("SHARED", "STATIC", "MODULE"):
                lib.artifact = Path(lib.output_dir or self.binary_dir) / lib.filename()

        for exe in self.info.executables:
            props = self.target_props.get(exe.name, {})
            if "OUTPUT_NAME" in props:
                exe.output_name = props["OUTPUT_NAME"]
            if "RUNTIME_OUTPUT_DIRECTORY" in props:
                exe.output_dir = props["RUNTIME_OUTPUT_DIRECTORY"]
            exe.artifact = Path(exe.output_dir or self.binary_dir) / exe.filename()


def parse_cmake_file(cmake_file: Path, build_dir: Path = Path("build")) -> CMakeInfo:
    """Parse *cmake_file* (and add_subdirectory() children) and return CMakeInfo.

    *build_dir* is the top-level build tree used to resolve
    ``CMAKE_BINARY_DIR`` and default output paths; its CMakeCache.txt, if
    present, seeds the variables.
    """
    return CMakeParser(cmake_file, build_dir).parse()
