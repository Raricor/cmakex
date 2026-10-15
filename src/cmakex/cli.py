"""cli.py — Click command-line interface for cmakex."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence, Union

import click

from .builder import Cleaner, build, clean, configure, install, purge, uninstall
from .introspect import (
    BACKENDS,
    IntrospectError,
    Profile,
    describe_backend,
    inspect_project,
)
from .models import ExecutableInfo, LibraryInfo

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(package_name="cmakex")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """cmakex — CMake build eXtension.

    Wraps common CMake workflows and provides project introspection.
    Run without a command to open the interactive interface (same as
    'cmakex ui').

    \b
    Typical workflow:
      cmakex info
      cmakex configure -B build -D CMAKE_BUILD_TYPE=Release
      cmakex build -B build -j8
      cmakex clean -B build
    """
    if ctx.invoked_subcommand is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            click.echo(ctx.get_help())
            return
        ctx.invoke(cmd_ui)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_cmake_file(source_dir: str) -> Path:
    path = Path(source_dir) / "CMakeLists.txt"
    if not path.exists():
        click.echo(f"Error: CMakeLists.txt not found in '{source_dir}'", err=True)
        sys.exit(1)
    return path


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@cli.command("info")
@click.option(
    "-S",
    "--source-dir",
    default=".",
    show_default=True,
    help="Source directory containing CMakeLists.txt.",
)
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory to read CMake File API data from.",
)
@click.option(
    "--backend",
    type=click.Choice(BACKENDS),
    default="auto",
    show_default=True,
    help="Where to get project information: the CMake File API, static "
    "CMakeLists.txt parsing, or the File API with a static fallback.",
)
@click.option(
    "--config",
    default=None,
    help="Configuration to show for multi-config generators (File API only).",
)
@click.option(
    "--preset",
    default=None,
    help="Analyze with this configure preset instead of the build directory.",
)
def cmd_info(
    source_dir: str,
    build_dir: str,
    backend: str,
    config: Optional[str],
    preset: Optional[str],
) -> None:
    """Show include directories and library/executable output paths.

    Results come from the CMake File API (CMake >= 3.14), so if() branches,
    functions and find_package() are evaluated exactly as CMake does:

    \b
      - a configured BUILD_DIR is read directly (refreshed if CMake files
        changed);
      - otherwise cmakex configures a private analysis tree in
        BUILD_DIR/.cmakex/ using the first configure preset, or CMake
        defaults (Debug) without presets. Nothing is compiled.

    If configure fails (e.g. a missing dependency), falls back to parsing
    CMakeLists.txt, which is best-effort.

    \b
    Lists:
      - Include directories
      - Shared libraries (.so)
      - Static libraries (.a)
      - Executables
    """
    cmake_file = _require_cmake_file(source_dir)

    def note(message: str) -> None:
        click.echo(click.style("Note: ", fg="yellow") + message, err=True)

    try:
        info = inspect_project(
            cmake_file.parent,
            Path(build_dir),
            backend,
            config,
            Profile("preset", preset) if preset else None,
            note,
        )
    except (IntrospectError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    root_dir = cmake_file.parent.resolve()
    click.echo(f"Source : {root_dir}")
    click.echo(f"Build  : {info.build_dir}")
    click.echo(f"Backend: {describe_backend(info)}")
    click.echo()

    # Include directories
    click.echo(click.style("Include Directories:", bold=True))
    if info.include_dirs:
        for d in info.include_dirs:
            click.echo(f"  {d}")
    else:
        click.echo("  (none detected)")
    click.echo()

    _echo_targets("Shared Libraries (.so):", info.shared_libs, root_dir)
    click.echo()
    _echo_targets("Static Libraries (.a):", info.static_libs, root_dir)
    click.echo()
    _echo_targets("Executables:", info.executables, root_dir)


def _relative_to(path: Path, base: Path) -> Optional[Path]:
    try:
        return path.relative_to(base)
    except ValueError:
        return None


def _echo_targets(
    title: str,
    targets: Sequence[Union[LibraryInfo, ExecutableInfo]],
    root_dir: Path,
) -> None:
    click.echo(click.style(title, bold=True))
    if not targets:
        click.echo("  (none detected)")
        return

    cwd = Path.cwd()
    for t in targets:
        if t.artifact is None:
            out = t.filename()
        else:
            out = str(_relative_to(t.artifact, cwd) or t.artifact)
        src_info = ""
        if t.source_dir and t.source_dir != root_dir:
            src_info = f"  [{_relative_to(t.source_dir, root_dir) or t.source_dir}]"
        click.echo(f"  {t.name:<30}  {out}{src_info}")


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------


@cli.command("configure")
@click.option(
    "-S",
    "--source-dir",
    default=".",
    show_default=True,
    help="Source directory containing CMakeLists.txt.",
)
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory to create.",
)
@click.option(
    "-G",
    "--generator",
    default=None,
    metavar="GEN",
    help='CMake generator, e.g. "Ninja" or "Unix Makefiles".',
)
@click.option(
    "-D",
    "defines",
    multiple=True,
    metavar="KEY=VALUE",
    help="Set a CMake cache variable (repeatable).",
)
@click.option(
    "--preset",
    default=None,
    help="Use this configure preset (its binaryDir is replaced by -B).",
)
@click.option("-v", "--verbose", is_flag=True, help="Print the cmake command.")
@click.argument("extra", nargs=-1)
def cmd_configure(
    source_dir: str,
    build_dir: str,
    generator: Optional[str],
    defines: tuple,
    preset: Optional[str],
    verbose: bool,
    extra: tuple,
) -> None:
    """Create build directory and run cmake configuration.

    Extra arguments after -- are forwarded verbatim to cmake.

    \b
    Examples:
      cmakex configure
      cmakex configure -B build -G Ninja -D CMAKE_BUILD_TYPE=Release
      cmakex configure --preset release
      cmakex configure -- -DCMAKE_VERBOSE_MAKEFILE=ON
    """
    source = Path(source_dir).resolve()
    if not (source / "CMakeLists.txt").exists():
        click.echo(f"Error: CMakeLists.txt not found in '{source}'", err=True)
        sys.exit(1)

    cmake_args = ["--preset", preset] if preset else []
    cmake_args += [f"-D{d}" for d in defines]
    cmake_args += list(extra)

    try:
        rc = configure(
            source_dir=source,
            build_dir=Path(build_dir),
            generator=generator,
            cmake_args=cmake_args or None,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    sys.exit(rc)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@cli.command("build")
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory.",
)
@click.option(
    "-j",
    "--jobs",
    default=None,
    type=int,
    metavar="N",
    help="Parallel jobs (default: auto).",
)
@click.option("-t", "--target", default=None, help="Build target.")
@click.option(
    "--config",
    default=None,
    help="Build configuration, e.g. Release or Debug.",
)
@click.option("-v", "--verbose", is_flag=True, help="Print the cmake command.")
@click.argument("extra", nargs=-1)
def cmd_build(
    build_dir: str,
    jobs: Optional[int],
    target: Optional[str],
    config: Optional[str],
    verbose: bool,
    extra: tuple,
) -> None:
    """Build the project.

    Extra arguments after -- are forwarded to the underlying build tool
    (make, ninja, …).

    \b
    Examples:
      cmakex build
      cmakex build -j8
      cmakex build -t my_library --config Release
      cmakex build -- VERBOSE=1
    """
    bd = Path(build_dir)
    if not bd.exists():
        click.echo(
            f"Error: build directory '{build_dir}' does not exist. "
            "Run 'cmakex configure' first.",
            err=True,
        )
        sys.exit(1)

    try:
        rc = build(
            build_dir=bd,
            jobs=jobs,
            target=target,
            config=config,
            extra_args=list(extra) or None,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    sys.exit(rc)


# ---------------------------------------------------------------------------
# compile  (alias for build)
# ---------------------------------------------------------------------------


@cli.command("compile")
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory.",
)
@click.option(
    "-j",
    "--jobs",
    default=None,
    type=int,
    metavar="N",
    help="Parallel jobs (default: auto).",
)
@click.option("-t", "--target", default=None, help="Build target.")
@click.option(
    "--config",
    default=None,
    help="Build configuration, e.g. Release or Debug.",
)
@click.option("-v", "--verbose", is_flag=True, help="Print the cmake command.")
@click.argument("extra", nargs=-1)
def cmd_compile(
    build_dir: str,
    jobs: Optional[int],
    target: Optional[str],
    config: Optional[str],
    verbose: bool,
    extra: tuple,
) -> None:
    """Compile the project (alias for 'build').

    \b
    Examples:
      cmakex compile
      cmakex compile -j$(nproc)
    """
    bd = Path(build_dir)
    if not bd.exists():
        click.echo(
            f"Error: build directory '{build_dir}' does not exist. "
            "Run 'cmakex configure' first.",
            err=True,
        )
        sys.exit(1)

    try:
        rc = build(
            build_dir=bd,
            jobs=jobs,
            target=target,
            config=config,
            extra_args=list(extra) or None,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    sys.exit(rc)


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


@cli.command("clean")
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory.",
)
@click.option(
    "--purge",
    "do_purge",
    is_flag=True,
    help="Remove the entire build directory instead of running the clean target.",
)
@click.option("-v", "--verbose", is_flag=True, help="Print the cmake command.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
def cmd_clean(build_dir: str, do_purge: bool, verbose: bool, yes: bool) -> None:
    """Clean build artifacts.

    By default runs ``cmake --build <build_dir> --target clean``.
    Use --purge to delete the entire build directory.

    \b
    Examples:
      cmakex clean
      cmakex clean --purge
      cmakex clean -y  # Skip confirmation
    """
    bd = Path(build_dir)

    if not bd.exists():
        click.echo(f"Build directory '{build_dir}' does not exist, nothing to clean.")
        return

    # Get list of files/directories that will be removed
    try:
        cleaner = Cleaner(bd)
        if do_purge:
            clean_info = cleaner.get_purge_targets()
        else:
            clean_info = cleaner.get_clean_targets()
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Display what will be removed
    if clean_info.directories or clean_info.files or clean_info.installed_dirs or clean_info.installed_files:
        click.echo(click.style("The following will be removed:", bold=True))
        click.echo()

        if clean_info.directories:
            click.echo(click.style("Directories:", fg="yellow"))
            for d in clean_info.directories:
                click.echo(f"  {d}")
            click.echo()

        if clean_info.files:
            click.echo(
                click.style(f"Files ({len(clean_info.files)} total):", fg="yellow")
            )
            # Limit file display to avoid overwhelming output
            display_limit = 50
            for f in clean_info.files[:display_limit]:
                click.echo(f"  {f}")
            if len(clean_info.files) > display_limit:
                click.echo(
                    f"  ... and {len(clean_info.files) - display_limit} more files"
                )
            click.echo()

        if clean_info.installed_dirs or clean_info.installed_files:
            installed_total = clean_info.installed_dirs.__len__() + clean_info.installed_files.__len__()
            click.echo(
                click.style(
                    f"Installed (from install_manifest.txt):",
                    fg="yellow",
                )
            )
            for d in clean_info.installed_dirs:
                click.echo(f"  {d}/")
            display_limit = 50
            shown = 0
            for f in clean_info.installed_files[:display_limit]:
                click.echo(f"  {f}")
                shown += 1
            remaining = len(clean_info.installed_files) - shown
            if remaining > 0:
                click.echo(f"  ... and {remaining} more files")
            click.echo()

        # Show total count if different from displayed
        if clean_info.total_file_count > len(clean_info.files):
            click.echo(
                click.style(
                    f"Total: {clean_info.total_file_count} files in {len(clean_info.directories)} directories",
                    fg="cyan",
                )
            )
            click.echo()

        # Ask for confirmation unless -y flag is passed
        if not yes:
            if not click.confirm("Do you want to proceed?"):
                click.echo("Clean operation cancelled.")
                return
    else:
        click.echo("No files to clean.")
        return

    # Proceed with clean
    try:
        if do_purge:
            rc = purge(bd, verbose=verbose)
        else:
            rc = clean(bd, verbose=verbose)
            # Also remove files that were previously installed
            if clean_info.installed_dirs or clean_info.installed_files:
                uninstall(bd, verbose=verbose)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if rc == 0:
        click.echo(click.style("Clean completed successfully.", fg="green"))

    sys.exit(rc)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@cli.command("install")
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory.",
)
@click.option(
    "--prefix",
    default=None,
    metavar="PATH",
    help="Override install prefix (e.g. /usr/local or ~/myapp).",
)
@click.option(
    "--config",
    default=None,
    help="Build configuration to install, e.g. Release or Debug.",
)
@click.option(
    "--component",
    default=None,
    metavar="COMP",
    help="Install only a specific component.",
)
@click.option(
    "--strip",
    "do_strip",
    is_flag=True,
    help="Strip installed binaries.",
)
@click.option("-v", "--verbose", is_flag=True, help="Print the cmake command.")
def cmd_install(
    build_dir: str,
    prefix: Optional[str],
    config: Optional[str],
    component: Optional[str],
    do_strip: bool,
    verbose: bool,
) -> None:
    """Install the project.

    Runs ``cmake --install <build_dir> [options]``.

    \b
    Examples:
      cmakex install
      cmakex install --prefix /usr/local
      cmakex install --prefix ~/myapp --config Release --strip
      cmakex install --component headers --prefix /opt/myproject
    """
    bd = Path(build_dir)
    if not bd.exists():
        click.echo(
            f"Error: build directory '{build_dir}' does not exist. "
            "Run 'cmakex configure' and 'cmakex build' first.",
            err=True,
        )
        sys.exit(1)

    try:
        rc = install(
            build_dir=bd,
            prefix=prefix,
            config=config,
            component=component,
            strip=do_strip,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    sys.exit(rc)


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


@cli.command("ui")
@click.option(
    "-S",
    "--source-dir",
    default=".",
    show_default=True,
    help="Source directory containing CMakeLists.txt.",
)
@click.option(
    "-B",
    "--build-dir",
    default="build",
    show_default=True,
    help="Build directory that configure/build/clean/install act on.",
)
def cmd_ui(source_dir: str, build_dir: str) -> None:
    """Open the interactive interface.

    Analyzes the project on startup (see 'cmakex info') and lets you browse
    targets, include directories, cache options and compilers, and run
    configure/build/clean/install on BUILD_DIR.
    """
    cmake_file = _require_cmake_file(source_dir)
    # Imported lazily: Textual is only needed for the interactive interface.
    from .tui import CmakexApp

    CmakexApp(cmake_file.parent.resolve(), Path(build_dir)).run()
