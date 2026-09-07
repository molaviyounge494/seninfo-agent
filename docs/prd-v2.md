# 产品需求文档 (PRD v2.0): seninfo-agent 敏感信息智能研判 Agent

> 状态:评审稿 ｜ 日期:2026-09-06 ｜ 取代范围:`docs/prd.md`(v1.0)
>
> 本版本依据边界收敛评审结论重写:本工具**只做"研判"一格**,即"读取静态扫描器输出 → AI 判定是否真实敏感信息 → 输出结构化裁决结果与导出接口"。不做人工审核界面、不做缺陷/漏洞流转、不做 CI 阻断。

---

## 0. 本次修订摘要(相对 v1.0)

| 处理 | 内容 |
|---|---|
| ✂️ 删除 | Web 管理看板、人工覆盖与"加入评测集"、一键阻断与派单、主动验证插件(Active Check) |
| 🔄 收敛 | 输出从"5 类平级分类"改为 **二元结论(敏感/非敏感)+ 原因子类**;CLI 从"阻断退出码"改为"只判定不阻断,退出码仅表示工具自身成败" |
| ➕ 新增 | 统一的"归一化告警模型"与解析器扩展机制;结果记录 Schema 与外部人工审核平台的消费契约;逐条失败兜底语义(UNRESOLVED 转人工);脱敏矩阵附录 |
| ♻️ 保留 | 确定性短路 → 受控探索工具 → Critic 对抗判定 的分层引擎;防注入沙盒;只读/无外网约束;数据不出域 |

---

## 1. 概述

### 1.1 背景

TruffleHog、Gitleaks 等静态扫描器基于正则与熵检测,产生大量假阳性(测试 Mock、文档占位符、构建指纹、哈希等)。安全工程师需在"漏放真凭据"与"误报淹没真告警"之间做大量人工研判。

### 1.2 产品定位(一句话)

**seninfo-agent 接收静态扫描器输出,对每条告警判定"是否为真实敏感信息",输出带证据链的裁决结果,交付下游人工审核环节。**

它在业务链路中的位置:

```
[ 静态扫描器(TruffleHog/Gitleaks) ] → [ seninfo-agent: 自动研判 ] → [ 人工审核(外部) ] → [ 缺陷流转平台(外部) ]
                                        ▲ 本工具只做这一格(不含 UI 与流转)
```

### 1.3 范围边界

| 范围内(本版本承诺) | 范围外(明确不做) |
|---|---|
| 解析 TruffleHog JSONL、Gitleaks JSON/SARIF(解析器可扩展) | 问题提单、派单、状态跟踪、闭环管理 |
| 凭据脱敏(贯穿日志/模型/存储/导出) | 人工审核界面、人工覆盖判定 |
| 逐条研判:确定性短路 + 受控代码探索 + LLM 裁决 | 由本工具阻断 CI(退出码仅表工具自身成败) |
| 结构化裁决结果 + 证据链 + 导出接口(文件/API) | 主动联网验证凭据(仅透传扫描器原生 verified) |
| 全仓只读上下文探索(含 LSP 跨文件溯源) | 任何写操作、外网发包、Shell 执行 |

**关键设计前提(已与业务方确认)**:
1. 下游总有人工审核兜底,本工具不阻断、不静默吞掉任何一条 —— 判不出来的必须显式交给人工(UNRESOLVED)。
2. 研判时 CI 中**存在完整仓库 checkout**,可做 LSP 跨文件溯源;仅当无仓库时才退化为"报告内置上下文"模式。
3. 扫描器自带的 `verified` 状态仅作为高置信输入,本工具不自行发起网络验证。

---

## 2. 业务链路与角色

### 2.1 一条告警的完整旅程(外部视角)

1. CI 中扫描器(如 TruffleHog)产出结构化报告并执行本工具 `scan`。
2. 本工具逐条判定,输出结果文件(JSONL)+ 摘要;敏感项与无法判定项带证据链导出。
3. 外部人工审核平台消费结果文件/API,**只对 `sensitive` 与 `UNRESOLVED` 项复核**;`not_sensitive` 项默认不进人工(组织可配置抽样复核)。
4. 人工确认后由外部平台进入缺陷流转。

