"""CMake parser package.

This package provides functionality to parse CMakeLists.txt files and
extract build information.
"""

from ..models import CMakeInfo, ExecutableInfo, LibraryInfo
from .parser import parse_cmake_file

__all__ = [
    "CMakeInfo",
    "ExecutableInfo",
    "LibraryInfo",
    "parse_cmake_file",
]
