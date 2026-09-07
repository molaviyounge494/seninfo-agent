"""Read-only workspace tools (PRD 4.2.2) with traversal protection.

Path-escape enforcement lives ONLY here: everything (direct reads, LSP
results, search hits) re-enters through ``Workspace.resolve``.
"""

from pathlib import Path
from typing import List, Optional, Tuple

from seninfo.config import BudgetConfig

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".venv", "venv",
    "__pycache__", ".idea", ".vscode", "coverage", ".tox", ".mypy_cache",
    ".pytest_cache", "vendor", ".svn", ".hg",
}
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".go", ".java", ".rs", ".rb", ".php", ".swift", ".kt", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".md", ".rst", ".txt", ".env",
    ".sh", ".bash", ".zsh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
    ".xml", ".csv", ".properties", ".gradle", ".lock",
}


class Workspace:
    """Root-bounded read-only access to the scanned repository."""

    def __init__(self, root: str, budget: Optional[BudgetConfig] = None):
        self.root = Path(root).resolve()
        self.budget = budget or BudgetConfig()

    def resolve(self, rel_path: str) -> Path:
        """Resolve a repo-relative path, raising on any escape (traversal)."""
        candidate = (self.root / rel_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path escapes workspace root: %s" % rel_path)
        return candidate

    def read_file_range(
        self, rel_path: str, start_line: int, end_line: int
    ) -> Tuple[Optional[int], List[str]]:
        """Return ``(actual_line_offset, lines)`` clamped to the file.

        ``start_line``/``end_line`` are 1-based inclusive.
        """
        path = self.resolve(rel_path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None, []
        lines = text.splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line)
        if start > end or not lines:
            return None, []
        return start, lines[start - 1 : end]

    def read_all_text(self, rel_path: str) -> Optional[str]:
        """Full file text (cap enforced); None when unreadable/too big/binary."""
        path = self.resolve(rel_path)
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.budget.max_file_bytes:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None  # binary
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def initial_slice(self, rel_path: str, center_line: Optional[int]) -> str:
        """Hit-line centered context slice (design 5.3)."""
        radius = self.budget.read_radius
        center = center_line or 1
        offset, lines = self.read_file_range(rel_path, center - radius, center + radius)
        if offset is None or not lines:
            return ""
        width = max(4, len(str(offset + len(lines))))
        return "\n".join(
            "%s:%s" % (offset + i, ln) for i, ln in enumerate(lines)
        )

    def search_codebase(self, keyword: str, limit: Optional[int] = None) -> List[dict]:
        """Repo-wide case-insensitive text search (fallback tool).

        Skips VCS/dependency/build dirs, oversized and binary files.
        """
        limit = limit or self.budget.search_limit
        hits: List[dict] = []
        needle = keyword.lower()
        for path in self.root.rglob("*"):
            if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if suffix not in TEXT_EXTENSIONS and path.name not in (
                ".env", "Dockerfile", "Makefile", "LICENSE", "README",
            ):
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > self.budget.max_file_bytes:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, raw in enumerate(fh, 1):
                        if needle in raw.lower():
                            rel = str(path.relative_to(self.root)).replace("\\", "/")
                            hits.append(
                                {
                                    "file": rel,
                                    "line": line_no,
                                    "text": raw.strip()[:200],
                                }
                            )
                            if len(hits) >= limit:
                                return hits
            except OSError:
                continue
        return hits

    def render_hits(self, hits: List[dict], limit: int = 50) -> str:
        """Compact multi-line rendering for LLM consumption."""
        if not hits:
            return "(search: 无匹配)"
        lines = ["(search %d 条)" % len(hits)]
        for hit in hits[:limit]:
            lines.append("%s:%s %s" % (hit["file"], hit["line"], hit["text"]))
        return "\n".join(lines)

    def render_range(self, offset: Optional[int], lines: List[str]) -> str:
        if offset is None or not lines:
            return "(文件该区间不可读或为空)"
        width = max(4, len(str(offset + len(lines))))
        return "\n".join("%s:%s" % (offset + i, ln) for i, ln in enumerate(lines))