### 2.2 用户

| 用户 | 使用场景 | 诉求 |
|---|---|---|
| 安全工程师(AppSec) | 消费裁决结果做人工复核 | 只看"需要人看的少数项";每条附代码位置、证据链;不再逐条翻假阳性 |
| 业务研发 | CI 中扫描后自动去噪 | 测试数据/示例代码不被误报,流水线只获得"待人工确认清单"而非整包告警 |
| CI/CD 管理员 | Runner 内轻量嵌入 | 轻量、低延迟、无外网依赖、标准退出码(与判定结果解耦) |

---

## 3. 总体架构

```
[ TruffleHog JSONL / Gitleaks JSON / Gitleaks SARIF ]        ← 输入(扫描器产物)
                          │
                          ▼
[ 解析器层 ] 各家格式 → 统一归一化告警模型 NormalizedFinding
                          │
                          ▼
[ 研判引擎 ]
├── 1. 确定性短路  (verified=true / 规则库命中)   ──→ 0 Token 直接出结果
├── 2. 上下文探索    (read_file_range / goto_definition / search_codebase)  ← 完整仓库
├── 3. 模型裁决 + Critic 对抗审查(防间接提示注入)
└── 4. 失败兜底      (超时/超轮次/模型不可用 → UNRESOLVED 交人工)
                          │
                          ▼
[ 结果与导出 ]
├── CLI:  结果文件(JSONL/SARIF)+ stdout 摘要 + 非阻断退出码
└── 服务:  REST 批量研判 → 任务进度 → 结果下载(供外部平台拉取/订阅)
```

---

## 4. 功能需求

### 4.1 输入层:解析与归一化

#### 4.1.1 支持的源与字段映射

解析器实现统一接口(`Parser → List[NormalizedFinding]`),各源产出一致的中间模型,后续引擎与格式无关;新增扫描器只需新写一个 Parser。

| 源 | 输入形态 | 关键字段映射 | 原生验证 |
|---|---|---|---|
| TruffleHog | JSONL | `DetectorName`→`detector`,`SourceMetadata.Data.File`→`file_path`,`SourceMetadata.Data.Line`→`line`,`Raw`→`secret_raw`,`Verified`→`verified` | 支持(true/false) |
| Gitleaks | JSON / SARIF | `RuleID`→`detector`,`File`→`file_path`,`StartLine`→`line`,`Secret`→`secret_raw`,`Match`→`match_context` | 不支持(置 null) |

归一化模型字段(最小集):
```
finding_id       # 稳定 ID:hash(tool, repo, file, line, secret_masked) —— 外部平台据此关联人工结论
tool / detector  # 来源与规则名(如 gitleaks/aws-access-token)
file_path / line # 命中位置(仓库相对路径)
secret_raw       # 原始密文(仅存在于内存,见附录 A)
match_context    # 命中行上下文(可选)
verified         # true | false | null(扫描器验证结论,仅透传)
entropy          # 香农熵(本地计算,仅作模型辅助特征,不单独作为裁决依据)
```

#### 4.1.2 解析健壮性

- 单条记录解析失败:跳过该条,在 manifest 中计数为 `parse_errors`,并产出 1 条 `UNRESOLVED` 占位(含原始片段引用),**不静默丢弃**。

### 4.2 研判引擎

#### 4.2.1 确定性短路(Fast-Path,0 Token)

按顺序评估,命中即出结果、不进入模型:

1. **在线验证直通**:`verified == true` → `verdict=sensitive`,子类 `VERIFIED_LIVE`,置信度 1.0。
2. **规则库静默排除**:命中规则(文档占位符 `example.com`、`your_token_here`、常见 Mock 前缀、白名单目录等)→ `verdict=not_sensitive`,子类按规则声明(`TEST_OR_MOCK`/`DOCUMENTATION`/`FALSE_POSITIVE`)。
   - 规则库由**可扩展规则文件**驱动(正则/glob/目录规则),非硬编码;支持按项目覆盖。规则需可溯源(结果中记录命中规则 ID)。

