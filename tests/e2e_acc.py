"""ACC-01..12 end-to-end tests (offline).

Mirrors docs/acceptance-cases.md. Fast-path cases exercise the real CLI
pipeline logic (run_scan); deep cases drive DeepEngine through the same
pipeline with a scripted LLM stand-in (FakeClient) because no model endpoint
exists in this environment. Deep runs pin workers=1 to keep the scripted
client deterministic.
"""

import json
from pathlib import Path

from seninfo.config import Config
from seninfo.engine.orchestrator import run_scan
from seninfo.engine.tools import Workspace
from seninfo.models import Category, Verdict
from seninfo.parsers.base import ParsedReport
from seninfo.parsers.gitleaks import GitleaksParser
from seninfo.parsers.trufflehog import TrufflehogParser
from seninfo.rules import DEFAULT_RULES

from test_phase1 import FakeClient, finding, tmp_workspace

FIXTURES = Path(__file__).parent / "fixtures"
ACC06_REPO = str(FIXTURES / "workspaces" / "acc-06-minimal")


def _cfg(workers: int = 1) -> Config:
    cfg = Config()
    cfg.budget.workers = workers
    return cfg


def _submit(verdict, category, confidence=0.9, rationale="r"):
    return json.dumps(
        {"kind": "submit", "verdict": verdict, "category": category,
         "confidence": confidence, "rationale": rationale}
    )


_AGREE = json.dumps({"agree": True, "rationale": "ok"})


def _fastpath(f: list, cfg=None):
    return run_scan(ParsedReport(findings=f), cfg or _cfg(),
                    DEFAULT_RULES, client=None, workspace=None)


# --- ACC-01..05: 确定性短路(Fast-Path) -------------------------------------

def test_acc_01_verified_live_passthrough():
    res = _fastpath([finding(verified=True, secret="AKIAIOSFODNN7EXAMPLE")])
    j = res.judgments[0]
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.VERIFIED_LIVE
    assert j.confidence == 1.0
    assert res.short_circuited == 1


def test_acc_02_placeholder_keyword():
    res = _fastpath([finding(secret="sk-test-your_token_here-abc")])
    j = res.judgments[0]
    assert j.verdict == Verdict.NOT_SENSITIVE
    assert j.category == Category.DOCUMENTATION


def test_acc_03_test_directory_mock():
    f = finding(file="tests/fixtures/api_keys.json", secret="dummy-token-1234")
    res = _fastpath([f])
    j = res.judgments[0]
    assert j.verdict == Verdict.NOT_SENSITIVE
    assert j.category == Category.TEST_OR_MOCK


def test_acc_04_docs_example():
    f = finding(file="docs/api-example.md", secret="https://user:pass@example.com")
    res = _fastpath([f])
    j = res.judgments[0]
    assert j.verdict == Verdict.NOT_SENSITIVE
    assert j.category == Category.DOCUMENTATION


def test_acc_05_build_hash_artifact():
    f = finding(file="dist/app.8f3a2c9e.js", secret="8f3a2c9e1b7d4a5f6c0e")
    res = _fastpath([f])
    j = res.judgments[0]
    assert j.verdict == Verdict.NOT_SENSITIVE
    assert j.category == Category.FALSE_POSITIVE


# --- ACC-06..09: 深度研判(脚本化模型) ----------------------------------------

def test_acc_06_hardcoded_prod_secret_deep():
    ws = Workspace(ACC06_REPO)
    f = finding(file="config/prod.py", line=4,
                secret="sk-live-prod-8f3a2c9e1b7d4a5f")
    fake = FakeClient([
        json.dumps({"kind": "tool_request", "tool": "read_file_range",
                    "args": {"file": "config/prod.py", "start": 1, "end": 6}}),
        _submit("sensitive", "LIKELY_REAL", 0.92, "生产配置模块硬编码, 非测试路径"),
        _AGREE,
    ])
    res = run_scan(ParsedReport(findings=[f]), _cfg(), DEFAULT_RULES,
                   client=fake, workspace=ws)
    j = res.judgments[0]
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.LIKELY_REAL
    assert any(e["type"] == "tool_trace" for e in j.evidence)
    assert j.locations == [{"file": "config/prod.py", "line": 4}]


