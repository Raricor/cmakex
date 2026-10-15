# Changelog

All notable changes to cmakex are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-15

### Added

- Interactive full-screen interface: run `cmakex` without a command in a
  terminal, or `cmakex ui [-S SOURCE_DIR] [-B BUILD_DIR]`. It shows targets
  and their outputs, include directories, cache options and compilers, and
  runs configure (`c`), build (`b`), clean (`l`) and install (`i`) on
  `BUILD_DIR`, streaming their output into a log panel. Clean and install ask
  for confirmation; `p` chooses what to analyze (the configured build
  directory, a configure preset, or the default configuration).
- Project analysis through the CMake File API (CMake 3.14 or later), used by
  `cmakex info` and the interface. A configured `BUILD_DIR` is read directly;
  otherwise cmakex configures a private analysis tree in
  `BUILD_DIR/.cmakex/<preset>` (configure only, nothing is compiled) with the
  first configure preset, or with CMake defaults and Debug when there are no
  presets. Results follow `if()`, functions and `find_package()` exactly as
  CMake evaluates them.
- `cmakex clean` also uninstalls what `cmakex install` installed, using
  `install_manifest.txt`; the installed files are listed in its confirmation.
- `cmakex info --preset`, `--backend auto|file-api|static` and `--config`
  (for multi-config generators); `cmakex configure --preset`.
- Change detection by content. After each analysis, cmakex records the MD5
  of every file CMake read while configuring (`CMakeLists.txt`, included
  `.cmake` files, package config files, and the presets files when analyzing
  a preset) in `.cmakex-inputs.json`. Edited files are reported by name, e.g.
  `2 CMake files changed: CMakeLists.txt, test/CMakeLists.txt`; files that
  were only touched are not.
- `cmakex info` and the interface's startup analysis re-run configure when an
  input file's content changed. While the interface is open, it reports
  changes in its header and details pane but does not re-analyze until you
  press `r`. Build, clean and install then ask to re-analyze first; declining,
  or a failed re-analysis, cancels them.
- Quitting the interface with `Ctrl+C` pressed twice within 2 seconds. This
  stops a running configure, build, clean or analysis, including the
  processes it started (make/ninja, compilers), instead of waiting for it to
  finish. A running install is never interrupted, and `q` only shows a hint
  while something is running. `Ctrl+Q` is left to editors and terminals.

### Changed

- `cmakex info` prefers the File API and falls back to static
  `CMakeLists.txt` parsing, with a note explaining why, when CMake is older
  than 3.14 or configure fails. It may therefore run CMake configure, and it
  writes File API query files into the build tree it analyzes.
- `cmakex configure` also enables the File API for the build directory, so
  later analyses need no extra configure.
- `cmakex clean` leaves `BUILD_DIR/.cmakex/` alone; `cmakex clean --purge`
  still removes it along with the rest of `BUILD_DIR`.
- Requires Python 3.9 or later (the code already relied on 3.9 APIs) and
  depends on Textual.

### Fixed

Static parsing, now the fallback, gives correct results for common projects:

- `project()` is handled, so `${PROJECT_NAME}` is expanded, including in
  target names.
- Subdirectories inherit their parent's variables, `set(... PARENT_SCOPE)`
  propagates back, and `CMAKE_SOURCE_DIR` no longer points at the
  subdirectory.
- `$<BUILD_INTERFACE:...>` is unwrapped and `$<INSTALL_INTERFACE:...>` is
  dropped, instead of both being printed verbatim.
- Output paths use the real build directory instead of a `<build>`
  placeholder, and targets defined in a subdirectory get that subdirectory's
  build path (e.g. `build/test/foo_test`).
- Project files pulled in with `include()`, `option()` defaults and an
  existing `CMakeCache.txt` are taken into account; function and macro bodies
  are skipped; `IMPORTED` libraries with a type are no longer listed as
  targets.

## [0.1.0] - 2026-04-15

### Added

- `cmakex info`: lists include directories, libraries and executables with
  their output paths by parsing `CMakeLists.txt`.
- `cmakex configure`, `cmakex build` (alias `compile`) and `cmakex install`,
  wrapping `cmake -S/-B`, `cmake --build` and `cmake --install`.
- `cmakex clean`: lists what will be removed and asks first (`-y` skips the
  question); `--purge` removes the whole build directory.

[0.2.0]: https://github.com/Raricor/cmakex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Raricor/cmakex/releases/tag/v0.1.0