> 说明:香农熵不单独短路(短 Token、真短密钥熵可能很低),仅作为 LLM 输入特征。白名单目录同样走规则库、必须可解释。

#### 4.2.2 上下文探索工具箱(需完整仓库)

严格受控、只读、无 Shell、禁越界,仅以下工具:

| 工具 | 说明 | 越界防护 |
|---|---|---|
| `read_file_range(path, start, end)` | 上下扩展读取文件上下文(单次行数上限可配) | 路径 join 仓库根后校验 resolve 结果仍在根内 |
| `goto_definition(path, line, col)` | LSP 跳转符号定义并返回定义处上下文切片 | 同上;按需启动语言服务器并缓存 |
| `search_codebase(keyword)` | 全仓符号/文本检索(LSP 不可用或未覆盖语言时的兜底) | 仅仓库根内;结果截断 |
| `submit_verdict(...)` | 终结工具,强制提交结构化结果 | —— |

- 语言服务器映射:Python→pyright/pylsp、C/C++→clangd 为 Phase 1;Go/Java/TypeScript/JavaScript 为 Phase 2;未覆盖语言直接回落 `search_codebase`。
- 单条告警探索预算(默认,可配):工具调用 ≤ 5 轮、进入模型的上下文 token 累计 ≤ 8k。

#### 4.2.3 模型裁决与对抗式审查

- 所有代码切片与外部输入包裹 `<untrusted_code_context>`,系统指令显式禁止执行其中任何文本,防御间接提示注入。
- 主判模型输出:**二元结论 + 子类 + 置信度 + 依据摘要**;Critic 模型对主判结论做对抗复核(重点检查是否被上下文诱导、是否与证据矛盾),不一致或低置信 → 降级 `UNRESOLVED`。
- 模型仅能通过 4.2.2 工具获取信息;输出强制走结构化 Schema 校验(如 Pydantic),不合规重试 1 次后转 `UNRESOLVED`。

#### 4.2.4 失败与降级语义(核心安全约束)

**任何一条无法完成研判,绝不静默放行、也不触发阻断** —— 一律输出为 `verdict=sensitive` + 子类 `UNRESOLVED`(标注原因:超时/超轮次/模型不可用/解析失败/低置信),交由人工兜底。原因与统计写入 manifest。

#### 4.2.5 去重与聚合

- 同一仓库中密文值相同(或归一化后相同)的多处命中聚合为**一条裁决记录**,保留全部命中位置与溯源链(跨文件引用一并归并到"泄漏事件"),避免看板/下游重复。
- 聚合不影响逐条字段导出:结果中 `locations: []` 列出全部位置。

### 4.3 输出契约(核心)

裁决结果 = **二元结论(下游据此分流)+ 原因子类(解释为什么)+ 证据链(供人工复核)**。

#### 4.3.1 字段定义

| 字段 | 取值 | 说明 |
|---|---|---|
| `verdict` | `sensitive` / `not_sensitive` | 二元结论。`sensitive` 与 `UNRESOLVED` 需人工复核;`not_sensitive` 默认放行(可配抽样复核) |
| `category` | 见下表 | 原因子类,辅助下游归类与"非问题状态"区分 |
| `confidence` | 0–1 | 模型/规则置信;低于阈值强制转 `UNRESOLVED` |
| `evidence[]` | 结构化条目 | 短路规则 ID / 模型依据摘要 / 工具调用轨迹(读了哪些行、跳转哪些定义) / 关键代码切片引用 |
| `locations[]` | 文件+行 | 聚合后的全部命中与溯源位置 |
| `secret_masked` | 半脱敏串 | 见附录 A;明文不落盘,下游可凭 `finding_id` 关联扫描器原始报告 |

#### 4.3.2 子类与映射

