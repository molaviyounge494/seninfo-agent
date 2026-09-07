"""Core data model: normalized scanner findings and judgement records.

Phase 1 uses plain dataclasses (zero runtime deps so the skeleton runs in
offline CI runners). PRD 4.3 mandates strict typed output; the service layer
(Phase 2) will migrate to pydantic models with identical field names.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Verdict(str, Enum):
    """Binary outcome consumed by the downstream human-review process."""

    SENSITIVE = "sensitive"  # requires human review (incl. UNRESOLVED)
    NOT_SENSITIVE = "not_sensitive"  # noise, auto-filtered


class Category(str, Enum):
    """Reason sub-class. See PRD 4.3.2."""

    VERIFIED_LIVE = "VERIFIED_LIVE"  # scanner-verified live (sensitive)
    LIKELY_REAL = "LIKELY_REAL"  # hardcoded in real logic/config (sensitive)
    UNRESOLVED = "UNRESOLVED"  # failed / low confidence -> must go to human
    TEST_OR_MOCK = "TEST_OR_MOCK"  # test dirs / fixtures / mock placeholders
    DOCUMENTATION = "DOCUMENTATION"  # README / comments / examples
    FALSE_POSITIVE = "FALSE_POSITIVE"  # build hashes / static fingerprints


_CATEGORY_VERDICT = {
    Category.VERIFIED_LIVE: Verdict.SENSITIVE,
    Category.LIKELY_REAL: Verdict.SENSITIVE,
    Category.UNRESOLVED: Verdict.SENSITIVE,
    Category.TEST_OR_MOCK: Verdict.NOT_SENSITIVE,
    Category.DOCUMENTATION: Verdict.NOT_SENSITIVE,
    Category.FALSE_POSITIVE: Verdict.NOT_SENSITIVE,
}


def verdict_for(category: Category) -> Verdict:
    """Map a reason sub-class to its binary verdict (PRD 4.3.2)."""
    return _CATEGORY_VERDICT[category]


@dataclass
class NormalizedFinding:
    """Tool-agnostic finding produced by every parser (PRD 4.1)."""

    tool: str  # "trufflehog" | "gitleaks"
    detector: str
    file_path: str  # repo-relative, forward slashes
    line: Optional[int] = None
    col: Optional[int] = None
    secret_raw: str = ""  # plaintext secret, memory only (see redaction)
    match_context: Optional[str] = None
    verified: Optional[bool] = None  # scanner-verified, transparent passthrough
    entropy: Optional[float] = None  # shannon entropy, auxiliary feature only
    finding_id: str = ""  # stable id for downstream join-back (PRD 4.3.1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "tool": self.tool,
            "detector": self.detector,
            "file_path": self.file_path,
            "line": self.line,
            "col": self.col,
            "verified": self.verified,
            "entropy": round(self.entropy, 4) if self.entropy is not None else None,
        }


@dataclass
class Judgment:
    """A single adjudication record (PRD 4.3.1 schema, JSONL line)."""

    finding_id: str
    verdict: Verdict
    category: Category
    confidence: Optional[float]  # None for UNRESOLVED / deterministic n/a
    secret_masked: str
    locations: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    tool: str = ""
    detector: str = ""
    verified: Optional[bool] = None
    entropy: Optional[float] = None
    engine_version: str = "0.1.0"
    rule_version: str = "builtin"

    def to_record(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "verdict": self.verdict.value,
            "category": self.category.value,
            "confidence": self.confidence,
            "tool": self.tool,
            "detector": self.detector,
            "secret_masked": self.secret_masked,
            "locations": self.locations,
            "evidence": self.evidence,
            "verified": self.verified,
            "entropy": round(self.entropy, 4) if self.entropy is not None else None,
            "engine_version": self.engine_version,
            "rule_version": self.rule_version,
        }
