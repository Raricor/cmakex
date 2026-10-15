"""Clean operation with smart directory grouping."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set, Tuple

from ..introspect import ANALYSIS_DIR_NAME


@dataclass
class CleanInfo:
    """Information about what will be cleaned."""

    directories: List[Path]  # Directories to display (may be collapsed)
    files: List[Path]  # Individual files to display
    total_file_count: int  # Total number of files (including those in collapsed dirs)
    installed_dirs: List[Path]  # Collapsed install directories (from install_manifest.txt)
    installed_files: List[Path]  # Individual installed files that couldn't be collapsed


class Cleaner:
    """Handles build artifact cleaning with smart directory grouping."""

    def __init__(self, build_dir: Path):
        """Initialize cleaner for a specific build directory."""
        self.build_dir = build_dir

    def get_clean_targets(self) -> CleanInfo:
        """Get list of files and directories for clean operation.

        Returns CleanInfo with smart directory grouping:
        - If all files in a directory will be removed, show only the directory
        - If some files remain, show individual files
        Also includes files from install_manifest.txt that will be uninstalled.
        """
        if not self.build_dir.exists():
            return CleanInfo(directories=[], files=[], total_file_count=0, installed_dirs=[], installed_files=[])

        # Collect all artifacts that would be removed
        artifacts = self._scan_build_artifacts()

        # Apply smart grouping
        info = self._group_by_directory(artifacts)
        inst_dirs, inst_files = self._scan_installed_files()
        info.installed_dirs = inst_dirs
        info.installed_files = inst_files
        return info

    def get_purge_targets(self) -> CleanInfo:
        """Get list for purge operation (entire build directory)."""
        if not self.build_dir.exists():
            return CleanInfo(directories=[], files=[], total_file_count=0, installed_dirs=[], installed_files=[])

        # Count total files for display
        total_files = sum(1 for _ in self.build_dir.rglob("*") if _.is_file())

        inst_dirs, inst_files = self._scan_installed_files()
        return CleanInfo(
            directories=[self.build_dir], files=[], total_file_count=total_files,
            installed_dirs=inst_dirs, installed_files=inst_files,
        )

    def _scan_installed_files(self) -> Tuple[List[Path], List[Path]]:
        """Parse install_manifest.txt and return (collapsed_dirs, individual_files)
        using the same smart-grouping logic as build artifacts."""
        manifest = self.build_dir / "install_manifest.txt"
        if not manifest.exists():
            return [], []
        raw: List[Path] = []
        with manifest.open() as fh:
            for line in fh:
                p = Path(line.strip())
                if p and p.is_file():
                    raw.append(p)
        if not raw:
            return [], []
        grouped = self._group_by_directory(raw)
        return grouped.directories, grouped.files

    def _scan_build_artifacts(self) -> List[Path]:
        """Scan build directory for CMake artifacts to clean.

        Returns list of file paths that would be removed.
        """
        artifacts: List[Path] = []

        # Artifact patterns
        file_patterns = ["*.o", "*.a", "*.so*", "*.dylib", "*.dll", "*.exe"]
        dir_patterns = ["CMakeFiles", "*.dir"]

        for root, dirs, filenames in os.walk(self.build_dir):
            root_path = Path(root)
            if root_path == self.build_dir:
                # The analysis trees are not touched by the clean target.
                dirs[:] = [d for d in dirs if d != ANALYSIS_DIR_NAME]

            # Check for special directories (CMakeFiles, *.dir)
            for dir_pattern in dir_patterns:
                for d in dirs:
                    dir_path = root_path / d
                    if dir_path.match(dir_pattern):
                        # Add all files in this directory
                        for f in dir_path.rglob("*"):
                            if f.is_file():
                                artifacts.append(f)

            # Check for build artifact files
            for filename in filenames:
                file_path = root_path / filename
                if any(file_path.match(pattern) for pattern in file_patterns):
                    artifacts.append(file_path)

        return artifacts

    def _group_by_directory(self, artifacts: List[Path]) -> CleanInfo:
        """Group artifacts by directory with smart collapsing.

        If all files in a directory are being removed, show only the directory.
        Otherwise, show individual files.
        """
        if not artifacts:
            return CleanInfo(directories=[], files=[], total_file_count=0, installed_dirs=[], installed_files=[])

        # Group files by their parent directory
        dir_files: Dict[Path, Set[Path]] = defaultdict(set)
        for artifact in artifacts:
            dir_files[artifact.parent].add(artifact)

        # Check which directories can be collapsed
        collapsible_dirs: Set[Path] = set()
        individual_files: List[Path] = []

        for dir_path, files_in_dir in dir_files.items():
            # Get all files that actually exist in this directory
            try:
                existing_files = {f for f in dir_path.iterdir() if f.is_file()}
            except (OSError, PermissionError):
                # If we can't read the directory, show individual files
                individual_files.extend(files_in_dir)
                continue

            # Check if we're removing ALL files in this directory
            if files_in_dir >= existing_files:
                # All files will be removed, can collapse to directory
                collapsible_dirs.add(dir_path)
            else:
                # Some files remain, show individual files
                individual_files.extend(files_in_dir)

        # Further optimize: collapse parent directories if all subdirs are collapsed
        optimized_dirs = self._collapse_parent_directories(
            collapsible_dirs, set(dir_files.keys())
        )

        # Remove individual files that are now covered by collapsed directories
        final_files = []
        for f in individual_files:
            if not any(f.is_relative_to(d) for d in optimized_dirs):
                final_files.append(f)

        return CleanInfo(
            directories=sorted(optimized_dirs),
            files=sorted(final_files),
            total_file_count=len(artifacts),
            installed_dirs=[],
            installed_files=[],
        )

    def _collapse_parent_directories(
        self, collapsible: Set[Path], all_dirs: Set[Path]
    ) -> Set[Path]:
        """Collapse parent directories if all their subdirectories are collapsible.

        Example: If build/CMakeFiles/foo.dir/ and build/CMakeFiles/bar.dir/
                 are both collapsible, collapse to build/CMakeFiles/
        """
        # Build parent-child relationships
        parent_children: Dict[Path, Set[Path]] = defaultdict(set)
        for d in all_dirs:
            if d.parent != d:
                parent_children[d.parent].add(d)

        result = set(collapsible)

        # Iteratively check if parents can be collapsed
        changed = True
        while changed:
            changed = False
            for parent, children in list(parent_children.items()):
                # Check if all children of this parent are in result
                if children and all(child in result for child in children):
                    # Check if parent has any other children not in our tracking
                    try:
                        actual_subdirs = {p for p in parent.iterdir() if p.is_dir()}
                        # If we're covering all subdirectories, collapse to parent
                        if children >= actual_subdirs:
                            # Remove children, add parent
                            for child in children:
                                result.discard(child)
                            result.add(parent)
                            changed = True

                            # Update tracking for next level up
                            if parent.parent != parent:
                                parent_children[parent.parent].add(parent)
                    except (OSError, PermissionError):
                        # Can't verify, keep individual directories
                        pass

        return result
