"""Variable resolution for CMake expressions."""

from __future__ import annotations

import re
from typing import Dict, Optional


class VariableResolver:
    """Handles CMake variable substitution ${VAR}."""

    VAR_RE = re.compile(r"\$\{([^}]+)\}")

    def __init__(self, variables: Dict[str, str]):
        """Initialize resolver with a variable dictionary."""
        self.variables = variables

    def resolve(self, text: str) -> str:
        """Substitute ${VAR} references (up to 5 nested levels)."""
        for _ in range(5):
            new = self.VAR_RE.sub(
                lambda m: self.variables.get(m.group(1), m.group(0)), text
            )
            if new == text:
                break
            text = new
        return text

    def set(self, name: str, value: str) -> None:
        """Set a variable value."""
        self.variables[name] = value

    def unset(self, name: str) -> None:
        """Remove a variable."""
        self.variables.pop(name, None)

    def get(self, name: str, default: Optional[str] = "") -> Optional[str]:
        """Get a variable value."""
        return self.variables.get(name, default)
