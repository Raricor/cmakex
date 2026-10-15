# cmakex

A lightweight CMake build eXtension for the command line.

Wraps common CMake workflows and provides project introspection through the
[CMake File API](https://cmake.org/cmake/help/latest/manual/cmake-file-api.7.html):
cmakex lets CMake itself configure the project (without compiling), so
`if()` branches, functions and `find_package()` are evaluated exactly. A
`CMakeLists.txt` parser is the fallback when that is not possible.

## Features

- **Interactive UI** — run `cmakex` with no command to browse targets, include directories, cache options and compilers, and configure/build/clean/install with one key.
- **`info`** — Display include directories, shared/static library output paths, and executables.
- **`configure`** — Create the build directory and run `cmake -S . -B build` (also enables the File API for `info`).
- **`build`** / **`compile`** — Run `cmake --build`.
- **`clean`** — Run the `clean` target, or `--purge` to delete the entire build directory.

## Installation

```bash
pip install cmakex
```

Or install from source:

```bash
git clone https://github.com/winterYANGWT/cmakex.git
cd cmakex
pip install -e .
```

## Quick Start

```bash
# Open the interactive UI
cmakex

# Inspect the current project
cmakex info

# Configure (creates ./build, runs cmake)
cmakex configure -B build -G Ninja -D CMAKE_BUILD_TYPE=Release

# Build
cmakex build -B build -j8

# Or use the alias
cmakex compile -j$(nproc)

# Clean (runs make clean / ninja clean)
cmakex clean

# Remove the build directory entirely
cmakex clean --purge
```

## Interactive UI

```
cmakex [ui] [-S SOURCE_DIR] [-B BUILD_DIR]
```

On startup cmakex analyzes the project (see [Analysis](#analysis)), then shows:

- Libraries and executables: type, output path (and whether it is built),
  where the target is defined, dependencies, include directories, sources
- Include directories
- Cache options (`option()`s, `CMAKE_BUILD_TYPE`, …)
- Compilers

| Key | Action |
|-----|--------|
| `c` | Configure `BUILD_DIR` (with the analyzed preset, if any) |
| `b` | Build `BUILD_DIR` |
| `l` | Clean `BUILD_DIR` (asks first; also uninstalls files from `install_manifest.txt`) |
| `i` | Install (asks first, showing `CMAKE_INSTALL_PREFIX`) |
| `r` | Re-analyze |
| `p` | Choose what to analyze: the configured build directory, a configure preset, or the default configuration |
| `g` | Show/hide the log |
| `q` | Quit |

Command output is streamed into the log panel. When stdin/stdout is not a
terminal, `cmakex` prints its help instead.

While the UI is open, cmakex watches the project's CMake files (see
[Change detection](#change-detection)). When one changes, the header shows
how many files changed and the project is re-analyzed automatically a moment
later; the selected item stays selected. If that configure fails, typically
because a `CMakeLists.txt` is half-edited, the previous results stay on
screen with "configure failed" in the header, and cmakex retries once the
file is saved again. Running `cmake` on the build directory outside cmakex
also triggers a reload.

## Analysis

`cmakex info` and the interactive UI collect project information the same way:

1. If `BUILD_DIR` is configured, read its File API data. If it is missing
   (e.g. the build was configured without cmakex) or a CMake file changed
   since, configure is re-run on that tree first.
2. Otherwise configure a private analysis tree, `BUILD_DIR/.cmakex/<preset>`
   — using the first configure preset from `CMakePresets.json`, or CMake
   defaults with `CMAKE_BUILD_TYPE=Debug` (`BUILD_DIR/.cmakex/default`) when
   there are no presets. This only configures; nothing is compiled. Later
   runs reuse it until a CMake file or the presets change.
   Paths are shown as they will be once `BUILD_DIR` is configured the same
   way.
3. If configure is not possible (CMake < 3.14, or it fails, e.g. because a
   dependency is missing), print a note with CMake's error and fall back to
   static parsing.

`BUILD_DIR/.cmakex/` contains a `.gitignore`, and `cmakex clean` leaves it
alone; `cmakex clean --purge` removes it along with the rest of `BUILD_DIR`.

### Change detection

After each analysis cmakex records the MD5 of every file CMake read while
configuring: all `CMakeLists.txt` files, `include()`d `.cmake` files,
`configure_file()` inputs and package config files, but not CMake's own
modules (plus `CMakePresets.json`/`CMakeUserPresets.json` when analyzing a
preset). The snapshot is stored as `.cmakex-inputs.json` in the analyzed
build tree.

The next analysis compares contents, not timestamps: an edited file is
reported (e.g. `2 CMake files changed: CMakeLists.txt, test/CMakeLists.txt`)
and triggers configure, while a file that was only touched or saved without
edits does not. Size and modification time are used only to avoid re-hashing
files that were not touched. If CMake regenerated the File API data itself
(e.g. during `cmake --build`), the snapshot is re-taken from that data.

## Commands

### `cmakex info`

```
cmakex info [-S SOURCE_DIR] [-B BUILD_DIR] [--preset PRESET] [--backend auto|file-api|static] [--config CONFIG]
```

Prints:

- Include directories
- Shared libraries (`.so`) with output paths
- Static libraries (`.a`) with output paths
- Executables with output paths

Information comes from one of two backends, chosen as described in
[Analysis](#analysis):

| Backend | When it is used | Accuracy |
|---------|-----------------|----------|
| `file-api` | CMake ≥ 3.14 and configure succeeds | Exact: what CMake itself computed |
| `static` | Fallback (CMake < 3.14, configure fails) | Best-effort |

`--preset` analyzes with that configure preset even if `BUILD_DIR` is
configured. `--backend file-api` turns the static fallback into an error;
`--backend static` skips the File API.

For multi-config generators (Visual Studio, Ninja Multi-Config), `--config`
selects the configuration; the default is the first one.

The static backend parses `CMakeLists.txt`, `add_subdirectory()` directories,
and project files pulled in with `include()`. It understands `project()`,
`set()`, `option()`, values from an existing `CMakeCache.txt`,
`$<BUILD_INTERFACE:…>` / `$<INSTALL_INTERFACE:…>`, and output directories from
`CMAKE_*_OUTPUT_DIRECTORY` and `set_target_properties()`. It does not evaluate
`if()` conditions or run functions/macros, so targets created conditionally
or inside functions may be missing or duplicated.

### `cmakex configure`

```
cmakex configure [-S SOURCE_DIR] [-B BUILD_DIR] [-G GENERATOR] [-D KEY=VALUE ...] [--preset PRESET] [-- EXTRA_CMAKE_ARGS]
```

Creates the build directory, writes a CMake File API query for `cmakex info`,
and runs:

```
cmake -S <source_dir> -B <build_dir> [-G <generator>] [--preset <preset>] [-D <key=value>...] [extra...]
```

With `--preset`, the preset's settings are used but its `binaryDir` is
replaced by `BUILD_DIR`.

### `cmakex build` / `cmakex compile`

```
cmakex build [-B BUILD_DIR] [-j N] [-t TARGET] [--config CONFIG] [-- EXTRA_ARGS]
```

Runs `cmake --build <build_dir>`. `compile` is an alias for `build`.

### `cmakex clean`

```
cmakex clean [-B BUILD_DIR] [--purge]
```

- Default: runs `cmake --build <build_dir> --target clean`
- `--purge`: removes `<build_dir>` entirely (`shutil.rmtree`)

## Global options

| Flag | Description |
|------|-------------|
| `-h`, `--help` | Show help |
| `--version` | Show version |
| `-v`, `--verbose` | Print the cmake command before running |

## Requirements

- Python ≥ 3.9
- CMake ≥ 3.13 (for `-S`/`-B` flag support); ≥ 3.14 for exact `info` results via the File API
- [click](https://click.palletsprojects.com/) ≥ 8.0
- [Textual](https://textual.textualize.io/) ≥ 1.0 (interactive UI)

## Development

```bash
pip install -e ".[dev]"
pytest
```

The analysis and UI tests need `cmake` and a C compiler and are skipped otherwise.

## License

MIT
