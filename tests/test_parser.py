"""Tests for the static CMakeLists.txt parser."""

from __future__ import annotations

import textwrap
from pathlib import Path

from cmakex.parser import parse_cmake_file


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def parse(src: Path, build: Path):
    return parse_cmake_file(src / "CMakeLists.txt", build)


def test_project_name_resolves_in_root_and_subdirectory(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo VERSION 1.2.3 LANGUAGES CXX)
        add_library(${PROJECT_NAME} src/demo.cc)
        add_subdirectory(test)
        """,
    )
    write(src / "test" / "CMakeLists.txt", "add_executable(${PROJECT_NAME}_test t.cc)\n")

    info = parse(src, build)

    assert [lib.name for lib in info.static_libs] == ["demo"]
    assert info.static_libs[0].artifact == build / "libdemo.a"
    [exe] = info.executables
    assert exe.name == "demo_test"
    assert exe.source_dir == src / "test"
    assert exe.artifact == build / "test" / "demo_test"


def test_include_dirs_evaluate_build_interface(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        add_library(demo a.cc)
        target_include_directories(demo
          PUBLIC
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
          PRIVATE
            src
        )
        target_include_directories(demo SYSTEM PUBLIC
          $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>)
        """,
    )

    info = parse(src, build)

    assert info.include_dirs == [
        str(src / "include"),
        str(src / "src"),
        str(build / "include"),
    ]


def test_subdirectory_inherits_output_directory(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
        add_subdirectory(app)
        """,
    )
    write(src / "app" / "CMakeLists.txt", "add_executable(app main.cc)\n")

    info = parse(src, build)

    assert info.executables[0].artifact == build / "bin" / "app"


def test_set_target_properties_from_another_directory(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        add_library(core SHARED core.cc)
        add_subdirectory(cfg)
        """,
    )
    write(
        src / "cfg" / "CMakeLists.txt",
        "set_target_properties(core PROPERTIES OUTPUT_NAME corex"
        " LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)\n",
    )

    info = parse(src, build)

    assert info.shared_libs[0].artifact == build / "lib" / "libcorex.so"


def test_include_file_runs_in_scope_but_function_bodies_do_not(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        include(cmake/Settings.cmake)
        add_library(demo a.cc)
        """,
    )
    write(
        src / "cmake" / "Settings.cmake",
        """
        set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/archive)
        function(make_target name)
          add_library(${name} b.cc)
        endfunction()
        """,
    )

    info = parse(src, build)

    assert [lib.name for lib in info.libraries] == ["demo"]
    assert info.libraries[0].artifact == build / "archive" / "libdemo.a"


def test_build_shared_libs_from_option_and_cache(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        option(BUILD_SHARED_LIBS "Shared" ON)
        add_library(demo a.cc)
        """,
    )
    assert parse(src, build).libraries[0].lib_type == "SHARED"

    # A configured cache value wins over the option() default.
    write(build / "CMakeCache.txt", "BUILD_SHARED_LIBS:BOOL=OFF\n")
    assert parse(src, build).libraries[0].lib_type == "STATIC"


def test_imported_and_alias_targets_are_skipped(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        add_library(ext SHARED IMPORTED)
        add_library(demo a.cc)
        add_library(demo::demo ALIAS demo)
        add_executable(tool IMPORTED)
        """,
    )

    info = parse(src, build)

    assert [lib.name for lib in info.libraries] == ["demo"]
    assert info.executables == []


def test_set_parent_scope_reaches_parent_directory(tmp_path):
    src, build = tmp_path / "src", tmp_path / "build"
    write(
        src / "CMakeLists.txt",
        """
        project(demo)
        add_subdirectory(sub)
        add_library(${SUB_LIB_NAME} a.cc)
        """,
    )
    write(src / "sub" / "CMakeLists.txt", "set(SUB_LIB_NAME from_sub PARENT_SCOPE)\n")

    info = parse(src, build)

    assert [lib.name for lib in info.libraries] == ["from_sub"]
