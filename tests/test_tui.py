"""Smoke tests for the interactive interface, driven headlessly."""

from __future__ import annotations

import asyncio
import io
import shutil
import time

import pytest
from rich.console import Console

from cmakex import introspect, process, tui
from cmakex.introspect import is_configured
from cmakex.tui import CmakexApp, ConfirmScreen, ProfileScreen

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("cc") is None,
    reason="requires cmake and a C compiler",
)


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\n"
        "project(demo LANGUAGES C)\n"
        "add_executable(app main.c)\n"
    )
    (src / "main.c").write_text("int main(void) { return 0; }\n")
    return src, tmp_path / "build"


async def wait_idle(app: CmakexApp, pilot) -> None:
    for _ in range(400):
        await pilot.pause(0.05)
        if not app.busy:
            return
    raise AssertionError("cmakex UI did not become idle")


def detail_text(app: CmakexApp) -> str:
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(app.query_one("#detail").content)
    return console.export_text()


def labels(app: CmakexApp):
    return [str(node.label) for node in app.query_one("#nav").root.children]


def test_startup_analysis_and_actions(project):
    src, build = project

    async def scenario() -> None:
        app = CmakexApp(src, build)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            assert app.info is not None and app.info.backend == "file-api"
            assert app.sub_title == "demo · default config"
            assert labels(app)[:2] == ["Libraries (0)", "Executables (1)"]

            # Build needs a configured build directory first.
            await pilot.press("b")
            assert not is_configured(build)

            await pilot.press("c")
            await wait_idle(app, pilot)  # configure, then re-analysis
            await wait_idle(app, pilot)
            assert is_configured(build)
            assert app.info.analysis_dir is None

            await pilot.press("b")
            await wait_idle(app, pilot)
            assert (build / "app").exists()

            await pilot.press("l")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")

            await pilot.press("p")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ProfileScreen)
            await pilot.press("escape")

    asyncio.run(scenario())


def test_static_fallback_is_shown(project):
    src, build = project
    with (src / "CMakeLists.txt").open("a") as fh:
        fh.write('message(FATAL_ERROR "missing dependency")\n')

    async def scenario() -> None:
        app = CmakexApp(src, build)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            assert app.info is not None and app.info.backend == "static"
            assert app.sub_title == "demo · static parse"

    asyncio.run(scenario())


async def wait_until(pilot, predicate, timeout: float = 20.0) -> None:
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if predicate():
            return
    raise AssertionError("condition not reached")


def exe_names(app: CmakexApp):
    return sorted(e.name for e in app.info.executables)


def test_edits_are_reported_not_reanalyzed(project):
    src, build = project
    cmakelists = src / "CMakeLists.txt"

    async def scenario() -> None:
        app = CmakexApp(src, build, watch_interval=0.1)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            first = app.info
            nav = app.query_one("#nav")
            app_node = nav.root.children[1].children[0]
            nav.move_cursor(app_node)
            await pilot.pause(0.1)

            # Touching without editing is not a change.
            cmakelists.touch()
            await pilot.pause(0.5)
            assert app.info is first and not app._pending

            cmakelists.write_text(cmakelists.read_text() + "add_executable(app2 main.c)\n")
            await wait_until(pilot, lambda: bool(app._pending))
            assert "1 file changed" in app.sub_title
            await pilot.pause(0.5)  # Only reported, not re-analyzed
            assert app.info is first and not app.busy

            await pilot.press("r")
            await wait_until(pilot, lambda: app.info is not first and not app.busy)
            assert exe_names(app) == ["app", "app2"]
            assert app.sub_title == "demo · default config"
            assert app.info.changes and app.info.changes[0].path.name == "CMakeLists.txt"
            # The selected target is still selected after re-analysis.
            await pilot.pause(0.1)
            assert str(nav.cursor_node.label) == "app"

    asyncio.run(scenario())


def test_failed_reanalysis_keeps_previous_results(project, monkeypatch):
    src, build = project
    cmakelists = src / "CMakeLists.txt"
    original = cmakelists.read_text()
    configures = []
    real_configure = introspect._configure

    def counting_configure(*args, **kwargs):
        configures.append(args)
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(introspect, "_configure", counting_configure)

    async def scenario() -> None:
        app = CmakexApp(src, build, watch_interval=0.1)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            first = app.info

            cmakelists.write_text(original + "add_executable(\n")  # Half-edited
            await wait_until(pilot, lambda: bool(app._pending))
            await pilot.press("r")
            await wait_until(pilot, lambda: "configure failed" in app.sub_title)
            assert app.info is first and app.info.backend == "file-api"
            attempts = len(configures)
            await pilot.pause(0.6)  # No retries on its own
            assert not app.busy and "configure failed" in app.sub_title
            assert len(configures) == attempts

            cmakelists.write_text(original + "add_executable(app2 main.c)\n")
            await wait_until(pilot, lambda: "configure failed" not in app.sub_title)
            assert "1 file changed" in app.sub_title and app.info is first
            await pilot.press("r")
            await wait_until(pilot, lambda: app.info is not first and not app.busy)
            assert exe_names(app) == ["app", "app2"]
            assert app.sub_title == "demo · default config"

    asyncio.run(scenario())


