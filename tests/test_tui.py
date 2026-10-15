"""Smoke tests for the interactive interface, driven headlessly."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from cmakex import introspect
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


def test_edits_are_detected_and_reanalyzed(project):
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
            await wait_until(pilot, lambda: "configure failed" in app.sub_title)
            assert app.info is first and app.info.backend == "file-api"
            attempts = len(configures)
            await pilot.pause(0.6)  # No retry loop for the same contents
            assert not app.busy and "configure failed" in app.sub_title
            assert len(configures) == attempts

            cmakelists.write_text(original + "add_executable(app2 main.c)\n")
            await wait_until(pilot, lambda: app.info is not first and not app.busy)
            assert exe_names(app) == ["app", "app2"]
            assert app.sub_title == "demo · default config"

    asyncio.run(scenario())
