"""Minimal LSP client over stdio JSON-RPC (design D3).

Implements just enough for ``textDocument/definition`` against Python
(pyright-langserver / pylsp) and C (clangd): Content-Length framing,
initialize handshake, didOpen, definition request.

Robustness: a background thread decodes inbound messages into a queue, so a
per-request timeout never deadlocks the process. One outstanding request at a
time per session; unsolicited notifications (diagnostics) are discarded.

Real servers are not expected in unit tests: the engine degrades to
``search_codebase`` whenever a session cannot be created (ACC-09).
"""

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from seninfo.engine.lsp_installer import resolve_command
from seninfo.engine.tools import Workspace
from seninfo.lsp_registry import LSP_DEFAULTS, defaults_for


class LspUnavailableError(RuntimeError):
    """Server binary missing, spawn failed, handshake timeout."""


def language_for(path: str) -> Optional[str]:
    """Default-registry language for a path (config-free; manager is the
    authoritative resolver once a scan runs)."""
    from seninfo.lsp_registry import builtin_language_for

    return builtin_language_for(path)


# ---------------------------------------------------------------------------
# Framing (pure helpers, unit-testable without a server)
# ---------------------------------------------------------------------------

def encode_message(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    header = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
    return header + body


class _ByteReader:
    """Buffered reader able to pull exact-size reads from a binary stream."""

    def __init__(self, stream):
        self._stream = stream
        self._buffer = b""

    def read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._stream.read(65536)
            if not chunk:
                raise EOFError("stream closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def read_line(self) -> bytes:
        while True:
            idx = self._buffer.find(b"\r\n")
            if idx != -1:
                line, self._buffer = self._buffer[:idx], self._buffer[idx + 2:]
                return line
            chunk = self._stream.read(65536)
            if not chunk:
                raise EOFError("stream closed while reading headers")
            self._buffer += chunk


def read_message(reader: _ByteReader) -> dict:
    """Read one LSP message (headers + body) from the stream."""
    content_length = 0
    while True:
        line = reader.read_line()
        if line.startswith(b"Content-Length:"):
            content_length = int(line.split(b":", 1)[1].strip())
        if line == b"":  # blank line ends headers
            break
    body = reader.read_exact(content_length)
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class LspSession:
    """One language-server process, safe for serialized cross-thread use."""

    def __init__(self, command: List[str], cwd: str, timeout_s: float = 10.0,
                 root: str = "", read=None, write=None):
        self._timeout = timeout_s
        self._root = Path(root).resolve() if root else Path(cwd).resolve()
        if read is not None and write is not None:  # injectable streams (tests)
            self._proc = None
            self._streams = (read, write)
        else:
            self._proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._streams = (self._proc.stdout, self._proc.stdin)
        self._reader = _ByteReader(self._streams[0])
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._closed = False
        self._request_id = 0
        self._initialized = False
        self._lock = threading.Lock()
        self._reader_thread = threading.Thread(
            target=self._pump, daemon=True, name="lsp-reader"
        )
        self._reader_thread.start()

    # -- background reader ------------------------------------------------

    def _pump(self) -> None:
        while not self._closed:
            try:
                msg = read_message(self._reader)
            except Exception as exc:  # surface reader errors to request()
                if not self._closed:
                    self._inbox.put({"id": None, "__eof": True, "__error": str(exc)})
                return
            self._inbox.put(msg)

    # -- IO ----------------------------------------------------------------

    def _send(self, payload: dict) -> None:
        self._streams[1].write(encode_message(payload))
        self._streams[1].flush()

    def request(self, method: str, params: dict) -> dict:
        """Send a request and wait (timeout-bounded) for its response."""
        deadline = self._timeout
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while deadline > 0:
            try:
                msg = self._inbox.get(timeout=deadline)
            except queue.Empty:
                raise LspUnavailableError("请求 %s 超时(%ss)" % (method, self._timeout))
            if msg.get("__eof"):
                raise LspUnavailableError(
                    "LSP 进程读取错误: %s" % msg.get("__error", "进程提前退出")
                )
            if msg.get("id") == request_id:
                return msg
            # notifications and other responses: drop
        raise LspUnavailableError("请求 %s 超时" % method)

    def notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._initialized:
            return
        root_uri = (self._root.as_uri() if self._root else None)
        resp = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "workspaceFolders": (
                    [{"uri": root_uri, "name": self._root.name}] if root_uri else None
                ),
                "capabilities": {},
            },
        )
        if resp.get("error"):
            raise LspUnavailableError("initialize error: %s" % resp["error"])
        self.notify("initialized", {})
        self._initialized = True

    def open_document(self, uri: str, language_id: str, text: str) -> None:
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

    def goto_definition(
        self, rel_path: str, line_1based: int, col_1based: int, file_text: str,
        language_id: str = "python",
    ) -> Optional[dict]:
        """Return ``{"uri":..., "line": 1based}`` for the first definition."""
        uri = (self._root / rel_path).as_uri()
        self.open_document(uri, language_id, file_text)
        resp = self.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line_1based - 1, "character": col_1based - 1},
            },
        )
        if resp.get("error"):
            raise LspUnavailableError("definition error: %s" % resp["error"])
        return self._first_location(resp.get("result"))

    @staticmethod
    def _first_location(result) -> Optional[dict]:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("targetUri")
            if not uri:
                continue
            target_range = item.get("targetRange") or item.get("range")
            if not isinstance(target_range, dict):
                continue
            start = target_range.get("start") or {}
            line0 = start.get("line")
            if line0 is None:
                continue
            return {"uri": uri, "line": int(line0) + 1}
        return None

    def close(self) -> None:
        self._closed = True
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        # drain thread end (daemon; safe to skip join)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class LspManager:
    """Lazily spawns one session per registry language; fallback-friendly
    (ACC-09). Server discovery is fully registry + managed-cache driven
    (``engine/lsp_installer``); config no longer carries LSP commands.
    """

    def __init__(self, workspace: Workspace):
        self._workspace = workspace
        self._sessions: dict = {}
        self._session_lock = threading.Lock()
        self._closed = False
        self._suffix_map: dict = {}
        self._language_id: dict = {}
        for language, default in LSP_DEFAULTS.items():
            for suffix in default.suffixes:
                self._suffix_map[suffix.lower()] = language
            self._language_id[language] = default.language_id

    def language_for(self, rel_path: str) -> Optional[str]:
        suffix = Path(rel_path).suffix.lower()
        return self._suffix_map.get(suffix)

    def language_id_for(self, language: str) -> str:
        return self._language_id.get(language, language)

    def session_for(self, language: str) -> Optional[LspSession]:
        """Ready session for ``language`` or None when unavailable/fallback."""
        if self._closed or language not in LSP_DEFAULTS:
            return None
        command = resolve_command(language)
        if not command:
            return None
        with self._session_lock:
            if language not in self._sessions:
                session = LspSession(
                    command,
                    str(self._workspace.root),
                    defaults_for(language).timeout_s,
                    root=str(self._workspace.root),
                )
                try:
                    session.start()
                except LspUnavailableError:
                    session.close()
                    return None
                self._sessions[language] = session
            return self._sessions[language]

    def close_all(self) -> None:
        self._closed = True
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