def test_acc_07_cross_file_duplicate_aggregation():
    ws = Workspace(ACC06_REPO)
    secret = "sk-live-prod-8f3a2c9e1b7d4a5f"
    f1 = finding(file="config/prod.py", line=4, secret=secret)
    f2 = finding(file="services/db.py", line=6, secret=secret)
    fake = FakeClient([
        _submit("sensitive", "LIKELY_REAL", 0.95, "定义点"),
        _AGREE,
        _submit("sensitive", "LIKELY_REAL", 0.95, "引用点"),
        _AGREE,
    ])
    res = run_scan(ParsedReport(findings=[f1, f2]), _cfg(), DEFAULT_RULES,
                   client=fake, workspace=ws)
    assert len(res.judgments) == 1  # merged into a single event
    locs = {(l["file"], l["line"]) for l in res.judgments[0].locations}
    assert locs == {("config/prod.py", 4), ("services/db.py", 6)}
    assert any(e["type"] == "aggregation" for e in res.judgments[0].evidence)


def test_acc_08_model_timeout_unresolved():
    from seninfo.llm.client import LLMTimeoutError

    root = tmp_workspace({"config/prod.py": "DB_PASSWORD='sk-x-1'\n"})
    f = finding(file="config/prod.py", secret="sk-x-1")
    fake = FakeClient([LLMTimeoutError("simulated timeout")])
    res = run_scan(ParsedReport(findings=[f]), _cfg(), DEFAULT_RULES,
                   client=fake, workspace=Workspace(root))
    j = res.judgments[0]
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.UNRESOLVED
    assert res.reason_counts.get("model_timeout") == 1


def test_acc_09_lsp_fallback_search():
    root = tmp_workspace({"services/pay.ts": "const token = 'sk-ts-live-1';\n"})
    f = finding(file="services/pay.ts", secret="sk-ts-live-1")
    fake = FakeClient([
        json.dumps({"kind": "tool_request", "tool": "goto_definition",
                    "args": {"file": "services/pay.ts", "line": 1, "col": 14}}),
        _submit("sensitive", "LIKELY_REAL", 0.9, "TS 生产路径(经检索回落)"),
        _AGREE,
    ])
    res = run_scan(ParsedReport(findings=[f]), _cfg(), DEFAULT_RULES,
                   client=fake, workspace=Workspace(root))
    assert res.lsp_fallback == 1
    assert res.judgments[0].category == Category.LIKELY_REAL


# --- ACC-10..12: 健壮性/格式 ------------------------------------------------

def test_acc_10_malformed_input_line_placeholder():
    text = (FIXTURES / "sample_trufflehog.jsonl").read_text(encoding="utf-8")
    report = TrufflehogParser().parse(text)
    assert len(report.issues) == 1
    res = run_scan(report, _cfg(), DEFAULT_RULES, client=None, workspace=None)
    assert res.parse_errors == 1
    placeholders = [j for j in res.judgments
                    if j.finding_id.startswith("parse-error")]
    assert len(placeholders) == 1
    assert placeholders[0].verdict == Verdict.SENSITIVE
    assert placeholders[0].category == Category.UNRESOLVED


def test_acc_11_no_workspace_degraded_mode():
    f = finding(file="config/prod.py", secret="sk-live-abcdef123456")
    res = run_scan(ParsedReport(findings=[f]), _cfg(), DEFAULT_RULES,
                   client=None, workspace=None)
    assert res.degraded is True
    j = res.judgments[0]
    assert j.category == Category.UNRESOLVED  # nothing silently dismissed
    assert res.reason_counts.get("degraded_no_workspace") == 1


def test_acc_12_gitleaks_sarif_normalization():
    text = (FIXTURES / "sample_gitleaks.sarif").read_text(encoding="utf-8")
    report = GitleaksParser().parse(text)
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.detector == "slack-access-token"
    assert f.file_path == "config/slack.yaml" and f.line == 4
    # SARIF carries no plaintext secret -> runs through the pipeline safely
    res = run_scan(report, _cfg(), DEFAULT_RULES, client=None, workspace=None)
    assert len(res.judgments) == 1
    assert res.judgments[0].verdict == Verdict.SENSITIVE
