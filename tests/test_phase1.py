"""Phase 1 module tests: config / tools / deep engine / aggregator / orchestrator / lsp.

Run without pytest: PYTHONPATH=src:tests python3 tests/run_all.py
(or via pytest when available — all tests are plain functions).
"""

import io
import json
import os
import tempfile
from pathlib import Path

from seninfo.config import Config, load_config
from seninfo.engine.aggregator import aggregate
from seninfo.engine.deep import DeepEngine
from seninfo.engine.lsp import LspSession, encode_message, read_message, _ByteReader
from seninfo.engine.orchestrator import run_scan
from seninfo.engine.tools import Workspace
from seninfo.entropy import shannon_entropy
from seninfo.llm.client import LLMError, LLMTimeoutError
from seninfo.models import (
    Category,
    Judgment,
    NormalizedFinding,
    Verdict,
)
from seninfo.parsers.base import ParsedReport
from seninfo.rules import DEFAULT_RULES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tmp_workspace(files: dict) -> str:
    root = tempfile.mkdtemp(prefix="seninfo_t_")
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def finding(file="src/prod.py", line=3, secret="sk-live-abcdef123456",
            detector="generic", verified=None, tool="gitleaks"):
    f = NormalizedFinding(
        tool=tool, detector=detector, file_path=file, line=line,
        secret_raw=secret, verified=verified,
    )
    f.entropy = shannon_entropy(secret)
    f.finding_id = "fid_" + file.replace("/", "_")
    return f


