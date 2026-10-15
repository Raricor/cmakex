"""Interactive full-screen interface (``cmakex`` / ``cmakex ui``)."""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from . import process
from .builder import Cleaner, build, clean, configure, install, uninstall
from .fileapi import Reply
from .inputs import Change, describe_changes
from .introspect import (
    IntrospectError,
    Profile,
    available_profiles,
    describe_backend,
    inspect_project,
    is_configured,
)
from .models import CacheEntry, CMakeInfo, Target, Toolchain
from .parser.parser import load_cache

# Cache entry types worth showing; INTERNAL/STATIC are CMake bookkeeping.
_USER_CACHE_TYPES = ("BOOL", "STRING", "PATH", "FILEPATH", "UNINITIALIZED")
_MAX_SOURCES = 50
#: A second ctrl+c within this many seconds quits.
QUIT_REPEAT_SECONDS = 2.0
#: Stopping this action midway would leave a partial installation.
_UNSTOPPABLE = "Install"

NodeData = Tuple[str, Any]


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _key_values(rows: List[Tuple[str, Any]]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    for key, value in rows:
        table.add_row(key, value)
    return table


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no dialog."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(Text(self._body))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Yes (y)", variant="primary", id="yes")
                yield Button("No (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class ProfileScreen(ModalScreen[Optional[Profile]]):
    """Pick which build tree to analyze."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, profiles: List[Profile]):
        super().__init__()
        self._profiles = profiles

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Analyze with", classes="dialog-title")
            yield OptionList(*(p.label for p in self._profiles))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._profiles[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


# Textual's own app bindings are not inherited: its ctrl+q quit is often
# taken by editors and terminals, so ctrl+c pressed twice quits instead.
class CmakexApp(App[None], inherit_bindings=False):
    """Browse an analyzed CMake project and run build actions on it."""

    TITLE = "cmakex"
    CSS = """
    #loading { height: 1fr; align: center middle; }
    #loading LoadingIndicator { height: 3; }
    #status { width: 100%; height: 3; content-align: center middle; color: $text-muted; }
    #main { height: 1fr; }
    #nav { width: 2fr; max-width: 60; border: round $primary; }
    #detail-pane { width: 3fr; border: round $primary; padding: 0 1; }
    #log { height: 10; border: round $secondary; }
    ConfirmScreen, ProfileScreen { align: center middle; }
    .dialog { width: 72; height: auto; max-height: 80%; padding: 1 2;
              border: thick $primary; background: $surface; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    .dialog-buttons Button { margin-left: 2; }
    .dialog OptionList { height: auto; max-height: 20; }
    """

    BINDINGS = [
        Binding("c", "configure", "Configure"),
        Binding("b", "build", "Build"),
        Binding("l", "clean", "Clean"),
        Binding("i", "install", "Install"),
        Binding("r", "analyze", "Re-analyze"),
        Binding("p", "profile", "Preset"),
        Binding("g", "toggle_log", "Log"),
        Binding("q", "quit", "Quit"),
        # Not a priority binding: with text selected, ctrl+c still copies it.
        Binding("ctrl+c", "quit_on_repeat", "Quit", show=False),
    ]

    def __init__(self, source_dir: Path, build_dir: Path, watch_interval: float = 1.0):
        super().__init__()
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.watch_interval = watch_interval
        self.profile: Optional[Profile] = None  # None: automatic choice
        self.info: Optional[CMakeInfo] = None
        self._info_profile: Optional[Profile] = None  # Profile self.info came from
        self.busy = False
        self._action: Optional[str] = None  # Title of the running action
        # CMake files changed since self.info was produced. They are only
        # reported; re-analysis waits for the user (r, or agreeing to it
        # before an action).
        self._pending: List[Change] = []
        self._pending_key: Optional[tuple] = None
        # Changes whose re-analysis failed.
        self._failed_key: Optional[tuple] = None
        # Action to continue with once the current re-analysis succeeds.
        self._after_analysis: Optional[Tuple[str, Callable[[], None]]] = None
        self._detail: Optional[NodeData] = None  # What the details pane shows
        self._quit_pressed_at: Optional[float] = None

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="loading"):
            yield LoadingIndicator()
            yield Static("Analyzing…", id="status")
        with Horizontal(id="main"):
            yield Tree("project", id="nav")
            with VerticalScroll(id="detail-pane"):
                yield Static(id="detail")
        yield RichLog(id="log", wrap=True, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.source_dir)
        nav = self.query_one("#nav", Tree)
        nav.show_root = False
        nav.border_title = "Project"
        self.query_one("#detail-pane").border_title = "Details"
        log = self.query_one("#log", RichLog)
        log.border_title = "Log"
        log.display = False  # Shown once there is output
        self.set_interval(self.watch_interval, self._watch_tick)
        self.action_analyze()

    def _set_loading(self, loading: bool, status: str = "") -> None:
        self.query_one("#loading").display = loading
        self.query_one("#main").display = not loading
        if status:
            self.query_one("#status", Static).update(status)

    def _log(self, line: Any) -> None:
        log = self.query_one("#log", RichLog)
        log.display = True
        log.write(line)

    # -- analysis ---------------------------------------------------------

    def action_analyze(self) -> None:
        if not self._claim():
            return
        # Refreshing exact results: if configure now fails (often a half-edited
        # CMakeLists.txt), keep showing them instead of a static fallback.
        refresh = (
            self.info is not None
            and self.info.backend == "file-api"
            and self._info_profile == self.profile
        )
        if refresh:
            self._update_subtitle("re-analyzing…")
        else:
            self._set_loading(True, "Analyzing…")
        self._analyze(self.profile, "file-api" if refresh else "auto")

    @work(thread=True, exclusive=True, group="analysis")
    def _analyze(self, profile: Optional[Profile], backend: str) -> None:
        def note(message: str) -> None:
            self.call_from_thread(self._log, Text(message, style="yellow"))

        def on_output(line: str) -> None:
            self.call_from_thread(self._on_cmake_output, line)

        try:
            info = inspect_project(
                self.source_dir,
                self.build_dir,
                backend=backend,
                profile=profile,
                note=note,
                on_output=on_output,
            )
        except (IntrospectError, ValueError) as exc:
            self.call_from_thread(self._analysis_failed, str(exc))
        else:
            self.call_from_thread(self._show_info, info, profile)

    def _on_cmake_output(self, line: str) -> None:
        self._log(line)
        self.query_one("#status", Static).update(Text(f"Analyzing…\n{line}"))

    def _analysis_failed(self, message: str) -> None:
        self.busy = False
        follow_up, self._after_analysis = self._after_analysis, None
        cancelled = f" {follow_up[0]} cancelled." if follow_up else ""
        self._set_loading(False)
        self._log(Text(f"Error: {message}", style="bold red"))
        if self.info is not None and self._pending:
            self._failed_key = self._pending_key
            self.notify(
                "CMake configure failed; still showing the previous analysis. "
                f"Fix the CMake files and press r to retry (see the log).{cancelled}",
                title="Re-analysis failed",
                severity="error",
            )
        else:
            self.notify(message + cancelled, title="Analysis failed", severity="error")
        self._update_subtitle()

    def _show_info(self, info: CMakeInfo, profile: Optional[Profile]) -> None:
        self.busy = False
        follow_up, self._after_analysis = self._after_analysis, None
        self.info = info
        self._info_profile = profile
        startup_changes = info.changes and not self._pending
        self._pending, self._pending_key, self._failed_key = [], None, None
        self._set_loading(False)
        self._update_subtitle()
        if info.backend != "file-api":
            self.notify(
                "CMake configure was not possible; showing best-effort static "
                "results. See the log for details.",
                severity="warning",
            )
        elif startup_changes:
            self.notify(
                f"{describe_changes(info.changes, self.source_dir)}.",
                title="Analysis updated",
            )
        self._populate_tree(info)
        if follow_up is not None:
            title, then = follow_up
            if info.backend == "file-api":
                then()
            else:
                self.notify(f"{title} cancelled: CMake configure failed.", severity="error")

    def _update_subtitle(self, activity: str = "") -> None:
        info = self.info
        if info is None:
            return
        if info.backend != "file-api":
            source = "static parse"
        elif info.preset:
            source = f"preset {info.preset}"
        elif info.analysis_dir:
            source = "default config"
        else:
            source = _display(self.build_dir)
        parts = [info.project_name or self.source_dir.name, source]
        if self._pending and self._failed_key == self._pending_key:
            parts.append("⚠ configure failed")
        elif self._pending:
            parts.append(f"⚠ {_plural(len(self._pending), 'file')} changed, press r to re-analyze")
        if activity:
            parts.append(activity)
        self.sub_title = " · ".join(parts)

    # -- change detection -------------------------------------------------

    def _watch_tick(self) -> None:
        if (
            not self.busy
            and not isinstance(self.screen, ModalScreen)
            and self.info is not None
            and self.info.inputs is not None
        ):
            self._check_inputs(self.info)

    @work(thread=True, exclusive=True, group="watch")
    def _check_inputs(self, info: CMakeInfo) -> None:
        assert info.inputs is not None
        changes = info.inputs.changes()
        reconfigured = False
        if info.backend == "file-api":
            index = Reply.latest_index(info.analysis_dir or self.build_dir)
            reconfigured = index is not None and index.name != info.inputs.reply
        self.call_from_thread(self._on_inputs_checked, info, changes, reconfigured)

    def _on_inputs_checked(
        self, info: CMakeInfo, changes: List[Change], reconfigured: bool
    ) -> None:
        if info is not self.info or self.busy:
            return  # Outdated result
        if self._update_pending(changes):
            self.notify(
                f"{describe_changes(changes, self.source_dir)}. Press r to re-analyze.",
                title="Analysis out of date",
                severity="warning",
            )
        elif reconfigured and not changes:
            # CMake already did the work; this only reads its fresh output.
            self._log(Text("CMake was re-run outside cmakex; reloading.", style="yellow"))
            self.action_analyze()

    def _update_pending(self, changes: List[Change]) -> bool:
        """Record the files changed since the analysis; True if newly out of date."""
        key = tuple((str(c.path), c.md5) for c in changes) or None
        if key == self._pending_key:
            return False
        newly = bool(changes) and not self._pending
        if newly:
            self._log(Text(describe_changes(changes, self.source_dir), style="yellow"))
        self._pending, self._pending_key = changes, key
        if not changes:
            self._failed_key = None  # The edits were reverted
        self._update_subtitle()
        self._refresh_detail()
        return newly

    @staticmethod
    def _node_key(node: Optional[TreeNode]) -> Optional[tuple]:
        """Identity of a node that survives re-analysis."""
        if node is None or node.data is None:
            return None
        kind, obj = node.data
        if kind == "overview":
            return (kind, str(node.label).split(" (")[0])
        return (kind, getattr(obj, "name", None) or getattr(obj, "language", None) or obj)

    def _populate_tree(self, info: CMakeInfo) -> None:
        nav = self.query_one("#nav", Tree)
        selected = self._node_key(nav.cursor_node)
        nav.clear()
        root = nav.root
        root.data = ("overview", info)

        def section(title: str, count: int) -> TreeNode:
            return root.add(f"{title} ({count})", data=("overview", info), expand=True)

        libs = section("Libraries", len(info.libraries))
        for lib in info.libraries:
            libs.add_leaf(
                Text.assemble(lib.name, (f"  {lib.lib_type.lower()}", "dim")),
                data=("target", lib),
            )
        exes = section("Executables", len(info.executables))
        for exe in info.executables:
            exes.add_leaf(Text(exe.name), data=("target", exe))

        incs = section("Include directories", len(info.include_dirs))
        for d in info.include_dirs:
            incs.add_leaf(Text(_display(Path(d))), data=("include", d))

        options = [
            e for e in info.cache if not e.advanced and e.type in _USER_CACHE_TYPES
        ]
        if options:
            cache = section("Cache options", len(options))
            for e in sorted(options, key=lambda e: e.name):
                cache.add_leaf(
                    Text.assemble(e.name, (f" = {e.value}", "dim")), data=("cache", e)
                )
        if info.toolchains:
            tools = section("Compilers", len(info.toolchains))
            for t in info.toolchains:
                tools.add_leaf(
                    Text(f"{t.language}: {t.compiler_id} {t.version}"),
                    data=("toolchain", t),
                )

        self._show_detail(("overview", info))
        nav.focus()
        if selected is not None:
            for section_node in root.children:
                for node in (section_node, *section_node.children):
                    if self._node_key(node) == selected:
                        # Node lines are assigned when the tree is next rendered.
                        self.call_after_refresh(self._select_node, node)
                        return

    def _select_node(self, node: TreeNode) -> None:
        self.query_one("#nav", Tree).move_cursor(node)
        if node.data is not None:
            self._show_detail(node.data)

    # -- details ----------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self._show_detail(event.node.data)

    def _show_detail(self, data: NodeData) -> None:
        self._detail = data
        kind, obj = data
        renderers: dict = {
            "overview": self._render_overview,
            "target": self._render_target,
            "include": self._render_include,
            "cache": self._render_cache,
            "toolchain": self._render_toolchain,
        }
        self.query_one("#detail", Static).update(renderers[kind](obj))

    def _refresh_detail(self) -> None:
        """Re-render the details pane, e.g. after the analysis went stale."""
        # Not the tree's cursor node: there is none until the user moves it.
        if self._detail is not None:
            self._show_detail(self._detail)

    def _render_overview(self, info: CMakeInfo) -> Any:
        rows: List[Tuple[str, Any]] = [
            ("Project", info.project_name or "?"),
            ("Source", str(info.source_dir)),
            ("Build dir", _display(self.build_dir)),
            ("Configured", "yes" if is_configured(self.build_dir) else "no (press c)"),
            ("Backend", describe_backend(info)),
            ("Targets", f"{_plural(len(info.libraries), 'library', 'libraries')}, "
                        f"{_plural(len(info.executables), 'executable')}"),
        ]
        for t in info.toolchains:
            rows.append((f"{t.language} compiler", f"{t.compiler_id} {t.version}"))
        tracked = sorted(info.inputs.files) if info.inputs else []
        if self._pending and self._failed_key == self._pending_key:
            status = Text("configure failed for the current changes (see log)", style="red")
        elif self._pending:
            status = Text("out of date, CMake files changed (press r)", style="yellow")
        else:
            status = Text("up to date", style="green")
        rows.append(("Analysis", status))
        parts: List[Any] = [_key_values(rows)]
        if self._pending:
            parts.append(Text(f"\nChanged ({len(self._pending)})", style="bold yellow"))
            for c in self._pending:
                suffix = "" if c.kind == "modified" else f" ({c.kind})"
                parts.append(Text(f"  {_display(c.path)}{suffix}", style="yellow"))
        if tracked:
            parts.append(Text(f"\nWatched CMake files ({len(tracked)}, MD5)", style="bold"))
            own = [p for p in tracked if p.is_relative_to(info.source_dir)]
            external = [p for p in tracked if p not in own]
            parts += self._path_list(own, "")
            if external:
                parts.append(Text(f"  outside the project ({len(external)}):", style="dim"))
                parts += self._path_list(external, "dim")
        return Group(*parts)

    @staticmethod
    def _path_list(paths: List[Path], style: str) -> List[Text]:
        lines = [Text(f"  {_display(p)}", style=style) for p in paths[:_MAX_SOURCES]]
        if len(paths) > _MAX_SOURCES:
            lines.append(Text(f"  … and {len(paths) - _MAX_SOURCES} more", style="dim"))
        return lines

    def _render_target(self, t: Target) -> Any:
        kind = getattr(t, "lib_type", "EXECUTABLE")
        if t.artifact is None:
            output: Any = "—"
        else:
            state = ("built", "green") if t.artifact.exists() else ("not built", "dim")
            output = Text.assemble(_display(t.artifact), "  ", (f"({state[0]})", state[1]))
        rows: List[Tuple[str, Any]] = [
            ("Target", Text(t.name, style="bold")),
            ("Type", kind),
            ("Output", output),
        ]
        if t.defined_at:
            rows.append(("Defined at", t.defined_at))
        if t.dependencies:
            rows.append(("Depends on", ", ".join(t.dependencies)))
        parts: List[Any] = [_key_values(rows)]
        if t.include_dirs:
            parts += [Text("\nInclude directories", style="bold")]
            parts += [Text(f"  {_display(Path(d))}") for d in t.include_dirs]
        if t.sources:
            parts += [Text(f"\nSources ({len(t.sources)})", style="bold")]
            parts += [Text(f"  {s}") for s in t.sources[:_MAX_SOURCES]]
            if len(t.sources) > _MAX_SOURCES:
                parts.append(Text(f"  … and {len(t.sources) - _MAX_SOURCES} more", style="dim"))
        return Group(*parts)

    def _render_include(self, path: str) -> Any:
        users = [
            t.name
            for t in [*self.info.libraries, *self.info.executables]  # type: ignore[union-attr]
            if path in t.include_dirs
        ]
        return _key_values(
            [
                ("Directory", path),
                ("Exists", "yes" if Path(path).is_dir() else "no (generated at build time?)"),
                ("Used by", ", ".join(users) or "—"),
            ]
        )

    def _render_cache(self, e: CacheEntry) -> Any:
        return _key_values(
            [("Name", Text(e.name, style="bold")), ("Type", e.type),
             ("Value", e.value or '""'), ("Help", e.help or "—")]
        )

    def _render_toolchain(self, t: Toolchain) -> Any:
        return _key_values(
            [("Language", t.language), ("Compiler", t.compiler_id),
             ("Version", t.version), ("Path", t.path)]
        )

    # -- actions ----------------------------------------------------------

    def _claim(self) -> bool:
        """Mark the app busy; False (with a hint) if something is running."""
        if self.busy:
            self.notify("Another operation is still running.", severity="warning")
            return False
        self.busy = True
        return True

    def _run_command(self, cmd: List[str]) -> int:
        """Runner for builder operations: streams output into the log."""
        self.call_from_thread(self._log, Text(f"$ {shlex.join(cmd)}", style="bold"))
        proc = process.spawn(cmd)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                self.call_from_thread(self._log, line.rstrip())
        finally:
            rc = process.finish(proc)
        return rc

    def _run_action(self, title: str, action: Callable[[], int], reanalyze: bool) -> None:
        self._action = title
        self._action_worker(title, action, reanalyze)

    @work(thread=True, exclusive=True, group="action")
    def _action_worker(self, title: str, action: Callable[[], int], reanalyze: bool) -> None:
        try:
            rc = action()
        except (OSError, ValueError) as exc:
            rc = 1
            self.call_from_thread(self._log, Text(f"Error: {exc}", style="bold red"))
        self.call_from_thread(self._action_done, title, rc, reanalyze)

    def _action_done(self, title: str, rc: int, reanalyze: bool) -> None:
        self.busy = False
        self._action = None
        if rc == 0:
            self.notify(f"{title} finished.")
        else:
            self.notify(f"{title} failed (exit {rc}); see the log.", severity="error")
        if reanalyze and rc == 0:
            self.action_analyze()
        else:
            self._refresh_detail()  # e.g. a target's "built" state

    def _require_configured(self) -> bool:
        if is_configured(self.build_dir):
            return True
        self.notify(
            f"'{_display(self.build_dir)}' is not configured yet; press c first.",
            severity="warning",
        )
        return False

    def _when_current(self, title: str, then: Callable[[], None]) -> None:
        """Run *then* if the analysis matches the CMake files on disk.

        Otherwise the user must agree to re-analyze first; *then* runs after
        a successful re-analysis, and declining cancels it.
        """
        if self.busy:
            self.notify("Another operation is still running.", severity="warning")
            return
        inputs = self.info.inputs if self.info is not None else None
        # Check now rather than trusting the watcher, which may not have run
        # since the last edit.
        self._update_pending(inputs.changes() if inputs is not None else [])
        if not self._pending:
            then()
            return

        def answered(ok: Optional[bool]) -> None:
            if not ok:
                self.notify(f"{title} cancelled: the analysis is out of date.",
                            severity="warning")
                return
            self._after_analysis = (title, then)
            self.action_analyze()

        body = (
            f"{describe_changes(self._pending, self.source_dir)}.\n\n"
            f"The analysis no longer matches the project. Re-analyze now and "
            f"then continue? No cancels the {title.lower()}."
        )
        self.push_screen(ConfirmScreen(f"{title}: re-analyze first?", body), answered)

    def action_configure(self) -> None:
        if not self._claim():
            return
        # Configure the way the analysis did, so the results stay accurate.
        args: Optional[List[str]] = None
        if self.info is not None and self.info.preset:
            args = ["--preset", self.info.preset]
        elif self.info is not None and self.info.analysis_dir is not None:
            args = ["-DCMAKE_BUILD_TYPE=Debug"]
        self._log(Text(f"Configuring {_display(self.build_dir)}", style="bold green"))
        # Afterwards, analyze the freshly configured build directory itself.
        self.profile = None
        self._run_action(
            "Configure",
            lambda: configure(self.source_dir, self.build_dir, cmake_args=args,
                              runner=self._run_command),
            True,
        )

    def action_build(self) -> None:
        if self._require_configured():
            self._when_current("Build", self._build)

    def _build(self) -> None:
        if self._claim():
            self._run_action(
                "Build", lambda: build(self.build_dir, runner=self._run_command), False
            )

    def action_clean(self) -> None:
        if self._require_configured():
            self._when_current("Clean", self._clean)

    def _clean(self) -> None:
        targets = Cleaner(self.build_dir).get_clean_targets()
        installed = len(targets.installed_dirs) + len(targets.installed_files)
        body = f"Run the clean target in '{_display(self.build_dir)}' ({targets.total_file_count} files)."
        if installed:
            body += f"\nAlso uninstall {installed} installed entries listed in install_manifest.txt."

        def do_clean() -> int:
            rc = clean(self.build_dir, runner=self._run_command)
            if rc == 0 and installed:
                uninstall(self.build_dir)
            return rc

        def confirmed(ok: Optional[bool]) -> None:
            if ok and self._claim():
                self._run_action("Clean", do_clean, False)

        self.push_screen(ConfirmScreen("Clean build artifacts?", body), confirmed)

    def action_install(self) -> None:
        if self._require_configured():
            self._when_current("Install", self._install)

    def _install(self) -> None:
        # Read after any re-analysis: the prefix may have changed.
        prefix = load_cache(self.build_dir).get("CMAKE_INSTALL_PREFIX", "?")

        def confirmed(ok: Optional[bool]) -> None:
            if ok and self._claim():
                self._run_action(
                    "Install",
                    lambda: install(self.build_dir, runner=self._run_command),
                    False,
                )

        self.push_screen(
            ConfirmScreen("Install?", f"Install '{_display(self.build_dir)}' into {prefix}."),
            confirmed,
        )

    def action_profile(self) -> None:
        if self.busy:
            self.notify("Another operation is still running.", severity="warning")
            return

        def chosen(profile: Optional[Profile]) -> None:
            if profile is not None:
                self.profile = profile
                self.action_analyze()

        self.push_screen(ProfileScreen(available_profiles(self.source_dir, self.build_dir)), chosen)

    def _activity(self) -> Optional[str]:
        """What is running, e.g. "build" or "analysis"; None if idle."""
        if self._action is not None:
            return self._action.lower()
        return "analysis" if self.busy else None

    def _refuse_quit(self) -> bool:
        """Notify and return True if quitting now would do harm."""
        if self._action != _UNSTOPPABLE:
            return False
        self.notify(
            f"{_UNSTOPPABLE} is still running; stopping it now would leave a "
            "partial installation. Quit once it has finished.",
            severity="warning",
        )
        return True

    async def action_quit(self) -> None:
        # A single key should not kill a long build: that takes Ctrl+C twice.
        running = self._activity()
        if running is None:
            self.exit()
        elif not self._refuse_quit():
            self.notify(
                f"The {running} is still running; press Ctrl+C twice to stop it and quit.",
                severity="warning",
            )

    def action_quit_on_repeat(self) -> None:
        if self._refuse_quit():
            self._quit_pressed_at = None
            return
        now = time.monotonic()
        if (
            self._quit_pressed_at is not None
            and now - self._quit_pressed_at <= QUIT_REPEAT_SECONDS
        ):
            # Otherwise exiting waits for CMake, which can take a whole build.
            process.stop_all()
            self.exit()
            return
        self._quit_pressed_at = now
        running = self._activity()
        what = f"stop the {running} and quit" if running else "quit"
        self.notify(f"Press Ctrl+C again to {what}.", timeout=QUIT_REPEAT_SECONDS)

    def action_toggle_log(self) -> None:
        log = self.query_one("#log", RichLog)
        log.display = not log.display