| verdict | category | 含义 | 典型证据 |
|---|---|---|---|
| sensitive | `VERIFIED_LIVE` | 扫描器已验证存活(透传,高置信) | 原生 verified=true |
| sensitive | `LIKELY_REAL` | 业务逻辑/生产配置中硬编码,高度疑似真实凭据 | 溯源到真实调用/配置路径 |
| sensitive | `UNRESOLVED` | 无法判定/工具失败/低置信 → **必须人工** | 失败原因/探索轨迹 |
| not_sensitive | `TEST_OR_MOCK` | 测试目录/fixture/mock 占位符 | 路径与内容规则、定义溯源 |
| not_sensitive | `DOCUMENTATION` | README/注释/示例代码 | 规则/上下文 |
| not_sensitive | `FALSE_POSITIVE` | 构建哈希、静态指纹、UUID、误匹配 | 规则/语义判定 |

#### 4.3.3 判定优先级

1. 确定性短路(规则/verified)优先于模型结论;
2. 短路未命中 → 模型裁决 → Critic 复核;
3. 任何环节失败/不确定 → 上表 `UNRESOLVED`(敏感侧,交人工),**优先级高于一切放行判断**。

---

## 5. 交付与集成

### 5.1 CLI(`seninfo-agent scan`)

```bash
seninfo-agent scan \
  --input ./trufflehog-results.jsonl \
  --tool trufflehog \
  --workspace-dir . \            # 完整仓库;缺省则进入"仅报告上下文"模式(见 5.1.1)
  --rules ./rules.json \        # 可选:项目级规则覆盖(JSON)
  --output report.jsonl \
  --format jsonl                 # jsonl | sarif(可选)
```

**退出码语义(与判定结果解耦,不阻断)**:

| 退出码 | 含义 |
|---|---|
| 0 | 工具正常运行完成(无论发现多少敏感项) |
| 2 | 工具/配置错误(输入不可读、模型端点不可达导致整批未判等) |

- stdout 输出摘要:`total / sensitive / not_sensitive / unresolved / short_circuited / token_usage / duration`;详细统计入 manifest。
- 敏感项**不**使进程失败 —— 阻断与否由外部人工审核/流转流程决定(本工具无此职责)。

#### 5.1.1 无仓库降级模式

未提供 `--workspace-dir` 时,跳过 4.2.2 探索工具,仅用报告携带上下文 + 规则库判定;无法仅凭上下文确定的一律 `UNRESOLVED`,并在 manifest 标记 `degraded_mode=true`。

### 5.2 REST 服务(面向外部审核平台集成)

| 接口 | 说明 |
|---|---|
| `POST /api/v1/triage/batch` | 上传扫描报告(附 workspace 路径或文件),异步触发批量研判,返回 `task_id` |
| `GET /api/v1/tasks/{task_id}` | 任务进度:处理数/总数/状态/错误 |
| `GET /api/v1/tasks/{task_id}/results` | 完成后拉取裁决结果(JSONL) |
| `GET /api/v1/tasks/{task_id}/manifest` | 统计与失败明细 |

- 鉴权:内部部署默认 Bearer Token;支持 mTLS(可配)。不做多租户/角色权限(单作业队列,见 6.2)。
- 结果推送(可选):支持配置 webhook sink,任务完成时向外部平台 POST 结果地址(签名校验可配)。

### 5.3 导出文件与消费契约(外部人工审核平台对接要点)

- 结果 JSONL 每行 = 一条裁决记录(4.3.1 Schema),按 `verdict` 分流:`not_sensitive` 行可被下游直接归类"非问题";`sensitive` 行进入人工队列。
- **关联原文**:本工具导出不含明文(附录 A);提供 `finding_id` 与原始 `tool`+`file`+`line`+`secret_masked`,外部平台可 join 扫描器原始报告取原文复核。
- 证据链是交付物的一部分:人工复核人必须能看到"为什么判的"(规则 ID/探索轨迹/依据),这是本工具可信度的核心。
- 稳定 `finding_id` 支持外部平台回写人工结论(评测闭环归外部平台,本版本不做接收)。