class FakeClient:
    """Scripted OpenAI-compatible double. Items: str | Exception | callable."""

    def __init__(self, script, model="fake-judge"):
        self.script = list(script)
        self.model = model
        self.critic_model = None
        self.calls = []
        self.leftover = []

    def complete(self, messages, model=None, temperature=None, timeout=None):
        self.calls.append((model, list(messages)))
        if not self.script:
            self.leftover.append("extra-call")
            raise LLMError("unexpected extra model call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages)
        return item


def engine_for(root, fake, cfg=None):
    ws = Workspace(root, cfg.budget if cfg else None)
    return DeepEngine(ws, fake, cfg or Config())


def reason_of(judgment: Judgment) -> str:
    for e in judgment.evidence:
        if e.get("type") == "unresolved":
            return (e.get("detail") or {}).get("reason_code", "?")
    return "?"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_offline():
    cfg = load_config(None)
    assert cfg.llm_configured is False
    assert cfg.budget.tool_rounds == 5
    assert cfg.rules_file is None


def test_config_loads_json_and_env(monkeypatch=None):
    cfg_file = Path(tempfile.mktemp(suffix=".json"))
    cfg_file.write_text(
        json.dumps(
            {
                "model": {"endpoint": "http://127.0.0.1:8000/v1",
                          "judge_model": "deepseek-chat"},
                "budget": {"tool_rounds": 3},
                "tools": {"lsp": {"python": {"command": ["pyright-langserver", "--stdio"]}}},
                "workspace": "/tmp/repo",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg.llm_configured
    assert cfg.model.endpoint == "http://127.0.0.1:8000/v1"
    assert cfg.model.judge_model == "deepseek-chat"
    assert cfg.budget.tool_rounds == 3
    assert not hasattr(cfg, "lsp")  # tools.lsp is ignored (registry-driven now)
    assert cfg.workspace == "/tmp/repo"
    old = os.environ.get("SENINFO_LLM_ENDPOINT")
    os.environ["SENINFO_LLM_ENDPOINT"] = "http://10.0.0.2:8080/v1"
    try:
        env_cfg = load_config(None)
        assert env_cfg.model.endpoint == "http://10.0.0.2:8080/v1"
    finally:
        if old is None:
            os.environ.pop("SENINFO_LLM_ENDPOINT", None)
        else:
            os.environ["SENINFO_LLM_ENDPOINT"] = old


# ---------------------------------------------------------------------------
# Workspace tools
# ---------------------------------------------------------------------------

def test_search_codebase_skips_vcs_and_deps():
    root = tmp_workspace(
        {
            "app.py": "SECRET_KEY='abc-123'\nprint(1)\n",
            ".git/HEAD": "abc-123\n",
            "node_modules/x/index.js": "abc-123\n",
            "README.md": "# t\n",
        }
    )
    ws = Workspace(root)
    hits = ws.search_codebase("abc-123")
    files = [h["file"] for h in hits]
    assert files == ["app.py"], files
    assert ws.initial_slice("app.py", 1).startswith("1:SECRET_KEY")


def test_read_range_clamps_and_guards_traversal():
    root = tmp_workspace({"f.py": "l1\nl2\nl3\nl4\nl5\n"})
    ws = Workspace(root)
    offset, lines = ws.read_file_range("f.py", 2, 4)
    assert offset == 2 and lines == ["l2", "l3", "l4"]
    try:
        ws.resolve("../etc/passwd")
        raise AssertionError("traversal should have been rejected")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# DeepEngine with scripted LLM
# ---------------------------------------------------------------------------

def test_deep_submit_sensitive_agreed_by_critic():
    root = tmp_workspace({"config/prod.py": "DB_PASSWORD='sk-live-abcdef123456'\n"})
    fake = FakeClient(
        [
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.92,
                        "rationale": "生产配置硬编码"}),
            json.dumps({"agree": True, "rationale": "证据充分"}),
        ]
    )
    j = engine_for(root, fake).judge(finding(file="config/prod.py", line=1))
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.LIKELY_REAL
    assert j.confidence == 0.92
    assert any(e["type"] == "critic_review" for e in j.evidence)


def test_deep_tool_loop_then_not_sensitive():
    root = tmp_workspace({"tests/mock_util.py": "dummy-token-abc\n"})
    # path tests/mock_util.py not matched by default rules? it is: test-path rule
    # -> so use a non-rule path to force deep adjudication:
    root2 = tmp_workspace({"src/util.py": "line1\nline2\ntoken='dummy-xyz-1'\n"})

    def judge_turn(messages):
        return json.dumps(
            {"kind": "tool_request", "tool": "read_file_range",
             "args": {"file": "src/util.py", "start": 1, "end": 3}}
        )

    fake = FakeClient(
        [
            judge_turn,
            json.dumps({"kind": "submit", "verdict": "not_sensitive",
                        "category": "TEST_OR_MOCK", "confidence": 0.88,
                        "rationale": "mock 数据"}),
            json.dumps({"agree": True, "rationale": "ok"}),
        ]
    )
    j = engine_for(root2, fake).judge(finding(file="src/util.py", line=3, secret="dummy-xyz-1"))
    assert j.verdict == Verdict.NOT_SENSITIVE
    trace = [e for e in j.evidence if e["type"] == "tool_trace"]
    assert trace and trace[0]["tool"] == "read_file_range"
    assert trace[0]["args"]["file"] == "src/util.py"


def test_deep_low_confidence_goes_unresolved():
    root = tmp_workspace({"a.py": "x\n"})
    fake = FakeClient(
        [json.dumps({"kind": "submit", "verdict": "not_sensitive",
                     "category": "DOCUMENTATION", "confidence": 0.3,
                     "rationale": "不确定"})]
    )
    j = engine_for(root, fake).judge(finding())
    assert j.category == Category.UNRESOLVED and j.verdict == Verdict.SENSITIVE
    assert reason_of(j) == "low_confidence"


def test_deep_model_timeout_goes_unresolved():
    root = tmp_workspace({"a.py": "x\n"})
    fake = FakeClient([LLMTimeoutError("boom")])
    j = engine_for(root, fake).judge(finding())
    assert j.category == Category.UNRESOLVED
    assert reason_of(j) == "model_timeout"


def test_deep_schema_retry_then_submit():
    root = tmp_workspace({"a.py": "x\n"})
    fake = FakeClient(
        [
            "this is not json at all",
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "r"}),
            json.dumps({"agree": True, "rationale": "ok"}),
        ]
    )
    j = engine_for(root, fake).judge(finding())
    assert j.verdict == Verdict.SENSITIVE
    assert j.category == Category.LIKELY_REAL


def test_deep_critic_disagreement_after_reconsider():
    root = tmp_workspace({"a.py": "x\n"})
    fake = FakeClient(
        [
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "看起来真"}),
            json.dumps({"agree": False, "rationale": "路径是测试样例, 证据不足"}),
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "仍坚持"}),
        ]
    )
    j = engine_for(root, fake).judge(finding(file="tests/sample/x.py"))
    assert j.category == Category.UNRESOLVED
    assert reason_of(j) == "critic_disagreement"


def test_deep_critic_objection_changes_verdict():
    root = tmp_workspace({"a.py": "x\n"})
    fake = FakeClient(
        [
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "误判"}),
            json.dumps({"agree": False, "rationale": "该目录为 docs 示例"}),
            json.dumps({"kind": "submit", "verdict": "not_sensitive",
                        "category": "DOCUMENTATION", "confidence": 0.85,
                        "rationale": "确认是文档示例"}),
        ]
    )
    j = engine_for(root, fake).judge(finding(file="docs/x.md"))
    assert j.verdict == Verdict.NOT_SENSITIVE
    assert j.category == Category.DOCUMENTATION
    crit = [e for e in j.evidence if e["type"] == "critic_review"]
    assert crit and crit[0]["detail"]["agree"] is False


