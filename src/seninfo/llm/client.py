"""OpenAI-compatible chat client (design decision D1, PRD 6.2).

Stdlib urllib only — works against vLLM / Ollama / internal gateway without
extra dependencies. Any failure surfaces as a typed LLMError so the engine
can map it to UNRESOLVED reasons (ACC-08).
"""

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from seninfo.config import ModelConfig


class LLMError(RuntimeError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMHttpError(LLMError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__("http %d: %s" % (status, body[:300]))


class LLMProtocolError(LLMError):
    pass


class ChatClient:
    """Minimal OpenAI-compatible ``/chat/completions`` client."""

    def __init__(self, cfg: ModelConfig):
        if not cfg.endpoint:
            raise ValueError("model endpoint 未配置")
        self.endpoint = cfg.endpoint.rstrip("/")
        self.model = cfg.judge_model
        self.critic_model = cfg.critic_model
        self.api_key = cfg.api_key
        self.timeout_s = cfg.timeout_s
        self.temperature = cfg.temperature

    # -- HTTP --------------------------------------------------------------

    def _request(self, payload: Dict[str, Any], timeout: Optional[float]) -> Dict[str, Any]:
        url = self.endpoint + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMHttpError(exc.code, detail)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise LLMTimeoutError("请求超时/网络错误: %s" % exc)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMProtocolError("响应非 JSON: %s" % exc)

    # -- API ---------------------------------------------------------------

    def complete(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": model or self.model or "default",
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        data = self._request(payload, timeout)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMProtocolError("响应缺少 choices[0].message.content")

    def list_models(self) -> List[str]:
        url = self.endpoint + "/models"
        req = urllib.request.Request(url, method="GET")
        if self.api_key:
            req.add_header("Authorization", "Bearer %s" % self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as exc:
            raise LLMError("模型探测失败: %s" % exc)
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if not ids:
            raise LLMProtocolError("/v1/models 未返回可用模型")
        return ids

    def probe(self) -> str:
        """Connectivity check at CLI startup (design D5). Resolves model id."""
        if not self.model:
            models = self.list_models()
            self.model = models[0]
        reply = self.complete(
            [{"role": "user", "content": "回复 OK 即可"}], timeout=min(10.0, self.timeout_s)
        )
        return reply
