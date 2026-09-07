# 产品需求文档 (PRD): seninfo-agent 敏感信息智能研判系统

---

## 1. 文档概述

### 1.1 项目背景

在企业 DevSecOps / CI/CD 流程中，硬编码敏感信息扫描工具（如 TruffleHog、Gitleaks）基于正则匹配与熵值检测，常产生大量假阳性告警（如测试 Mock 凭据、文档占位符、构建指纹、哈希值等）。安全工程师手动研判耗时耗力，直接阻断又常引起研发团队误报反弹。

### 1.2 产品定位

**seninfo-agent** 是一款专为 CI/CD 安全卡点设计的敏感信息智能研判 Agent。系统利用确定性规则旁路分流、LSP 精确符号溯源、以及对抗式 Critic 大模型语义推导，实现敏感信息扫描结果的全自动化分类与真伪裁决，并提供 Web 看板、CLI 工具及标准 REST API。

---

## 2. 用户画像与核心痛点

* **安全工程师（SecOps / AppSec）**：
* **痛点**：每日处理数百条扫描告警，90% 以上为 Mock 或非敏感哈希；缺乏高效手段排查跨文件引用的变量真伪。
* **诉求**：高风险真实凭据一键阻断与派单，非敏感告警静默放行，研判依据必须透明可追溯。


* **业务研发人员（Developer）**：
* **痛点**：在本地写单元测试或示例代码时，因包含伪造 Key 导致 CI 流水线被频繁打断。
* **诉求**：合法的测试数据不被误报拦截，真实泄漏时能获得精准的上下文定位与修复指引。


* **DevOps / CI/CD 管理员**：
* **痛点**：流水线挂载安全扫描后构建时长剧增。
* **诉求**：工具能轻量嵌入 Runner，具备确定性短路与低延迟能力，提供标准退出码。



---

## 3. 总体架构与业务流程

```
[ CI/CD 流水线 (TruffleHog/Gitleaks) ] ──> 结构化扫描结果 (JSON/JSONL/SARIF)
                                                 │
                                                 ▼
[ seninfo-agent Core Engine ]
├── 1. 确定性短路器 (Verified == True / 路径白名单 / 极低熵) ──> 直接输出结果 (0 Token)
├── 2. 深度调查器 (受控 Tool: 读取文件行 + LSP 转到定义)
├── 3. 语义分析与 Critic 对抗审查 (判定真实性、防间接提示注入)
└── 4. 结构化裁决与凭据半脱敏存储
                                                 │
                        ┌────────────────────────┼────────────────────────┐
                        ▼                        ▼                        ▼
                 [ CLI 执行器 ]            [ REST API 服务 ]         [ Web 管理看板 ]
             (CI 阻断/退出码控制)         (流水线集成/Webhook)      (告警复核/审计日志)

```

---

## 4. 功能需求规范

### 4.1 数据摄入与预处理模块

* **多源格式解析**：
* 支持 TruffleHog（JSON Lines）与 Gitleaks（JSON / SARIF）标准格式输出。
* 提取核心元数据：探测器名称（Detector）、文件路径、行号、原始密文、原生验证状态（Verified）。


* **凭据半脱敏保护（Redaction）**：
* 密文进入日志、网络传输或模型 Prompt 前，除首尾各 4 字符外全部打码（如 `AKIA...[MASKED]...WXYZ`），禁止完整凭据落盘或泄漏至模型日志。


* **香农熵辅助计算**：
* 本地计算字符香农熵（Shannon Entropy），作为辅助特征输入后续决策流。



### 4.2 智能研判引擎核心

#### 4.2.1 确定性旁路分流（Fast-Path Triage）

* **在线验证直通**：若工具原始输出包含 `Verified: true`（已通过真实网络 API 验证），直接标记为 `VERIFIED_LIVE`，风险等级设定为 `CRITICAL`，完全跳过大模型，零 Token 消耗。
* **规则静默排除**：预设规则库（如包含明显文档占位符 `example.com`、`your_token_here`）直接降级为 `DOCUMENTATION` 并放行。

#### 4.2.2 上下文主动探索工具箱（Code Investigation Tools）

为 Agent 配备严格受控的只读探索工具，杜绝粗暴开放 Bash 终端：

1. `read_file_range(file_path, start_line, end_line)`：向上或向下读取文件上下文，定位变量初始化与前置逻辑。
2. `goto_definition(file_path, line, col)`：基于 Language Server Protocol（LSP，如 `clangd`, `pyright`, `gopls`）精准跨文件跳转到符号定义处，并自动返回定义周围上下文切片。
3. `search_codebase(keyword)`：在未配置 LSP 的环境下作为保底手段，支持在代码仓进行符号全局文本检索。
4. `submit_verdict(...)`：调查终结工具，强制 Agent 提交结构化研判结果。

