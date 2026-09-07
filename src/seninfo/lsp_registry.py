"""LSP registry: suffix -> language -> default server command + install hints.

The registry is the single source of truth for "which server, how to launch,
how to install" per language. The engine resolves ANY registry language
automatically and provisions missing npm-installable servers on demand
(see ``engine/lsp_installer``); config does not carry LSP commands anymore.
Adding a new language = one registry entry here.

``npm_package`` set => auto-downloadable via npm (default ON; disable with
``SENINFO_LSP_AUTO=0``); others carry an install hint (system package / go
install / etc.).
"""

from pathlib import Path
from typing import Dict, List, Optional


class LspDefaults:
    __slots__ = ("language_id", "command", "suffixes",
                 "npm_package", "hint", "timeout_s")

    def __init__(self, language_id: str, command: List[str], suffixes: List[str],
                 timeout_s: float = 10.0,
                 npm_package: Optional[str] = None, hint: str = ""):
        self.language_id = language_id
        self.command = list(command)
        self.suffixes = list(suffixes)
        self.timeout_s = timeout_s
        self.npm_package = npm_package
        self.hint = hint

    @property
    def binary(self) -> str:
        return self.command[0]


LSP_DEFAULTS: Dict[str, LspDefaults] = {
    "python": LspDefaults(
        language_id="python", command=["pyright-langserver", "--stdio"],
        suffixes=[".py"], npm_package="pyright",
        hint="npm install -g pyright(或 pip install python-lsp-server 并在 config 覆盖 command=[\"pylsp\"])",
    ),
    "c": LspDefaults(
        language_id="cpp", command=["clangd"],
        suffixes=[".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"],
        hint="sudo apt install clangd;C++ 建议生成 compile_commands.json(见教程 §2.1)",
    ),
    "java": LspDefaults(
        language_id="java", command=["jdtls"], suffixes=[".java"], timeout_s=60.0,
        hint="JDK17+ 与 eclipse.jdt.ls(jdtls), 见 docs/tutorial-user.md §2.2",
    ),
    "ts": LspDefaults(
        language_id="typescript",
        command=["typescript-language-server", "--stdio"],
        suffixes=[".ts", ".tsx", ".js", ".jsx"], npm_package="typescript-language-server",
        hint="npm install -g typescript-language-server typescript",
    ),
    "go": LspDefaults(
        language_id="go", command=["gopls"], suffixes=[".go"],
        hint="go install golang.org/x/tools/gopls@latest",
    ),
    "rust": LspDefaults(
        language_id="rust", command=["rust-analyzer"], suffixes=[".rs"],
        hint="rustup component add rust-analyzer",
    ),
    "ruby": LspDefaults(
        language_id="ruby", command=["solargraph", "stdio"], suffixes=[".rb", ".rake"],
        hint="gem install solargraph",
    ),
    "lua": LspDefaults(
        language_id="lua", command=["lua-language-server"], suffixes=[".lua"],
        hint="brew install lua-language-server / 发行版包 / 官方 release",
    ),
    "bash": LspDefaults(
        language_id="shellscript", command=["bash-language-server", "start"],
        suffixes=[".sh", ".bash", ".zsh"], npm_package="bash-language-server",
        hint="npm install -g bash-language-server",
    ),
    "php": LspDefaults(
        language_id="php", command=["intelephense", "--stdio"], suffixes=[".php"],
        npm_package="intelephense",
        hint="npm install -g intelephense(需 license key 否则限单文件)",
    ),
    "yaml": LspDefaults(
        language_id="yaml", command=["yaml-language-server", "--stdio"],
        suffixes=[".yaml", ".yml"], npm_package="yaml-language-server",
        hint="npm install -g yaml-language-server",
    ),
    "kotlin": LspDefaults(
        language_id="kotlin", command=["kotlin-language-server"], suffixes=[".kt", ".kts"],
        hint="github.com/fwcd/kotlin-language-server 发行版",
    ),
    "csharp": LspDefaults(
        language_id="csharp", command=["csharp-ls"], suffixes=[".cs"],
        hint="dotnet tool install --global csharp-ls",
    ),
}

# Any new language follows the same pattern; add one entry, e.g.:
#   "scala": LspDefaults("scala", ["scala-cli", "lsp"], [".scala", ".sc"], ...)


def defaults_for(language: str) -> Optional[LspDefaults]:
    return LSP_DEFAULTS.get(language)


def builtin_language_for(path: str) -> Optional[str]:
    """Resolve a path suffix to its default-registry language."""
    suffix = Path(path).suffix.lower()
    for language, defaults in LSP_DEFAULTS.items():
        if suffix in defaults.suffixes:
            return language
    return None