def test_deep_lsp_fallback_flag_on_unknown_language():
    root = tmp_workspace({"app.ts": "const k='x';\n"})
    cfg = Config()  # no lsp configs at all
    fake = FakeClient(
        [
            json.dumps({"kind": "tool_request", "tool": "goto_definition",
                        "args": {"file": "app.ts", "line": 1, "col": 8}}),
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "r"}),
            json.dumps({"agree": True, "rationale": "ok"}),
        ]
    )
    engine = engine_for(root, fake, cfg)
    j = engine.judge(finding(file="app.ts", line=1))
    assert engine.lsp_fallback is True
    trace = [e for e in j.evidence if e["type"] == "tool_trace"]
    assert trace and trace[0]["ok"] is False


# ---------------------------------------------------------------------------
# Aggregation (ACC-07)
# ---------------------------------------------------------------------------

def test_aggregate_same_secret_merges_and_sensitive_wins():
    f1 = finding(file="config/prod.py", line=10, secret="AKIA-SAME-1234")
    f2 = finding(file="services/db.py", line=4, secret="AKIA-SAME-1234")
    j1 = Judgment(finding_id=f1.finding_id, verdict=Verdict.SENSITIVE,
                  category=Category.LIKELY_REAL, confidence=0.9,
                  secret_masked="AKIA[MASKED]1234",
                  locations=[{"file": f1.file_path, "line": 10}],
                  evidence=[{"type": "model_reason", "summary": "prod"}])
    j2 = Judgment(finding_id=f2.finding_id, verdict=Verdict.NOT_SENSITIVE,
                  category=Category.DOCUMENTATION, confidence=0.9,
                  secret_masked="AKIA[MASKED]1234",
                  locations=[{"file": f2.file_path, "line": 4}],
                  evidence=[{"type": "model_reason", "summary": "doc"}])
    out = aggregate([(f1, j1), (f2, j2)])
    assert len(out) == 1
    assert out[0].verdict == Verdict.SENSITIVE  # conservative
    assert len(out[0].locations) == 2
    assert any(e["type"] == "aggregation" for e in out[0].evidence)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def test_orchestrator_fastpath_plus_deep():
    root = tmp_workspace({"config/prod.py": "DB_PASSWORD='sk-live-abcdef123456'\n"})
    ws = Workspace(root)
    docs = finding(file="README.md", line=1, secret="your_token_here-x")
    prod = finding(file="config/prod.py", line=1)
    report = ParsedReport(findings=[docs, prod])
    fake = FakeClient(
        [
            json.dumps({"kind": "submit", "verdict": "sensitive",
                        "category": "LIKELY_REAL", "confidence": 0.9,
                        "rationale": "生产配置"}),
            json.dumps({"agree": True, "rationale": "ok"}),
        ]
    )
    res = run_scan(report, Config(), DEFAULT_RULES, client=fake, workspace=ws)
    assert res.by_verdict == {"sensitive": 1, "not_sensitive": 1}
    assert res.short_circuited == 1 and res.deep_judged == 1
    verdicts = {j.verdict.value for j in res.judgments}
    assert verdicts == {"sensitive", "not_sensitive"}


def test_orchestrator_without_llm_all_pending_unresolved():
    report = ParsedReport(findings=[finding()])
    res = run_scan(report, Config(), DEFAULT_RULES, client=None, workspace=None)
    assert res.judgments[0].category == Category.UNRESOLVED
    assert res.reason_counts.get("degraded_no_workspace") == 1


# ---------------------------------------------------------------------------
# LSP framing + fake-server end-to-end
# ---------------------------------------------------------------------------

def test_lsp_frame_roundtrip_with_interleaved_notification():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "textDocument/definition",
               "params": {"a": 1}}
    stream = io.BytesIO(encode_message(payload))
    msg = read_message(_ByteReader(stream))
    assert msg == payload

    notif = {"jsonrpc": "2.0", "method": "window/logMessage", "params": {}}
    combined = encode_message(notif) + encode_message(payload)
    reader = _ByteReader(io.BytesIO(combined))
    assert read_message(reader) == notif
    assert read_message(reader) == payload


class _QueueStream:
    """Blocking byte stream over a thread queue (in-process pipe)."""

    _EOF = object()

    def __init__(self):
        import queue as _queue

        self._q = _queue.Queue()
        self._buf = b""

    def write(self, data: bytes) -> int:
        self._q.put(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, n: int = -1) -> bytes:
        # File-like semantics: block until >=1 byte, then return what is
        # available (up to n). _ByteReader accumulates partial chunks itself.
        while not self._buf:
            item = self._q.get()
            if item is self._EOF:
                return b""
            self._buf += item
        if n > 0 and len(self._buf) > n:
            out, self._buf = self._buf[:n], self._buf[n:]
        else:
            out, self._buf = self._buf, b""
        return out