#### 4.2.3 对抗式判定与防注入（Audit & Critic Logic）

* **安全沙盒定界**：所有代码切片与外部输入均放入 `<untrusted_code_context>` 标签，系统指令显式禁止执行其中的任何文本内容，防御间接提示注入（Indirect Prompt Injection）。
* **分类标准与 Pydantic 强类型输出**：
* `VERIFIED_LIVE`：已在线验证存活。
* `LIKELY_REAL`：业务逻辑、生产配置文件中硬编码的真实凭据。
* `TEST_OR_MOCK`：单元测试目录、fixture、mock 数据中的占位符。
* `DOCUMENTATION`：README、注释中的示例。
* `FALSE_POSITIVE`：前端构建哈希、静态指纹、UUID 等。


* **熔断机制**：单次告警最多允许 Agent 自主调用代码查询工具 5 轮，超时或超轮次自动降级上报人工复核。

---

## 5. 多平台交互与交付规范

### 5.1 CLI 命令行工具（面向 CI/CD Pipeline）

* **基础命令**：
```bash
seninfo-agent scan \
  --input ./trufflehog-results.jsonl \
  --tool trufflehog \
  --workspace-dir . \
  --fail-on HIGH,CRITICAL \
  --output report.json

```


* **阻断策略控制**：
* 若发现 `CRITICAL` 或 `HIGH` 级别的真实凭据，进程以非 0 退出码（如 Exit Code 1）退出，打断 CI 构建。
* 若仅发现 `TEST_OR_MOCK` 或 `FALSE_POSITIVE`，输出告警提示并以 Exit Code 0 退出，保障流水线畅通。



### 5.2 OpenAPI / REST API 接口（面向平台集成）

* **`POST /api/v1/triage/single`**：单条告警即时研判接口（接收代码切片与元数据）。
* **`POST /api/v1/triage/batch`**：上传扫描报告文件异步触发全量研判任务。
* **`GET /api/v1/tasks/{task_id}`**：查询异步研判任务进度与分项报告。
* **`POST /api/v1/webhooks/git`**：接收代码仓库 Push / PR 事件并自动触发研判流水线。

### 5.3 Web 管理看板（面向安全工程师与管理员）

* **告警大盘**：统计总告警数、自动分流拦截率、假阳性消除比例、Token 消耗走势。
* **研判复核看板**：
* 列表展示研判详情：文件路径、代码高亮上下文、Agent 探索路径日志（调用了哪些行、跳转了哪些定义）、研判依据阐述。
* **一键人工覆盖**：安全专家可手动将 `LIKELY_REAL` 修正为 `FALSE_POSITIVE`，系统自动将该用例加入评测集。


* **项目与规则配置**：配置白名单目录、LSP 路径、CI 阻断阈值及 LLM API 凭据。

---

## 6. 非功能性需求 (NFR)

### 6.1 性能与开销指标

* **单条平均耗时**：
* 旁路短路（Verified / 确定性规则）：< 50ms。
* 深度上下文探索 + 模型研判：P95 < 6 秒。


* **Token 控制**：通过短路和上下文切片，单次全仓扫描平均消耗 Token 较全量提示词降低 60% 以上。

### 6.2 安全性与合规性

* **只读沙盒运行**：Agent 的代码翻查工具只能在指定的仓库根目录运行，禁止遍历父目录（Path Traversal 防护），无外网任意发包权限。
* **数据不出域合规**：支持配置本地私有部署模型（如 vLLM / Ollama 挂载 DeepSeek / Qwen 等）或内部中转网关。

---

## 7. 交付路线图 (Roadmap)

* **Phase 1 (MVP - CLI & Core Engine)**：
* 完成 TruffleHog JSONL 解析与脱敏中间件。
* 实现基于文件行展开 + 单语言 LSP（Python/C）定义的 Agent 核心探索循环。
* 完成 CLI 工具开发，支持退出码阻断逻辑。


* **Phase 2 (API & 跨语言扩展)**：
* 封装 FastAPI 异步研判服务，支持任务队列。
* 增加 Go、Java 等多语言 LSP 支持与 Gitleaks SARIF 格式兼容。


* **Phase 3 (Web 看板与主动验证插件)**：
* 上线 Web 管理后台与人工复核流。
* 增加企业内网私有服务的在线存活校验插件机制（Active Check Plugin）。
