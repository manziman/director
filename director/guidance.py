"""Discover and render repository-local instructions for coding agents."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_FILENAMES = {"AGENTS.md", "CLAUDE.md"}
_IGNORED_DIRECTORIES = {".director", ".git", ".venv", "node_modules"}


@dataclass(frozen=True)
class GuidanceFile:
    """One repository-local coding guidance file."""

    path: str
    content: str


@dataclass(frozen=True)
class RepositoryGuidance:
    """Immutable discovered guidance with context selection hidden behind one seam."""

    repo: Path
    files: tuple[GuidanceFile, ...]

    @classmethod
    def discover(cls, repo: Path) -> RepositoryGuidance:
        """Read supported guidance files once, excluding dependency and tool metadata."""
        root = Path(repo).resolve()
        found: list[GuidanceFile] = []
        for current, directories, filenames in os.walk(root):
            directories[:] = sorted(d for d in directories if d not in _IGNORED_DIRECTORIES)
            for name in sorted(filenames):
                if name not in _FILENAMES:
                    continue
                path = Path(current) / name
                found.append(
                    GuidanceFile(
                        path.relative_to(root).as_posix(),
                        path.read_text(encoding="utf-8", errors="replace"),
                    )
                )
        found.sort(key=lambda item: (len(Path(item.path).parts), item.path))
        return cls(root, tuple(found))

    def for_planning(self) -> str:
        """Return all discovered guidance so planning can account for nested rules."""
        return self._render(self.files)

    def for_files(self, paths: list[str]) -> str:
        """Return root and ancestor guidance applicable to the supplied repo paths."""
        directories = {"."}
        for raw_path in paths:
            path = Path(raw_path)
            target = path.resolve() if path.is_absolute() else (self.repo / path).resolve()
            try:
                relative = target.relative_to(self.repo)
            except ValueError:
                continue
            current = relative.parent
            while True:
                directories.add(current.as_posix())
                if current == Path("."):
                    break
                current = current.parent

        relevant = [item for item in self.files if Path(item.path).parent.as_posix() in directories]
        return self._render(relevant)

    @staticmethod
    def _render(files: list[GuidanceFile] | tuple[GuidanceFile, ...]) -> str:
        if not files:
            return "No repository coding guidance files were found."
        parts = [
            "REPOSITORY CODING GUIDANCE (authoritative; follow it alongside the task).",
            "A nested file applies only to work under the directory shown in its path.",
        ]
        for item in files:
            parts.extend(("", f"--- {item.path} ---", item.content.rstrip()))
        return "\n".join(parts)