def test_actions_require_reanalysis_after_edits(project):
    src, build = project
    cmakelists = src / "CMakeLists.txt"

    async def scenario() -> None:
        # The watcher never runs: actions must check the files themselves.
        app = CmakexApp(src, build, watch_interval=3600)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            await pilot.press("c")
            await wait_until(pilot, lambda: not app.busy and app.info.analysis_dir is None)
            first = app.info
            cmakelists.write_text(cmakelists.read_text() + "add_executable(app2 main.c)\n")

            for key in ("b", "l", "i"):
                await pilot.press(key)
                await pilot.pause(0.1)
                assert isinstance(app.screen, ConfirmScreen)
                assert app.screen._title.endswith("re-analyze first?")
                await pilot.press("n")  # Declining cancels the action
                await pilot.pause(0.2)
                assert not isinstance(app.screen, ConfirmScreen)
            assert not app.busy and app.info is first
            assert not (build / "app").exists()
            assert "1 file changed" in app.sub_title
            assert "Changed (1)" in detail_text(app)

            await pilot.press("b")
            await pilot.pause(0.1)
            await pilot.press("y")  # Re-analyze, then build
            await wait_until(pilot, lambda: (build / "app2").exists() and not app.busy)
            assert exe_names(app) == ["app", "app2"]
            assert "changed" not in app.sub_title
            assert "up to date" in detail_text(app)

            # Up to date again: no question asked.
            await pilot.press("i")
            await pilot.pause(0.1)
            assert app.screen._title == "Install?"
            await pilot.press("n")

    asyncio.run(scenario())


def test_quit_needs_ctrl_c_twice(project, monkeypatch):
    src, build = project
    monkeypatch.setattr(tui, "QUIT_REPEAT_SECONDS", 0.5)

    async def scenario() -> None:
        app = CmakexApp(src, build)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)

            await pilot.press("ctrl+q")  # Left to the editor/terminal
            await pilot.pause(0.1)
            assert app.return_code is None

            await pilot.press("ctrl+c")
            await pilot.pause(0.7)  # Too slow: starts over
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.return_code is None

            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.return_code == 0

    asyncio.run(scenario())


@pytest.fixture
def slow_project(tmp_path):
    """A build that takes 30 s and an install that takes 2 s."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\n"
        "project(slow LANGUAGES NONE)\n"
        'set(CMAKE_INSTALL_PREFIX "${CMAKE_BINARY_DIR}/prefix" CACHE PATH "" FORCE)\n'
        "add_custom_target(slow ALL COMMAND sleep 30)\n"
        'install(CODE "execute_process(COMMAND sleep 2)")\n'
    )
    return src, tmp_path / "build"


def test_ctrl_c_twice_stops_a_running_build(slow_project):
    src, build = slow_project

    async def scenario() -> None:
        app = CmakexApp(src, build)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            await pilot.press("c")
            await wait_idle(app, pilot)
            await pilot.press("b")
            await wait_until(pilot, lambda: bool(process._running))

            await pilot.press("q")  # One key must not kill a build
            await pilot.pause(0.1)
            assert app.return_code is None and process._running

            start = time.monotonic()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.return_code == 0
            assert time.monotonic() - start < 5
            assert not process._running

    asyncio.run(scenario())


def test_install_is_not_interrupted(slow_project):
    src, build = slow_project

    async def scenario() -> None:
        app = CmakexApp(src, build)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_idle(app, pilot)
            await pilot.press("c")
            await wait_idle(app, pilot)
            await pilot.press("i")
            await pilot.pause(0.1)
            await pilot.press("y")
            await wait_until(pilot, lambda: bool(process._running))

            for key in ("ctrl+c", "ctrl+c", "q"):
                await pilot.press(key)
            await pilot.pause(0.1)
            assert app.return_code is None and process._running

            await wait_idle(app, pilot)  # The install completes
            assert (build / "install_manifest.txt").exists()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.return_code == 0

    asyncio.run(scenario())
