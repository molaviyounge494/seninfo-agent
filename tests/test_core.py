"""Unit tests for the Phase 1 fast-path slice (core helpers)."""

from pathlib import Path

from seninfo.entropy import shannon_entropy

from seninfo.entropy import shannon_entropy
from seninfo.engine.fastpath import short_circuit, unresolved_judgment
from seninfo.models import Category, NormalizedFinding, Verdict
from seninfo.parsers.trufflehog import TrufflehogParser
from seninfo.redaction import mask_secret
from seninfo.rules import DEFAULT_RULES, load_rules


def _finding(**overrides):
    base = dict(
        tool="gitleaks",
        detector="generic",
        file_path="src/app.py",
        line=3,
        secret_raw="sk-live-abcdef123456",
        verified=None,
    )
    base.update(overrides)
    return NormalizedFinding(**base)


def test_mask_secret():
    assert mask_secret("AKIAIOSFODNN7EXAMPLE") == "AKIA[MASKED]MPLE"
    assert mask_secret("short") == "[REDACTED]"
    assert "AKIAIOSFODNN7EXAMPLE" not in mask_secret("AKIAIOSFODNN7EXAMPLE")


def test_entropy_range():
    e = shannon_entropy("abcdefghijklmnop")
    assert 3.5 < e < 4.5
    assert shannon_entropy("") == 0.0


def test_verified_passthrough_short_circuits():
    f = _finding(verified=True)
    j = short_circuit(f, DEFAULT_RULES)
    assert j is not None
    assert j.category == Category.VERIFIED_LIVE
    assert j.verdict == Verdict.SENSITIVE
    assert j.confidence == 1.0


def test_doc_placeholder_rule():
    f = _finding(file_path="README.md", secret_raw="sk-test-your_token_here-abc")
    j = short_circuit(f, DEFAULT_RULES)
    assert j is not None
    assert j.category == Category.DOCUMENTATION
    assert j.verdict == Verdict.NOT_SENSITIVE


def test_test_path_rule_beats_keyword():
    f = _finding(file_path="tests/fixtures/api_keys.json", secret_raw="dummy-token-1234")
    j = short_circuit(f, DEFAULT_RULES)
    assert j is not None
    assert j.category == Category.TEST_OR_MOCK


def test_unmatched_goes_unresolved_not_silent():
    f = _finding(file_path="services/db.py", secret_raw="-----BEGIN OPENSSH PRIVATE KEY-----")
    assert short_circuit(f, DEFAULT_RULES) is None
    j = unresolved_judgment(f, "no deep engine yet")
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.UNRESOLVED


def test_root_level_md_and_fixtures_match():
    """Glob matcher: root-level paths must match **/ patterns too."""
    for path, rule_id in [
        ("README.md", "docs-path"),
        ("tests/x.py", "test-path"),
        ("node_modules/x/index.js", "build-artifact-path"),
    ]:
        f = _finding(file_path=path, secret_raw="zzz")
        j = short_circuit(f, DEFAULT_RULES)
        assert j is not None, path
        assert any(e["detail"]["rule_id"] == rule_id for e in j.evidence), path


def test_custom_rules_file_replaces_defaults():
    rules_path = Path(__file__).parent / "fixtures" / "custom_rules.json"
    rules = load_rules(str(rules_path))
    assert len(rules) == 1
    assert rules[0].id == "org-env-template"
    # explicit rules file replaces built-ins
    f = _finding(file_path=".env.template", secret_raw="zzz")
    j = short_circuit(f, rules)
    assert j is not None and j.category == Category.DOCUMENTATION


def test_trufflehog_parser_normalizes_and_masks():
    sample = Path(__file__).parent / "fixtures" / "sample_trufflehog.jsonl"
    text = sample.read_text(encoding="utf-8")
    report = TrufflehogParser().parse(text)
    assert len(report.findings) == 4
    assert len(report.issues) == 1  # malformed line
    verified = [f for f in report.findings if f.verified is True]
    assert len(verified) == 1
    assert all(f.finding_id for f in report.findings)
    assert all(f.entropy is not None for f in report.findings)


def test_gitleaks_json_parser_and_rules():
    from seninfo.parsers.gitleaks import GitleaksParser

    text = (
        Path(__file__).parent / "fixtures" / "sample_gitleaks.json"
    ).read_text(encoding="utf-8")
    report = GitleaksParser().parse(text)
    assert len(report.findings) == 2
    assert all(f.tool == "gitleaks" and f.verified is None for f in report.findings)

    for f in report.findings:
        j = short_circuit(f, DEFAULT_RULES)
        if f.file_path.startswith("docs/"):
            # docs sample -> not_sensitive via placeholder rule
            assert j is not None and j.verdict == Verdict.NOT_SENSITIVE, f
        else:
            # production-looking finding must not be silently dismissed
            assert j is None or j.verdict == Verdict.SENSITIVE, f


def test_gitleaks_sarif_parser():
    from seninfo.parsers.gitleaks import GitleaksParser

    text = (
        Path(__file__).parent / "fixtures" / "sample_gitleaks.sarif"
    ).read_text(encoding="utf-8")
    report = GitleaksParser().parse(text)
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.detector == "slack-access-token"
    assert f.file_path == "config/slack.yaml"
    assert f.line == 4
    assert f.verified is None
    assert f.match_context  # message best-effort (ACC-12 known limitation)
