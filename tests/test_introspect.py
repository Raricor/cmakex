"""Tests for project analysis, using a real CMake when available."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from cmakex import introspect, process
from cmakex.builder import Cleaner, configure
from cmakex.fileapi import Reply
from cmakex.inputs import SNAPSHOT_NAME, InputSnapshot
from cmakex.introspect import (
    ANALYSIS_DIR_NAME,
    IntrospectError,
    Profile,
    available_profiles,
    inspect_project,
)

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("cc") is None,
    reason="requires cmake and a C compiler",
)


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / "CMakeLists.txt").write_text(
        textwrap.dedent(
            """
            cmake_minimum_required(VERSION 3.14)
            project(demo LANGUAGES C)
            option(DEMO_WITH_TOOL "Build the tool" OFF)
            add_library(core SHARED core.c)
            target_include_directories(core PUBLIC
              $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)
            add_subdirectory(app)
            if(DEMO_WITH_TOOL)
              add_executable(tool app/main.c)
            endif()
            """
        )
    )
    (src / "core.c").write_text("int core(void) { return 1; }\n")
    (src / "app" / "CMakeLists.txt").write_text(
        "add_executable(app main.c)\ntarget_link_libraries(app PRIVATE core)\n"
    )
    (src / "app" / "main.c").write_text("int main(void) { return 0; }\n")
    return src, tmp_path / "build"


def add_presets(src: Path) -> None:
    (src / "CMakePresets.json").write_text(
        json.dumps(
            {
                "version": 3,
                "configurePresets": [
                    {"name": "plain", "binaryDir": "${sourceDir}/out/plain"},
                    {
                        "name": "tools",
                        "binaryDir": "${sourceDir}/out/tools",
                        "cacheVariables": {"DEMO_WITH_TOOL": "ON"},
                    },
                ],
            }
        )
    )


def run_cmake(src: Path, build: Path) -> None:
    assert configure(source_dir=src, build_dir=build) == 0


def analyze(src: Path, build: Path, **kwargs):
    notes = []
    info = inspect_project(src, build, note=notes.append, **kwargs)
    return info, notes


# -- configured build directory --------------------------------------------


def test_configured_build_dir_is_read_without_reconfigure(project):
    src, build = project
    run_cmake(src, build)

    info, notes = analyze(src, build)

    assert notes == []
    assert info.backend == "file-api"
    assert info.analysis_dir is None
    assert info.project_name == "demo"
    assert info.include_dirs == [str(src / "include")]
    [core] = info.shared_libs
    assert core.artifact == build / "libcore.so"
    assert core.defined_at == "CMakeLists.txt:5"
    assert core.sources == ["core.c"]
    [exe] = info.executables
    assert exe.artifact == build / "app" / "app"
    assert exe.source_dir == src / "app"
    assert exe.dependencies == ["core"]
    assert exe.include_dirs == [str(src / "include")]
    assert any(e.name == "DEMO_WITH_TOOL" and e.value == "OFF" for e in info.cache)
    assert [t.language for t in info.toolchains] == ["C"]


def test_missing_reply_triggers_refresh(project):
    src, build = project
    run_cmake(src, build)
    shutil.rmtree(build / ".cmake")

    info, notes = analyze(src, build)

    assert info.backend == "file-api"
    assert len(notes) == 1 and "Re-running CMake configure" in notes[0]


def test_edited_cmake_file_triggers_refresh(project):
    src, build = project
    run_cmake(src, build)
    analyze(src, build)  # Records the MD5 snapshot
    sub = src / "app" / "CMakeLists.txt"
    sub.write_text(sub.read_text() + "add_executable(app2 main.c)\n")

    info, notes = analyze(src, build)

    assert notes == [
        "1 CMake file changed: app/CMakeLists.txt. "
        f"Re-running CMake configure in '{build}'."
    ]
    assert [c.path for c in info.changes] == [sub]
    assert sorted(e.name for e in info.executables) == ["app", "app2"]

    _, notes = analyze(src, build)
    assert notes == []


def test_touching_a_file_without_editing_does_not_reconfigure(project):
    src, build = project
    run_cmake(src, build)
    analyze(src, build)
    future = time.time() + 60
    os.utime(src / "CMakeLists.txt", (future, future))

    info, notes = analyze(src, build)

    assert notes == []
    assert info.changes == []


def test_snapshot_covers_cmake_inputs(project):
    src, build = project

    info, _ = analyze(src, build)

    tree = build / ANALYSIS_DIR_NAME / "default"
    saved = InputSnapshot.load(tree / SNAPSHOT_NAME)
    assert saved is not None and saved == info.inputs
    assert saved.reply == Reply.latest_index(tree).name
    assert sorted(p.relative_to(src).as_posix() for p in saved.files) == [
        "CMakeLists.txt",
        "app/CMakeLists.txt",
    ]


def test_reconfigure_outside_cmakex_is_accepted(project):
    src, build = project
    run_cmake(src, build)
    analyze(src, build)
    subprocess.run(["cmake", "-DDEMO_WITH_TOOL=ON", str(build)], check=True,
                   capture_output=True)

    info, notes = analyze(src, build)

    assert notes == []  # The new reply is fresh; no extra configure
    assert sorted(e.name for e in info.executables) == ["app", "tool"]
    assert info.inputs.reply == Reply.latest_index(build).name


# -- analysis trees ---------------------------------------------------------


def test_unconfigured_build_dir_is_analyzed_in_private_tree(project):
    src, build = project

    info, notes = analyze(src, build)

    tree = build / ANALYSIS_DIR_NAME / "default"
    assert info.backend == "file-api"
    assert info.analysis_dir == tree
    assert "nothing is compiled" in notes[0]
    assert (build / ANALYSIS_DIR_NAME / ".gitignore").read_text() == "*\n"
    # Paths are presented as they will be in the user's build directory.
    assert info.build_dir == build
    assert info.executables[0].artifact == build / "app" / "app"
    assert not (build / "CMakeCache.txt").exists()
    # Nothing was built.
    assert not list(tree.rglob("*.o"))

    _, notes = analyze(src, build)
    assert notes == []  # Second run reuses the analysis


def test_if_branches_follow_the_preset(project):
    src, build = project
    add_presets(src)

    assert [p.name for p in available_profiles(src, build)] == ["plain", "tools", None]

    info, _ = analyze(src, build)  # First preset is the automatic choice
    assert info.preset == "plain"
    assert sorted(e.name for e in info.executables) == ["app"]

    info, _ = analyze(src, build, profile=Profile("preset", "tools"))
    assert info.preset == "tools"
    assert info.analysis_dir == build / ANALYSIS_DIR_NAME / "tools"
    assert sorted(e.name for e in info.executables) == ["app", "tool"]
    assert not (src / "out").exists()  # The preset's binaryDir is not used


def test_editing_presets_triggers_reanalysis(project):
    src, build = project
    add_presets(src)
    analyze(src, build)
    presets = json.loads((src / "CMakePresets.json").read_text())
    presets["configurePresets"][0]["cacheVariables"] = {"DEMO_WITH_TOOL": "ON"}
    (src / "CMakePresets.json").write_text(json.dumps(presets))

    info, notes = analyze(src, build)

    assert notes[0].startswith("1 CMake file changed: CMakePresets.json. Re-analyzing")
    assert sorted(e.name for e in info.executables) == ["app", "tool"]


def test_unknown_preset_is_an_error(project):
    src, build = project
    add_presets(src)

    with pytest.raises(ValueError, match="not found; available: plain, tools"):
        inspect_project(src, build, profile=Profile("preset", "nope"))


def test_configured_build_dir_is_preferred_over_presets(project):
    src, build = project
    add_presets(src)
    run_cmake(src, build)

    assert available_profiles(src, build)[0] == Profile("build")
    info, _ = analyze(src, build)
    assert info.analysis_dir is None


# -- fallbacks and errors ---------------------------------------------------


def test_failing_configure_falls_back_to_static(project):
    src, build = project
    with (src / "CMakeLists.txt").open("a") as fh:
        fh.write('message(FATAL_ERROR "boom")\n')

    info, notes = analyze(src, build)

    assert info.backend == "static"
    assert "CMake configure failed" in notes[-1] and "boom" in notes[-1]
    assert info.executables[0].artifact == build / "app" / "app"
    # The static result is watched too, for the files the parser read.
    assert sorted(info.inputs.files) == [
        (src / "CMakeLists.txt").resolve(),
        (src / "app" / "CMakeLists.txt").resolve(),
    ]


def test_old_cmake_falls_back_to_static(project, monkeypatch):
    src, build = project
    monkeypatch.setattr(introspect, "supports_file_api", lambda cmake: False)

    info, notes = analyze(src, build)

    assert info.backend == "static"
    assert "CMake >= 3.14" in notes[0]


def test_explicit_file_api_backend_does_not_fall_back(project):
    src, build = project

    with pytest.raises(IntrospectError, match="not configured"):
        inspect_project(src, build, backend="file-api", profile=Profile("build"))


def test_unknown_config_is_an_error(project):
    src, build = project
    run_cmake(src, build)

    with pytest.raises(ValueError, match="not found"):
        inspect_project(src, build, config="NoSuchConfig")


def test_clean_ignores_analysis_trees(project):
    src, build = project
    run_cmake(src, build)
    analyze(src, build, profile=Profile("default"))
    assert (build / ANALYSIS_DIR_NAME / "default" / "CMakeFiles").is_dir()

    targets = Cleaner(build).get_clean_targets()

    listed = [*targets.directories, *targets.files]
    assert listed
    assert not any(ANALYSIS_DIR_NAME in p.parts for p in listed)


def test_interrupt_stops_cmake(tmp_path):
    # Ctrl+C during `cmakex info` must not leave CMake running: it has its
    # own session, so the terminal's SIGINT does not reach it.
    src = tmp_path / "src"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\n"
        "project(slow LANGUAGES NONE)\n"
        'message(STATUS "started")\n'
        "execute_process(COMMAND sleep 30)\n"
    )

    def interrupt(line: str) -> None:
        if "started" in line:
            raise KeyboardInterrupt

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        inspect_project(src, tmp_path / "build", on_output=interrupt)
    assert time.monotonic() - start < 10
    assert not process._running
