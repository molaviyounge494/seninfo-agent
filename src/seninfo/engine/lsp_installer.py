"""Managed LSP server provisioning (default-ON auto download).

Servers are installed with ``npm install --prefix`` into a per-language cache
under ``SENINFO_LSP_HOME`` (default ``~/.cache/seninfo/lsp``) — no system-wide
``npm -g``, no PATH pollution, consistent across Linux/macOS/Windows (npm is
the single distribution channel for every npm-installable server).

Behaviour:
  * Discovery order per language: PATH → managed cache.
  * Auto-download happens by default when a server is missing and has an
    ``npm_package`` source; disable with ``SENINFO_LSP_AUTO=0`` or the CLI
    ``--no-lsp-download`` (offline / locked-down runners).
  * Languages without an npm source (clangd, jdtls, gopls, ...) are reported
    with an install hint and keep the search fallback.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from seninfo.lsp_registry import LSP_DEFAULTS, defaults_for


def _auto_enabled() -> bool:
    value = os.environ.get("SENINFO_LSP_AUTO", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def cache_root() -> Path:
    return Path(os.environ.get("SENINFO_LSP_HOME") or
                (Path.home() / ".cache" / "seninfo" / "lsp")).resolve()


def _bin_candidates(bin_dir: Path, binary: str) -> List[Path]:
    # npm bin shims: posix script + windows .cmd/.exe
    return [bin_dir / binary, bin_dir / (binary + ".cmd"), bin_dir / (binary + ".exe")]


def cached_binary(language: str, binary: str) -> Optional[Path]:
    """Return the managed binary path for ``language`` if already installed."""
    bin_dir = cache_root() / language / "node_modules" / ".bin"
    for candidate in _bin_candidates(bin_dir, binary):
        if candidate.exists():
            return candidate
    return None


def resolve_command(language: str) -> Optional[List[str]]:
    """Full launch argv for ``language``: PATH first, then managed cache."""
    default = defaults_for(language)
    if default is None:
        return None
    binary = default.command[0]
    if shutil.which(binary):
        return list(default.command)
    cached = cached_binary(language, binary)
    if cached is not None:
        return [str(cached)] + list(default.command[1:])
    return None


def ensure(language: str) -> Optional[List[str]]:
    """Resolve or (auto-download) install; None when unavailable."""
    resolved = resolve_command(language)
    if resolved is not None:
        return resolved
    default = defaults_for(language)
    if default is None or not default.npm_package or not _auto_enabled():
        return None
    target = cache_root() / language
    pkg = default.npm_package
    print("  [lsp] 自动下载 %s -> npm install --prefix %s %s (缓存:%s)"
          % (language, target, pkg, cache_root()))
    try:
        target.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["npm", "install", "--prefix", str(target),
             "--ignore-scripts", "--no-audit", "--no-fund", pkg],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            print("  [lsp] %s 安装失败: %s"
                  % (language, (proc.stderr or proc.stdout)[-300:]),
                  file=sys.stderr)
            return None
    except Exception as exc:  # noqa: BLE001 - network/CLI errors tolerated
        print("  [lsp] %s 安装异常(已回落检索): %s" % (language, exc),
              file=sys.stderr)
        return None
    return resolve_command(language)


def installable_languages() -> List[str]:
    """Registry languages that can be auto-downloaded (npm source)."""
    return [lang for lang, d in LSP_DEFAULTS.items() if d.npm_package]