def _run_inprocess_lsp_server(c2s, s2c) -> None:
    """Minimal LSP server on queue streams: initialize + definition."""
    import threading

    def serve():
        reader = _ByteReader(c2s)
        while True:
            try:
                msg = read_message(reader)
            except Exception:
                return
            if msg.get("method") == "initialize":
                s2c.write(encode_message({"jsonrpc": "2.0", "id": msg["id"],
                                          "result": {"capabilities": {}}}))
            elif msg.get("method") == "textDocument/definition":
                s2c.write(encode_message({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "uri": "file:///def_target.py",
                    "range": {"start": {"line": 9, "character": 0},
                               "end": {"line": 9, "character": 4}}}}))

    threading.Thread(target=serve, daemon=True).start()


def test_lsp_session_goto_definition_inprocess():
    import threading

    root = tempfile.mkdtemp(prefix="seninfo_lsp_")
    c2s, s2c = _QueueStream(), _QueueStream()
    threading.Thread(target=_run_inprocess_lsp_server, args=(c2s, s2c), daemon=True).start()
    session = LspSession(command=[], cwd=root, timeout_s=3.0, root=root,
                         read=s2c, write=c2s)
    try:
        session.start()
        target = session.goto_definition("caller.py", 1, 1, "import SECRET\n")
        assert target is not None
        assert target["line"] == 10
        assert target["uri"].endswith("def_target.py")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Language registry / preflight
# ---------------------------------------------------------------------------

def test_manager_language_resolution_registry_only():
    from seninfo.engine.lsp import LspManager
    from seninfo.engine.tools import Workspace

    mgr = LspManager(Workspace(tempfile.mkdtemp()))
    for path, lang in [("src/app.py", "python"), ("main.cpp", "c"),
                       ("Main.java", "java"), ("app.rb", "ruby"),
                       ("deploy.sh", "bash"), ("x.rs", "rust"),
                       ("cfg.yaml", "yaml")]:
        assert mgr.language_for(path) == lang, path
    assert mgr.language_id_for("c") == "cpp"
    assert mgr.language_id_for("ts") == "typescript"
    assert mgr.language_id_for("java") == "java"
    assert mgr.language_for("x.scala") is None  # not in registry


def test_installer_path_priority_cache_and_auto_off():
    import os
    from seninfo.engine.lsp_installer import (
        cache_root,
        cached_binary,
        ensure,
        resolve_command,
    )

    saved = {k: os.environ.get(k) for k in ("PATH", "SENINFO_LSP_HOME", "SENINFO_LSP_AUTO")}
    tmp = tempfile.mkdtemp(prefix="seninfo_lspi_")
    try:
        # 1) PATH priority
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        (bindir / "pyright-langserver").write_text("#!/bin/sh\n", encoding="utf-8")
        (bindir / "pyright-langserver").chmod(0o755)
        os.environ["PATH"] = str(bindir)
        assert resolve_command("python") == ["pyright-langserver", "--stdio"]
        # 2) no PATH, auto disabled -> no download attempt, nothing resolved
        os.environ.pop("PATH")
        os.environ["SENINFO_LSP_AUTO"] = "0"
        os.environ["SENINFO_LSP_HOME"] = str(Path(tmp) / "cache")
        assert ensure("python") is None  # auto off -> no npm run
        # java has no npm source -> never auto-downloaded, still None
        assert ensure("java") is None
        # 3) managed cache fallback once a binary is present there
        bin_dir = cache_root() / "python" / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "pyright-langserver").write_text("#!/bin/sh\n", encoding="utf-8")
        resolved = resolve_command("python")
        assert resolved is not None
        assert resolved[0].startswith(str(cache_root()))
        assert cached_binary("python", "pyright-langserver") is not None
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_preflight_lists_registry_defaults_offline():
    from seninfo.engine.preflight import run_preflight

    report = ParsedReport(findings=[finding(file="a.py"), finding(file="b.java")])
    statuses = run_preflight(report, download=False)  # never touches network
    langs = [s["language"] for s in statuses]
    assert langs == ["python", "java"]
    by_lang = {s["language"]: s for s in statuses}
    assert by_lang["python"]["binary"] == "pyright-langserver"
    assert by_lang["python"]["npm_package"] == "pyright"
    assert by_lang["java"]["binary"] == "jdtls"
    assert by_lang["java"]["npm_package"] is None and by_lang["java"]["hint"]
