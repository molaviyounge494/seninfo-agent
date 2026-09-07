# seninfo-agent 使用教程(统一版:C / C++ / Python / Java / 混合仓库)

> 一句话:**研判与语言无关。** 语言只影响一件事——深度溯源用的 LSP 服务器(可选)。装了服务器 → 精确跳转定义;没装 → 自动回落关键词检索(ACC-09),流程不中断。所以下面的步骤**对任何语言完全一样**,差异只在"装哪个 LSP 服务器"和"扫描报告怎么来"。

```
[1 安装 agent] → [2 部署语言服务器] → [3 写 config.json] → [4 生成扫描报告] → [5 跑研判] → [6 验收]
```

---

## 1. 安装 seninfo-agent(三种方式任选)

```bash
# A. 常规安装(本仓库内)
pip install -e .

# B. 不安装,直接跑(零依赖)
PYTHONPATH=src python3 -m seninfo scan ...

# C. 自检(不需要模型,离线可跑)
PYTHONPATH=src:tests python3 tests/run_all.py   # 期望 40 passed
```

> seninfo 只做"研判",不做扫描。**扫描报告需要先用 gitleaks / trufflehog 生成**,步骤 4 有命令。

---

## 2. 语言服务器部署表(按你要扫的语言装)

| 语言 | 服务器 | 安装(Ubuntu/Debian) | config 里写的 command | 备注 |
|---|---|---|---|---|
| Python | pyright(推荐) | `npm install -g pyright` | `["pyright-langserver","--stdio"]` | 或 pylsp:`pip install python-lsp-server`,command 写 `["pylsp"]` |
| C / C++ | clangd | `sudo apt install clangd` | `["clangd"]` | C++ 建议先生成 `compile_commands.json`(见下) |
| Java | jdtls(Eclipse JDT) | 需 JDK 17+ 并安装 eclipse.jdt.ls(见下) | `["jdtls"]` | 重、启动慢;默认超时已调至 60s;首次调用需等索引 |
| TypeScript/JS | typescript-language-server | `npm i -g typescript-language-server typescript` | `["typescript-language-server","--stdio"]` | npm 自动可装 |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` | `["gopls"]` | —— |
| Rust | rust-analyzer | `rustup component add rust-analyzer` | `["rust-analyzer"]` | —— |
| Ruby | solargraph | `gem install solargraph` | `["solargraph","stdio"]` | —— |
| Shell | bash-language-server | `npm i -g bash-language-server` | `["bash-language-server","start"]` | npm 自动可装 |
| YAML | yaml-language-server | `npm i -g yaml-language-server` | `["yaml-language-server","--stdio"]` | npm 自动可装(配置文件常被扫到) |
| PHP / Lua / Kotlin / C# | intelephense / lua-language-server / kotlin-language-server / csharp-ls | 见提示 | 相应默认命令 | 见 `src/seninfo/lsp_registry.py` |

> **重要**:以上 13+ 语言**全部内置默认识别与默认启动命令**(`src/seninfo/lsp_registry.py`),**不再需要任何 config 配置**。扫描前预检按报告实际后缀检查;缺服务器且带 npm 源(pyright/TS/bash/yaml 等)时**自动下载**到 `~/.cache/seninfo/lsp`(默认开启;离线请 `SENINFO_LSP_AUTO=0` 或加 `--no-lsp-download`)。无 npm 源的(clangd/jdtls/gopls/rust-analyzer 等)给出安装提示并自动回落检索。

验证装没装:`which pyright-langserver pylsp clangd jdtls`,有输出=已装。

### 2.1 C++ 生成编译数据库(可选但推荐,clangd 解析 include/宏 需要它)

```bash
# 方法一:CMake 工程
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
ln -s build/compile_commands.json .

# 方法二:非 CMake(用 bear 包裹构建命令)
sudo apt install bear
bear -- make            # 或 bear -- ninja / bear -- <你的构建命令>
```

### 2.2 Java 安装 jdtls(较重,可选)

```bash
# 需要 JDK 17+
java -version

# 下载 eclipse.jdt.ls(以 snapshot 为例,版本号以官方页面为准)
mkdir -p ~/jdtls && cd ~/jdtls
curl -L -o jdtls.tar.gz "https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz"
tar -xzf jdtls.tar.gz

