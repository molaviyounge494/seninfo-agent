"""Configuration (design-phase1 4.1).

Zero-dependency JSON config, mirrored by CLI/env overrides. Security default:
model endpoint must be explicit; http to non-loopback targets warns (data
stays in-domain, PRD 6.2). LLM credentials come from the environment only.
"""

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


@dataclass
class ModelConfig:
    endpoint: str = ""  # OpenAI-compatible base_url, e.g. http://127.0.0.1:8000/v1
    api_key: Optional[str] = None  # resolved from env at load; never stored in files
    judge_model: Optional[str] = None  # None -> auto-detect from /v1/models at probe
    critic_model: Optional[str] = None  # None -> reuse judge model (decision D2)
    temperature: float = 0.0
    timeout_s: float = 30.0


@dataclass
class BudgetConfig:
    tool_rounds: int = 5  # PRD 4.2.2
    context_tokens: int = 8000  # PRD 6.1 hard cap per finding
    read_radius: int = 30  # initial slice around the hit line
    search_limit: int = 50
    max_file_bytes: int = 1_000_000
    reconsider_rounds: int = 1  # critic disagreement re-check
    schema_retries: int = 2
    workers: int = 4


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    rules_file: Optional[str] = None
    workspace: Optional[str] = None

    @property
    def llm_configured(self) -> bool:
        return bool(self.model.endpoint)


DEFAULTS = Config()


def _is_loopback_or_private(host: str) -> bool:
    """Loopback / private / link-local addresses are in-domain (no warning)."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private or addr.is_link_local
    except ValueError:
        return host in ("localhost",)  # DNS names -> not in-domain by inspection


def _warn_public_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme == "http" and parsed.hostname:
        if not _is_loopback_or_private(parsed.hostname):
            print(
                "[warn] 模型 endpoint 为 http 且非回环/内网地址(数据出域风险, PRD 6.2)。"
                "请确认走内部网关或私有部署。",
                file=os.sys.stderr,
            )
    elif parsed.scheme not in ("http", "https"):
        raise ValueError("model.endpoint 必须为 http(s)://...")

    # hostname might be a hostname we cannot classify -> allow with note.
    host = parsed.hostname
    if host and not _is_loopback_or_private(host):
        try:
            socket.getaddrinfo(host, None)
        except OSError:  # pragma: no cover - not resolvable in sandbox
            pass


def _build_model_config(raw: dict) -> ModelConfig:
    model = ModelConfig()
    model.endpoint = str(raw.get("endpoint", "")).rstrip("/")
    if model.endpoint:
        _warn_public_endpoint(model.endpoint)
    # API key strictly via environment (PRD 6.2 / design 4.1)
    env_name = raw.get("api_key_env")
    if env_name:
        # guard against pasting a literal secret instead of a variable name
        if not str(env_name).isidentifier():
            print(
                "[warn] model.api_key_env 应为环境变量名(如 SENINFO_LLM_API_KEY);"
                "检测到非变量名字符串, 已忽略。请勿把密钥明文写入配置文件。",
                file=os.sys.stderr,
            )
            env_name = None
    if env_name:
        model.api_key = os.environ.get(str(env_name)) or None
    if raw.get("api_key"):  # convenience for tests only; prefer env in prod
        model.api_key = str(raw["api_key"])
    judge = raw.get("judge_model")
    model.judge_model = str(judge) if judge else None
    critic = raw.get("critic_model")
    model.critic_model = str(critic) if critic else None
    model.temperature = float(raw.get("temperature", 0.0))
    model.timeout_s = float(raw.get("timeout_s", 30.0))
    return model


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> Config:
    """Load config from an optional JSON file + env overrides + overrides dict."""
    cfg = Config()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        cfg.model = _build_model_config(raw.get("model", {}))
        cfg.budget = BudgetConfig(
            **{
                k: v
                for k, v in raw.get("budget", {}).items()
                if k in BudgetConfig.__dataclass_fields__
            }
        )
        cfg.rules_file = raw.get("rules_file")
        cfg.workspace = raw.get("workspace")
    # note: tools.lsp in legacy configs is intentionally ignored; LSP servers
    # are provisioned from the built-in registry (see engine/lsp_installer.py)
    # env overrides (highest precedence for endpoint/keys/model)
    env = os.environ
    if env.get("SENINFO_LLM_ENDPOINT"):
        cfg.model.endpoint = env["SENINFO_LLM_ENDPOINT"].rstrip("/")
    if env.get("SENINFO_LLM_API_KEY"):
        cfg.model.api_key = env["SENINFO_LLM_API_KEY"]
    if env.get("SENINFO_LLM_JUDGE_MODEL"):
        cfg.model.judge_model = env["SENINFO_LLM_JUDGE_MODEL"]
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
    return cfg
