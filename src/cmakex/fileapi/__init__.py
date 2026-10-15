"""CMake File API client.

Reads exact target and include information from a configured build tree.
Requires CMake >= 3.14; see
https://cmake.org/cmake/help/latest/manual/cmake-file-api.7.html
"""

from .query import MIN_CMAKE_VERSION, supports_file_api, write_query
from .reply import FileApiError, Reply

__all__ = [
    "MIN_CMAKE_VERSION",
    "FileApiError",
    "Reply",
    "supports_file_api",
    "write_query",
]