### 5.4 明确不在本版本交付

Web 看板、人工复核/覆盖 UI、缺陷流转对接、主动联网验证 —— 均由外部环节承担。

---

## 6. 非功能性需求

### 6.1 性能与成本(验收口径见第 7 节)

| 指标 | 目标 | 说明 |
|---|---|---|
| 单条短路(Fast-Path) | < 50 ms | 规则/verified 直通,0 Token |
| 单条深度研判 P95 | < 6 s | 含上下文探索 + 模型推理(LSP 冷启动预热后) |
| Token 削减 | ≥ 60% | 口径:相对"无短路、每条全量上下文进模型"基线;manifest 记录实际用量 |
| 探索预算 | ≤5 轮 / ≤8k tokens / 条 | 可配,超限转 UNRESOLVED |

### 6.2 安全与合规

- **只读沙盒**:探索工具仅限 `workspace-dir` 根内(路径 resolve 校验防穿越);无 Shell、无写操作、无任意外网发包。
- **数据不出域**:支持本地/内网私有模型(vLLM/Ollama 挂载 DeepSeek/Qwen 等)或内部网关;默认关闭公网模型端点。
- **凭据脱敏**:密文进入日志/模型 Prompt/存储/导出前一律按附录 A 矩阵处理;完整密文只短暂存在于进程内存。
- **密钥管理**:LLM API 凭据经环境变量/挂载注入,不落配置文件与日志。
- **审计**:manifest 与单条 evidence 记录判定版本(引擎版本、规则库版本、模型标识),保证"当时为何这样判"可回溯。

### 6.3 可观测性

- 输出物自带统计(manifest):总数、按 category 分布、短路/模型占比、UNRESOLVED 及原因、token 用量、耗时、解析错误数。
- 日志仅记录脱敏内容(附录 A),级别可配。

---

## 7. 验收

### 7.1 典型验收场景(开发测试用例基准)

> 场景的可执行规格、断言点与 fixtures 约定见 **`docs/acceptance-cases.md`**(ACC-01 ~ ACC-12)。

| # | 场景 | 期望 verdict / category |
|---|---|---|
| 1 | TruffleHog 输出 `Verified: true` 的 AWS Key | sensitive / VERIFIED_LIVE(0 Token) |
| 2 | 命中规则库占位符 `your_token_here` | not_sensitive / DOCUMENTATION(0 Token) |
| 3 | `tests/` 下 mock key,需溯源文件头注释"test only" | not_sensitive / TEST_OR_MOCK |
| 4 | README 示例 `sk-xxx…example` | not_sensitive / DOCUMENTATION |
| 5 | 前端构建产物 64 位哈希指纹 | not_sensitive / FALSE_POSITIVE |
| 6 | 业务代码 `config.py` 硬编码真 DB 口令,且被生产模块 import | sensitive / LIKELY_REAL(evidence 含 goto_definition 轨迹) |
| 7 | 密钥在 A 文件定义、B 文件引用,两处均被 detector 命中 | 聚合为 1 条 sensitive / LIKELY_REAL,`locations` 含两处 |
| 8 | 模型端点超时(模拟) | sensitive / UNRESOLVED,manifest 记录原因,退出码 0 |
| 9 | 仓库为 JS/TS 且无对应 LSP(Phase 1) | 回落 search_codebase,仍产出裁决,manifest 标记 |
| 10 | 输入文件含 1 条损坏 JSON | 该条 UNRESOLVED + manifest parse_errors=1,其余正常判定 |
| 11 | 无 `--workspace-dir`(降级模式) | 仅报告上下文判定,不确定者 UNRESOLVED,degraded_mode=true |
| 12 | Gitleaks SARIF 输入(Phase 1 起支持) | 与 JSON 输入产出同构 NormalizedFinding |

### 7.2 建议默认指标(可调整,待运营校准)

> 业务方暂无既定指标,以下为建议默认值,上线后按真实分布校准:

1. 人工标注 Golden Set(≥200 条)判定一致率 ≥ 90%(`seninfo-agent eval` 离线路测,Phase 3 提供);
2. 真实敏感被误判为 `not_sensitive`(漏放行)比例 ≤ 1%;
3. 需人工复核率 = (sensitive + UNRESOLVED)/ 总告警 ≤ 20%,即短路与自动放行消化 ≥ 80% 噪声。

---

## 8. 路线图(Roadmap)

| 阶段 | 内容 | 完成定义(DoD) |
|---|---|---|
| **Phase 1(MVP)** | TruffleHog JSONL + Gitleaks JSON/SARIF 解析与归一化;脱敏中间件;确定性规则库(文件驱动);工具集(read_file_range + search_codebase + Python/C LSP goto_definition);LLM 二元裁决 + Critic;CLI scan + JSONL 导出 + 非阻断退出码 | 7.1 场景 1–12 在样例仓全绿;manifest 统计完整 |
| **Phase 2(服务化)** | FastAPI 批量研判 + 任务/结果 API + webhook sink;LSP 扩展 Go/Java/TS/JS;SARIF 结果导出 | 场景 9 通过;外部平台可按 5.3 契约消费结果 |
| **Phase 3(质量与扩展)** | `eval` 离线路测工具与 Golden Set 流程;解析器扩展若干(如 Semgrep);规则库运营与性能优化 | 7.2 指标可离线路测复现 |

---

## 附录 A:凭据脱敏矩阵

| 载体 | 密文处理 | 谁能见原文 |
|---|---|---|
| 进程内存(解析/判定) | 原文 | 仅引擎 |
| 日志 | 首尾各 4 字符外打码(`AKIA…[MASKED]…WXYZ`) | —— |
| LLM Prompt | 同上(半脱敏) | 模型(受信任/内网) |
| 结果文件 / API / 存储 | 同上(半脱敏);`finding_id` + 原始位置供外部 join 扫描器原文 | 外部需原文时自行关联 |
| 明文导出 | 默认禁止;如需明文交付,须显式开启 `export_include_raw=true`(建议仅受控通道,并记录审计) | 显式授权 |

> 说明:人工审核在外部平台进行,复核原文由外部平台从**扫描器原始报告**关联取得,本工具导出保持脱敏,从根上避免二次扩散。

## 附录 B:裁决记录 JSON 示例

```json
{
  "finding_id": "8f3a2c9e1b7d4a5f",
  "verdict": "sensitive",
  "category": "LIKELY_REAL",
  "confidence": 0.93,
  "tool": "gitleaks",
  "detector": "aws-access-token",
  "secret_masked": "AKIA…[MASKED]…WXYZ",
  "locations": [
    {"file": "config/prod.py", "line": 41},
    {"file": "services/db.py", "line": 12, "via": "goto_definition"}
  ],
  "evidence": [
    {"type": "tool_trace", "tool": "goto_definition", "args": "config/prod.py:41", "result": "→ services/db.py:12 (db_password)"},
    {"type": "code_slice", "file": "services/db.py", "lines": "10-14"},
    {"type": "model_reason", "summary": "生产配置模块引用,非测试路径,无 mock 标记"}
  ],
  "entropy": 4.1,
  "verified": null,
  "engine_version": "0.1.0",
  "rule_version": "2026.09",
  "model_id": "deepseek-v3-internal"
}
```

## 附录 C:输入字段映射摘要

| 归一化字段 | TruffleHog(JSONL) | Gitleaks(JSON) | Gitleaks(SARIF) |
|---|---|---|---|
| detector | `DetectorName` | `RuleID` | `ruleId` |
| file_path | `SourceMetadata.Data.File` | `File` | `physicalLocation…artifactLocation.uri` |
| line | `SourceMetadata.Data.Line` | `StartLine` | `region.startLine` |
| secret_raw | `Raw` | `Secret` | `message`(需解析) |
| verified | `Verified` | 无(null) | 无(null) |
| match_context | `SourceMetadata.Data.…`(可选) | `Match` | `message`/`contextRegion` |
