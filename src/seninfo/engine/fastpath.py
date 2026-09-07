"""Deterministic fast-path short-circuit (PRD 4.2.1) — 0 tokens.

Priority:
  1. verified == true  -> sensitive / VERIFIED_LIVE (confidence 1.0)
  2. deterministic rule hit -> not_sensitive with the rule's category
Otherwise None (deeper judgement is required).
"""

from typing import Optional

from seninfo import __version__
from seninfo.models import Category, Judgment, NormalizedFinding, Verdict
from seninfo.redaction import mask_secret
from seninfo.rules import Rule, match_rules


def short_circuit(
    finding: NormalizedFinding,
    rules: list,
) -> Optional[Judgment]:
    """Return a Judgment for deterministic cases, else None."""
    if finding.verified is True:
        return Judgment(
            finding_id=finding.finding_id,
            verdict=Verdict.SENSITIVE,
            category=Category.VERIFIED_LIVE,
            confidence=1.0,
            secret_masked=mask_secret(finding.secret_raw),
            locations=[{"file": finding.file_path, "line": finding.line}],
            evidence=[
                {
                    "type": "verified",
                    "summary": "scanner原生验证通过(透传), 直接直通",
                    "detail": {"verified": True, "tool": finding.tool},
                }
            ],
            tool=finding.tool,
            detector=finding.detector,
            verified=True,
            entropy=finding.entropy,
            engine_version=__version__,
        )

    hit = match_rules(rules, finding)
    if hit is not None:
        rule: Rule = hit.rule
        return Judgment(
            finding_id=finding.finding_id,
            verdict=verdict_for_not_sensitive(rule.category),
            category=rule.category,
            confidence=1.0,
            secret_masked=mask_secret(finding.secret_raw),
            locations=[{"file": finding.file_path, "line": finding.line}],
            evidence=[
                {
                    "type": "short_circuit_rule",
                    "summary": "命中确定性规则 %s (%s)" % (rule.id, rule.reason),
                    "detail": {"rule_id": rule.id, "matched": hit.matched},
                }
            ],
            tool=finding.tool,
            detector=finding.detector,
            entropy=finding.entropy,
            engine_version=__version__,
            rule_version=rule.id,
        )
    return None


def verdict_for_not_sensitive(category: Category) -> Verdict:
    """Rules short-circuit only ever produce not_sensitive categories."""
    if category in (Category.TEST_OR_MOCK, Category.DOCUMENTATION, Category.FALSE_POSITIVE):
        return Verdict.NOT_SENSITIVE
    raise ValueError("rule category cannot short-circuit to sensitive: %s" % category)


def unresolved_judgment(
    finding: NormalizedFinding, reason: str
) -> Judgment:
    """Safe default for anything not short-circuited (PRD 4.2.4).

    Never silently dismissed: an unjudged finding is reported as sensitive /
    UNRESOLVED so downstream human review is guaranteed to see it.
    """
    return Judgment(
        finding_id=finding.finding_id,
        verdict=Verdict.SENSITIVE,
        category=Category.UNRESOLVED,
        confidence=None,
        secret_masked=mask_secret(finding.secret_raw),
        locations=[{"file": finding.file_path, "line": finding.line}],
        evidence=[
            {
                "type": "unresolved",
                "summary": reason,
                "detail": {"degraded": True},
            }
        ],
        tool=finding.tool,
        detector=finding.detector,
        verified=finding.verified,
        entropy=finding.entropy,
        engine_version=__version__,
    )
