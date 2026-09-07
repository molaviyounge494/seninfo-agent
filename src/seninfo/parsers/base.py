"""Parser contract: every scanner adapter produces NormalizedFindings.

Adding a new scanner = implement this interface and register it in
``seninfo.parsers`` (PRD 4.1.1: engine stays format-agnostic).
"""

from dataclasses import dataclass, field
from typing import List

from seninfo.models import NormalizedFinding


@dataclass
class ParseIssue:
    index: int  # record index in source (0-based); for SARIF, result index
    message: str


@dataclass
class ParsedReport:
    findings: List[NormalizedFinding] = field(default_factory=list)
    issues: List[ParseIssue] = field(default_factory=list)


class Parser:
    """Base class for scanner report parsers."""

    def parse(self, text: str) -> ParsedReport:
        raise NotImplementedError

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _norm_path(path: str) -> str:
        """Normalize to repo-relative forward-slash path for rule matching."""
        return (path or "").replace("\\", "/").lstrip("./")
