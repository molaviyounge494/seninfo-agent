"""Deterministic rule engine for the fast-path (PRD 4.2.1).

Rules are data-driven: built-in defaults here, overridable per project via a
JSON rules file (``--rules rules.json``). A rule hit yields a not-sensitive
category immediately (0 tokens) or a whitelist-style dismissal.
"""

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from seninfo.models import Category, NormalizedFinding


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str  # "keyword" | "regex" | "path_glob"
    patterns: List[str]
    category: Category
    reason: str = ""


@dataclass
class RuleHit:
    rule: Rule
    matched: str


def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-style glob into a regex.

    ``**`` matches across ``/``; ``*`` and ``?`` stay within a segment.
    """
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                while i + 1 < n and pattern[i + 1] == "*":
                    i += 1
                out.append(".*")
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _path_matches(pattern: str, path: str) -> bool:
    """Match a path; a leading ``**/`` also allows the root-level form."""
    candidates = [pattern]
    if pattern.startswith("**/"):
        candidates.append(pattern[3:])
    return any(
        re.fullmatch(_glob_to_regex(p), path) is not None for p in candidates
    )


def _matches(rule: Rule, finding: NormalizedFinding) -> Optional[str]:
    """Return the first matching pattern, or None."""
    for pat in rule.patterns:
        if rule.kind == "keyword":
            if pat.lower() in finding.secret_raw.lower():
                return pat
        elif rule.kind == "regex":
            if re.search(pat, finding.secret_raw):
                return pat
        elif rule.kind == "path_glob":
            if _path_matches(pat, finding.file_path):
                return pat
        else:  # pragma: no cover - guarded at load time
            raise ValueError("unknown rule kind: %s" % rule.kind)
    return None


def match_rules(rules: List[Rule], finding: NormalizedFinding) -> Optional[RuleHit]:
    """First rule hit wins; rules are evaluated in declaration order."""
    for rule in rules:
        matched = _matches(rule, finding)
        if matched is not None:
            return RuleHit(rule=rule, matched=matched)
    return None


# --------------------------------------------------------------------------
# Built-in defaults (Phase 1 baseline). Project rules files replace these.
# --------------------------------------------------------------------------

DEFAULT_RULES: List[Rule] = [
    Rule(
        id="docs-placeholder-keyword",
        kind="keyword",
        patterns=[
            "example.com", "your_token_here", "your-api-key", "your-secret",
            "changeme", "<your_", "replace-me", "placeholder", "sample-key",
        ],
        category=Category.DOCUMENTATION,
        reason="文档/示例占位惯用语",
    ),
    Rule(
        id="mock-keyword",
        kind="keyword",
        patterns=[
            "mock_secret", "fake-key", "dummy-token", "test-only",
            "test-secret", "not-a-real", "dummy-secret", "fake-secret",
        ],
        category=Category.TEST_OR_MOCK,
        reason="Mock/测试占位惯用语",
    ),
    Rule(
        id="test-path",
        kind="path_glob",
        patterns=[
            "**/tests/**", "tests/**", "**/test/**", "test/**",
            "**/__tests__/**", "__tests__/**", "**/*_test.go", "**/*_test.py",
            "**/test_*.py", "**/*.test.js", "**/*.test.ts", "**/spec/**",
            "**/fixtures/**", "fixtures/**", "**/.env.test", "**/.env.mock",
            "**/jest.setup*", "**/conftest.py",
        ],
        category=Category.TEST_OR_MOCK,
        reason="测试/夹具目录",
    ),
    Rule(
        id="docs-path",
        kind="path_glob",
        patterns=[
            "**/*.md", "*.md", "**/docs/**", "docs/**", "**/examples/**",
            "examples/**", "**/*.example", "*.example", "**/.env.example",
            "**/samples/**", "**/sample/**",
        ],
        category=Category.DOCUMENTATION,
        reason="文档/示例目录",
    ),
    Rule(
        id="build-artifact-path",
        kind="path_glob",
        patterns=[
            "**/dist/**", "dist/**", "**/build/**", "build/**",
            "**/.next/**", ".next/**", "**/node_modules/**",
            "**/coverage/**", "**/vendor/**", "vendor/**", "**/.venv/**",
        ],
        category=Category.FALSE_POSITIVE,
        reason="构建产物/第三方目录",
    ),
]


def load_rules(path: Optional[str] = None) -> List[Rule]:
    """Load rules from a JSON file, or return built-in defaults.

    JSON shape: ``[{"id","kind","patterns":[...],"category","reason"}]``.
    An explicit file *replaces* the defaults (project-owned rule set).
    """
    if path is None:
        return list(DEFAULT_RULES)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rules: List[Rule] = []
    for item in data:
        rules.append(
            Rule(
                id=item["id"],
                kind=item["kind"],
                patterns=list(item["patterns"]),
                category=Category(item["category"]),
                reason=item.get("reason", ""),
            )
        )
    return rules