# 建一个软链让 seninfo 能找到(把下面路径换成你解压的真实位置)
sudo ln -s ~/jdtls/bin/jdtls /usr/local/bin/jdtls
```

> jdtls 冷启动需要数秒~数十秒;装好但嫌弃慢时,Java 代码可以**不装**,回落检索照样能给出上下文。

---

## 3. 写 config.json(只需 model + budget,无任何 LSP 段)

```json
{
  "model": {
    "endpoint": "http://127.0.0.1:8000/v1",
    "api_key_env": "SENINFO_LLM_API_KEY",
    "judge_model": "deepseek-chat",
    "critic_model": null,
    "temperature": 0.0,
    "timeout_s": 30.0
  },
  "budget": { "tool_rounds": 5, "context_tokens": 8000, "read_radius": 30,
              "search_limit": 50, "max_file_bytes": 1000000,
              "reconsider_rounds": 1, "schema_retries": 2, "workers": 4 },
  "rules_file": null,
  "workspace": null
}
```

> 无 `tools` 段:语言服务器完全由注册表 + 自动下载管理(见 §2 表格下方说明)。

- `endpoint`:内网模型服务(vLLM/Ollama/网关)的 OpenAI 兼容地址;未配 → 只跑 Fast-Path(未短路项全交人工)。
- `api_key_env`:填**环境变量名**,别把 key 写进文件;运行时 `export SENINFO_LLM_API_KEY=...`。
- `judge_model`:`curl <endpoint>/v1/models` 查真实 id,DeepSeek 官方通常是 `deepseek-chat`。
- **不再需要 LSP 配置段**:服务器命令/超时/安装全部由内置注册表 + 自动下载负责(`src/seninfo/lsp_registry.py` / `engine/lsp_installer.py`)。想换服务器(如 pylsp)或需要新语言时,改代码里的注册表条目即可。
- 仓库根不用写死,`--workspace-dir` 每次指定。

---

## 4. 生成扫描报告(先扫,后研判)

### 用 Gitleaks(适合 C/C++/Java/Go 等)

```bash
# JSON 格式
gitleaks detect --source /path/to/your/repo \
  --report-path ./gitleaks.json --report-format json

# 或 SARIF 格式(两种都支持)
gitleaks detect --source /path/to/your/repo \
  --report-path ./gitleaks.sarif --report-format sarif
```

### 用 TruffleHog(适合 Python/任意仓库)

```bash
trufflehog filesystem /path/to/your/repo --json > trufflehog.jsonl
# (较新版本需显式声明: --only-verified=false; 以 trufflehog filesystem --help 为准)
```

> 要点:**报告里的文件路径是相对仓库根的**(如 `src/main.py`),所以第 5 步的 `--workspace-dir` 必须指向同一仓库根,深度溯源才找得到文件。

---

## 5. 跑研判(命令统一,和语言无关)

```bash
export SENINFO_LLM_API_KEY=<你的key>

PYTHONPATH=src python3 -m seninfo scan \
  --input ./gitleaks.json \        # 扫描报告(JSON/SARIF/JSONL 均可)
  --tool gitleaks \                # gitleaks | trufflehog
  --workspace-dir /path/to/your/repo \
  --config ./config.json \
  --output ./report.jsonl
```

**任何语言的仓库都是这一条命令**,只换 `--tool` 与 `--workspace-dir`。同一仓库混着 C/Python/Java 也没关系:引擎按命中文件后缀自动选 LSP,一个配置管所有语言。

也可以不接模型只去噪:

```bash
PYTHONPATH=src python3 -m seninfo scan --input ./gitleaks.json --tool gitleaks \
  --workspace-dir . --output ./report.jsonl     # 无 --config:纯规则短路,其余交人工
```

---

## 6. 验收核对(每次扫完看三点)

```bash
python3 -m json.tool report.jsonl.manifest.json
```

| 检查项 | 期望 | 说明 |
|---|---|---|
| `counts.by_verdict` | `sensitive`(需人工)+ `not_sensitive`(去噪) | 别只盯总数 |
| `stages.lsp_fallback` | 0 = LSP 生效;1 = 回落(正常,但说明服务器没找到) | 想精确溯源就把它归 0 |
| `stages.deep_judged` | > 0 | 模型真的参与了深判 |
| 结果文件 | 每条含 `evidence`;`secret_masked` 只有首尾 4 字符 | 全文 grep 你的密钥串,应只出现 masked 形态 |

深判记录示例(`report.jsonl` 单行):`goto_definition` 类工具应该出现在 `evidence[].tool` 里,表示真的跳了定义。

---

## 7. 常见问题速查

| 现象 | 原因 → 处理 |
|---|---|
| `模型端点不可达: http 401` | 没带 key → `export SENINFO_LLM_API_KEY=...`,或 config `api_key_env` 填变量名 |
| `model not found` | `judge_model` 写错 → 用 `/v1/models` 查真实 id |
| `lsp_fallback: 1` | 服务器没装/不在 PATH → 看第 2 节安装;回落本身不报错 |
| clangd 找不到头文件/宏 | 没编译数据库 → 生成 `compile_commands.json`(2.1) |
| jdtls 调用超时 | 冷启动慢 → config `java.timeout_s: 60`,并预热一次 |
| 全是 `UNRESOLVED` | ①没配模型(纯 Fast-Path)②`--workspace-dir` 与报告路径不一致 ③模型弱(看原因词表) |
| 输出疑似有明文 | 违反脱敏,视为 bug → 报告作者 |
| `.ts/.go` 提示 LSP 不存在 | 那是 Phase 2 目标;回落检索即可,或提前装对应服务器也能用 |

---

## 8. 样例(跟着练一遍)

```bash
# Python:直接扫本仓库自带样例
PYTHONPATH=src python3 -m seninfo scan \
  --input tests/fixtures/inputs/acc-06_trufflehog.jsonl \
  --tool trufflehog \
  --workspace-dir tests/fixtures/workspaces/acc-06-minimal \
  --config config.example.json \
  --output /tmp/acc06.jsonl

# 结果对照 docs/runbook-phase1-validation.md §3 的通过标准
```

> 配置与验收更多细节:`docs/runbook-phase1-validation.md`;ACC 用例规格:`docs/acceptance-cases.md`。
