# Implementer 实现交接记录

## SQLite 五层记忆最小存储验证

依据 `docs/Planner/MINIMAL_FIVE_LAYER_MEMORY_STORAGE_PLAN.md` 的简化范围完成：

- 继续复用现有 SQLite `sessions/messages/tasks/tool_calls/approvals/task_commits`，未安装或接入 PostgreSQL、Neo4j 和向量数据库。
- `Database.initialize()` 新增 `preferences`、`memories` 两张表及来源、状态、重要性、索引状态和活动内容哈希约束。
- 新增 `PreferenceRepository`，支持 JSON 偏好读取、覆盖、删除、列表和损坏回退；系统来源不能覆盖用户来源，敏感键和值拒绝入库。
- 新增 `MemoryRepository`，支持长期记忆创建、读取、活动列表、来源查询、ID 回表、更新、禁用和 embedding 状态记录；活动内容按 SHA-256 去重，正文更新后索引状态回到 `pending`。
- `Runtime` 暴露 `preferences` 和 `memories` 仓储，暂不改变现有 CLI JSON 偏好行为，避免影响审查模式和活动 Session 兼容逻辑。
- `task_steps`、真实向量检索和 Neo4j 图投影暂未实现，等待 SQLite 数据模型验证后再单独实施。

本轮验证：

```text
.\\.venv\\Scripts\\python.exe -m pytest tests\\unit\\test_memory_storage.py -q：9 passed
.\\.venv\\Scripts\\python.exe -m pytest -q -rs：195 passed，2 skipped
.\\.venv\\Scripts\\ruff.exe check .：All checks passed!
.\\.venv\\Scripts\\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

## ContextBuilder 与记忆 CLI

- 新增 `memory/context_builder.py`，固定组装顺序为当前活动分支、有效偏好、相似度达标的活动长期记忆和当前任务范围说明；上下文总量、历史消息数、偏好和记忆条数均有上限。
- `SessionService` 在运行时注入 ContextBuilder；手动构造 SessionService 的旧调用仍沿用原始历史上下文，保持测试和扩展调用方兼容。
- SQLite 阶段使用可替换的 `MemoryRetriever` 协议和本地词项相似度检索器，不宣称已经接入真实向量数据库；未来只替换检索适配器，正文继续从 SQLite 回表读取。
- CLI 新增 `memory add/list/show/update/disable`，长期记忆只能由用户显式命令写入，模型没有自动写记忆入口。

本轮验证：

```text
.\\.venv\\Scripts\\python.exe -m pytest -q -rs：199 passed，2 skipped
.\\.venv\\Scripts\\ruff.exe check .：All checks passed!
.\\.venv\\Scripts\\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

## 最大模型轮次调整

- 将运行配置、`.env` 模板和 `AgentLoop` 直接构造时的默认最大模型轮次统一从 20 调整为 50。
- 增加配置默认值回归测试，确保环境变量缺省和直接构造两条入口保持一致。

## Session 树形会话与 Git Commit 关联实施记录

- 存储落点：`Database` 新增 `sessions`、自引用 `messages` 和唯一 `task_commits` 表；消息父节点、活动叶子和 Task 外键由 SQLite 与仓储层共同校验。删除 Session 使用级联事务删除消息，但保留 Task、工具、审批、Commit 和审计记录。
- 兼容策略：启动时识别缺少树字段的旧线性 `messages` 表，暂存后按原 `rowid` 和 Session 顺序重建父链；当前基线没有既有 Session 表，因此新安装直接创建空树。
- 编排落点：`memory/session.py` 负责当前活动路径、分支切换、user/assistant 可见消息顺序、失败时保留 user 消息、首个成功问答标题生成和 Task 关联；`AgentLoop` 只接收已准备好的可见历史，不选择 Session。
- CLI 落点：普通请求沿用本机活动 Session，新增 `session new/list/history/branches/continue-from/commit/switch/delete`；删除要求 `--confirm` 或精确输入 `DELETE_SESSION`。
- Git 边界：`git.py` 仅执行 `git rev-parse --verify HEAD^{commit}` 和 `git status --porcelain=v1 --untracked-files=all`；工作区有未提交变更、Git 不可读或关联保存失败时返回“未记录版本”，不执行 add、commit、stash、checkout、reset、push 等写操作。
- 标题调用失败、超时或空响应只保留默认标题，不改变已成功 Task、消息或分支；标题输入只包含首轮可见 user/assistant 内容。
- 本次相对计划的实现偏差：采用 `SessionService`、`SessionRepository` 和 `CommitRepository` 作为职责落点，命令名按计划示例实现；未引入通用版本控制抽象、后台队列或自动工程恢复。
- 验证结果：Session 专项测试 8 项通过；全量 `pytest` 为 174 passed、2 skipped，`ruff check .` 和 `python -m compileall src` 通过。

## 已实现范围

- 建立 Python 3.12 工程骨架和 `pyproject.toml`。
- 实现 `read`、`write`、`edit`、`bash` 四个工具。
- 实现工作区路径限制、符号链接逃逸检查、严格 Bash 命令策略和人工审批接口。
- 实现统一 `ToolExecutor`，保证工具调用先校验、安全检查、审批，再执行并写入脱敏审计。
- 实现 SQLite 任务、工具调用、审批记录和启动时 `running` 任务恢复。
- 实现可替换 `ModelBackend`、Fake Backend、OpenAI 兼容后端和最小 Agent Loop。
- 实现本地 CLI 入口及 Ctrl+C 取消处理。
- 增加运行时组装层，保持 CLI 和 Agent Loop 不直接耦合具体模型或 SQLite 实现。
- 配置入口自动读取当前工作目录 `.env`，并让进程环境变量覆盖文件配置。
- 补充安全、工具、存储、Agent Loop、模型协议和 CLI 集成测试。

## 实现取舍

- 真实模型后端使用 Python 标准库 `urllib` 调用 OpenAI Chat Completions 兼容接口，避免把供应商 SDK 类型带入编排层，也不增加运行时依赖。
- 审计只保存工具参数和结构化结果摘要，不保存 read 正文、edit diff 或 Bash 完整输出。
- Bash 使用允许列表和进程终止逻辑降低风险，但仍不等价于 Docker、Windows Sandbox 等 OS 级隔离。

## 验证记录

已完成 Python 3.12.10 和项目虚拟环境配置：

```text
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

已执行并通过：

```text
.\.venv\Scripts\python.exe -m pytest：39 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall src
```

其中 1 个跳过项是当前 Windows 权限不允许创建符号链接；其余 39 项测试全部通过。

## Review 修复记录

依据 `docs/Review/REVIEW.md` 的 `CHANGES_REQUIRED` 结论完成以下修复：

- Bash 命令只使用策略解析后的 argv，禁用 profile/rc、Shell 展开和敏感环境变量，并以并发有界流读取 stdout/stderr；补充变量、brace、glob、超时和运行中取消测试。
- 文件工具默认拒绝 `.env*`、私钥和凭据文件；任务请求、工具参数、审批记录和工具结果改为长度、路径、动作和 sha256 摘要，避免正文进入 SQLite。
- 工具审计改用内部 `audit_id`，并将供应商 `tool_call_id` 限制在任务内唯一；审计异常会补写失败终态并交给 Agent Loop 终结任务。
- `read` 增加“行号 + 行内字节偏移”游标，支持超长 ASCII 和多字节 UTF-8 行继续读取。
- write/edit 审批增加受限预览或 diff、内容摘要、目标状态摘要和审批指纹；执行前检测动作或目标状态漂移。
- 删除未接线配置开关，明确 `.env` 使用当前工作目录语义；模型阻塞 HTTP 的取消行为明确为 best-effort，并补充运行中取消验证。

本轮修改只涉及实现、测试、配置说明和实现交接文档；未读取或提交真实 `.env` 内容。

## 本轮复审前验证

```text
.\.venv\Scripts\python.exe -m pytest -q：55 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
git check-ignore -v --no-index .env：命中 .gitignore:1:.env，文件未被 Git 跟踪
```

## 第二轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第二轮 `CHANGES_REQUIRED` 结论，继续完成：

- 新增公开的 `WorkspaceAccessPolicy`，由文件工具和 Bash 的显式路径参数共享 `.env*`、凭据、私钥和敏感目录拒绝规则。
- Bash 审批界面仍展示脱敏后的完整命令，但持久化审批摘要只保存可执行文件、参数数量、超时、命令摘要哈希和审批指纹，不保存原始 argv。
- Windows Bash 自动发现优先 Git for Windows；显式或自动发现 WSL `bash.exe` 会被拒绝，运行时启动阶段会探测受控 Bash 和 Git 能力。
- read 对总正文预算、UTF-8 字节边界和非法游标进行校验；截断游标通过安全 metadata 传入模型。
- Bash 结果在单次预算内保留状态前缀和截断标记；ToolExecutor 将安全的截断、退出码和续读游标状态附加到模型工具消息。
- 增加跨工具敏感路径、Bash 审批哨兵、WSL 运行时、结果预算和非法游标回归测试。

## 第二轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：70 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
```

其中 1 个跳过项仍是当前 Windows 权限不允许创建符号链接。

## 第三轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第三轮 `CHANGES_REQUIRED` 结论，继续完成：

- 工作区敏感判断改为只检查工作区相对路径，显式 Bash 路径复用真实解析器并拒绝符号链接和 Windows 目录连接。
- `rg` 自动注入 `--no-follow` 与不可移除的敏感文件/目录排除规则，覆盖递归根目录和 `--files`；裸 `git diff` 及可能返回正文的变体不再允许。
- `MAX_READ_BYTES` 至少为 4，保证 UTF-8 截断游标可推进；`ToolExecutor` 使用 `MAX_OUTPUT_BYTES` 统一限制最终模型工具消息，并优先保留游标、退出码和截断状态。
- 增加递归敏感文件、显式符号链接、工作区祖先目录、最小 read 预算和最终消息总预算回归测试。

## 第三轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：77 passed，2 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
```

2 个跳过项均与当前 Windows 环境不允许创建符号链接有关；真实模型 HTTP 取消仍按 Review 记录为本地 MVP 的 best-effort 限制。

## 第四轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第四轮 `CHANGES_REQUIRED` 结论，继续完成：

- `WorkspaceAccessPolicy` 同时生成文件工具和 `rg` 使用的排除 glob，并启用 `--glob-case-insensitive`，覆盖大小写变体的敏感文件、后缀和目录。
- 从模型可调用的 Git 允许列表中移除 `git diff --check`，避免尾随空格违规行正文回填模型。
- `MAX_OUTPUT_BYTES` 至少为 132；未知工具、参数/审批/执行异常和正常工具结果统一经过消息预算格式化出口，并保留错误类型、截断和游标状态。
- 增加大小写敏感资源、`git diff --check` 哨兵和未知工具消息预算回归测试。

## 第四轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：81 passed，2 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
```

2 个跳过项均与当前 Windows 环境不允许创建符号链接有关；真实模型 HTTP 取消仍按 Review 记录为本地 MVP 的 best-effort 限制。

## 第五轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第五轮 `CHANGES_REQUIRED` 结论，完成：

- 为 read 状态信封定义 128 字节预留量；`ToolRegistry` 将 `min(MAX_READ_BYTES, MAX_OUTPUT_BYTES - 128)` 作为 ReadFileTool 的最终正文预算，避免 ToolExecutor 二次截断后继续使用旧游标。
- read 回填模型时只发送 `next_cursor` 和 `truncated` 状态，`ToolResult.metadata` 与 SQLite 审计继续使用同一份工具输出元数据；状态降级改为可解析的 JSON，不再返回无语义的 `!`。
- `MAX_OUTPUT_BYTES` 最低值提高到 132，并同步 README、`.env.example` 和测试说明。
- 增加 Fake Backend 驱动的 Agent Loop 连续分页测试，覆盖恰好达到预算、超过预算、长行、多行、3 字节中文和 4 字节表情符号，验证模型可见内容逐字节重组且无缺口。

## 第五轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：85 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过；仅有 Windows LF/CRLF 转换提示
```

2 个跳过项仍与当前 Windows 环境不允许创建符号链接有关；真实模型 HTTP 取消仍按 Review 记录为本地 MVP 的 best-effort 限制。

## 下一阶段过程可观测、审查模式与工具扩展实施记录

依据 `docs/Planner/NEXT_PHASE_OBSERVABILITY_REVIEW_MODES_TOOL_EXTENSIBILITY_PLAN.md`，本轮在保留原有执行主干的前提下完成：

- 新增 `strict`、`relaxed`、`full-access` 三种固定任务审查模式，CLI 通过 `--review-mode` 选择，默认严格模式；完全访问要求输入 `FULL-ACCESS` 强确认，确认前不创建任务、不调用模型。
- 新增结构化 `RuntimeEvent` 和事件接收器。CLI 默认展示任务、模型轮次、工具、安全检查、审批和结果过程，`--no-progress` 关闭展示；事件接收器故障被隔离，不改变任务结果。
- Tool 基类提供保守的默认展示、参数审计、模型 metadata 和结果摘要；Registry 按显式工具集合动态返回规格并检查重复名称，Agent Loop、ToolExecutor 不再按内置工具名称分支。
- strict 保持原有安全 argv；relaxed 使用每次人工审批的原始 Shell 脚本；full-access 使用任务级确认后的原始 Shell，并保留超时、取消、输出限制、环境清理和审计。
- 任务表新增 `review_mode`，审批表新增 `decision_source`；启动时为旧数据库增量补列，旧任务默认为 `strict`、旧审批来源为 `legacy`，不覆盖已有记录。
- full-access 读取敏感文件时对模型可见内容和过程结果做脱敏；写入、修改和 Bash 的展示/审计只保存安全摘要，不持久化正文、diff 或完整输出。
- 统一所有任务终态过程事件和 CLI 结果输出的总轮数；增加 relaxed 审批拒绝后不执行原始 Shell 的回归测试。
- 工具终态事件明确区分成功、失败、拒绝和取消，并为各终态补充耗时；扩展环境变量和输出脱敏规则，覆盖 `ACCESS_KEY`、`CLIENT_SECRET` 及 PEM 私钥块。
- AgentLoop 在直接组装场景下默认继承 Executor 的审查模式、审批器和事件接收器，避免任务记录与实际执行策略分裂。

本轮重要偏差：计划没有锁定文件名和具体类名，实际使用 `safety/review_mode.py`、`orchestrator/events.py` 和 Tool 基类完成契约收敛；未接入远程渠道，也未把 full-access 暴露给非 CLI 入口。

本轮验证结果：

```text
.\.venv\Scripts\python.exe -m pytest -q：103 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
git check-ignore -v --no-index .env：命中 .gitignore:1:.env
```

2 个跳过项仍与当前 Windows 环境的符号链接权限有关；下一阶段新增权限模式、原始 Shell 和数据库迁移已通过专项测试，待 Reviewer 对新权限模式和原始 Shell 风险复审。

## 最新 Review 修复记录

依据最新 `docs/Review/REVIEW.md` 的 `CHANGES_REQUIRED` 结论，完成以下修复：

- Registry 只接收显式工具集合；内置工具组装移到 `executor/tools/__init__.py` 注册点，并增加工具名称与 `ToolSpec.name` 一致性校验。
- Registry、ToolExecutor、AgentLoop 对审查模式执行一致性校验；任何 strict、relaxed、full-access 失配均在任务创建或模型调用前拒绝，避免权限策略和审计模式分裂。
- Tool 基类增加模型状态字段优先级契约，ToolExecutor 只做通用预算压缩，不再维护内置 metadata 白名单；新增工具的自定义必要状态可在最小预算下保留。
- Bash 截断后的 stdout/stderr 继续走保守脱敏路径；未闭合 PEM 私钥块、错误/超时/取消结果及 AgentLoop 最终模型回答均不会直接暴露敏感内容。
- full-access 工具规格改为模式中性的路径说明，并补充 `..` 外部路径、外部已有文件覆盖、CLI 强确认拒绝、生产注册点扩展、full-access Bash 输出/环境/超时/取消边界测试。
- 内置工具绑定实际审查模式，Registry 拒绝省略或错误传入的模式；Bash 结束路径显式释放 Windows Proactor 子进程 transport，避免取消后的管道回收警告。

## 最新 Review 修复验证

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：114 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：通过
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

当前仍未读取或提交真实 `.env` 内容；符号链接测试是否可执行取决于 Windows 权限。

## 最新复审修复记录

依据最新 `docs/Review/REVIEW.md` 的 `CHANGES_REQUIRED` 结论，完成以下安全修复：

- `AuditRepository` 成为不可信审计字段的最后安全边界：工具名、工具调用 ID、审批动作使用安全标识符；不安全值只保存固定标签和稳定哈希；参数摘要、审批摘要、结果摘要、错误类型和错误消息统一脱敏。
- `ToolExecutor` 对未知工具的过程标签和参数摘要使用安全出口，避免模型提供的工具名和参数键进入展示或 SQLite；保留未知工具错误的可诊断语义。
- CLI 的 `ConfigError`、`ModelBackendError`、未知异常、取消提示和最终模型结果统一调用 `redact_text()`，启动错误保留异常类型但不回显凭据内容。
- 模型工具状态 metadata 增加通用防御性脱敏，扩展工具误返回 `api_token`、Bearer 或 `token=...` 时不会直接进入模型消息。
- 新增已知/未知工具不可信字段审计扫描、三类 CLI 启动异常脱敏、`--no-progress` 实际运行、真实 `FULL-ACCESS` 精确匹配和扩展 metadata 脱敏测试。

## 最新复审修复验证

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：122 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
git check-ignore -v --no-index .env：命中 .gitignore:1:.env
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接测试；进程树专项门禁仍建议在 Linux CI 或启用 Developer Mode 的 Windows job 中执行。当前实现等待 Reviewer 对最新 P1 修复复审。

## 最新版 Review 修复记录

依据最新版 `docs/Review/REVIEW.md` 的 P1/P2 问题清单，继续完成以下修复：

- 不可信工具名、工具调用 ID 和未知参数字段默认只保存固定标签与稳定哈希；已注册工具名仅在执行器确认其为规范名称后保留可读值。
- 配置整数解析错误不再回显原始环境变量值；补充包含 `sk-proj-...` 形态凭据的 Settings 和真实 CLI 入口测试。
- 脱敏规则增加无标签的常见 OpenAI key、GitHub token 和 JWT 形态识别，扩展工具 metadata 进入模型消息前也统一处理。
- 审计列表改按 SQLite 插入顺序返回，避免调用 ID 哈希后同一秒内的分页调用被哈希字典序重排。

## 最新版 Review 修复验证

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：126 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接测试；进程树、符号链接和 EOF 专项门禁仍建议在 Linux CI 或启用 Developer Mode 的 Windows job 中执行。当前代码等待 Reviewer 对本轮 P1 修复复审，Review 文件中的结论尚未变更。

## Review-3 P1 修复记录

依据最新版 `docs/Review/REVIEW.md` 的 `P1-1`，修复审计故障异常路径重新拼接模型原始工具字段的问题：

- `ToolExecutor` 暴露统一的安全工具标签出口；已注册工具使用 Registry 规范名，未知工具只使用固定标签与稳定哈希。
- 审批审计写入失败的 `AuditError` 对调用 ID 使用与 SQLite 审计相同的 `call:<hash>` 表示，不再拼接模型原文。
- `AgentLoop` 捕获工具异常时使用安全工具标签，任务错误、`task_finished` 事件和 SQLite `tasks.error_message` 均不回流原始工具名或调用 ID。
- 新增启动审计失败、审批审计失败两条故障注入测试，扫描 AgentResult、任务记录、工具/审批审计和结构化事件。

## Review-3 P1 修复验证

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_next_phase.py tests/unit/test_file_tools.py tests/unit/test_storage_and_loop.py -q：66 passed
.\.venv\Scripts\python.exe -m pytest -q -rs：128 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接测试；进程树、符号链接和 EOF 仍属于 Review 标注的非阻断 P2 门禁。当前实现等待 Reviewer 对 Review-3 P1 修复复审。

## CLI 易用性、目录与持久化模式优化实施记录

依据 `docs/Planner/USABILITY_PERSISTENT_ACCESS_PROGRESS_STORAGE_PLAN.md` 完成：

- 新增 `data/preferences.json` 本地偏好存储，使用原子替换保存默认审查模式；显式 `strict`、`relaxed`、`full-access` 会更新默认模式。
- 首次 `full-access` 强确认成功后保存偏好；后续沿用本地偏好时跳过重复确认，并在审批审计中使用 `decision_source=preference` 区分人工首次确认。
- 偏好文件损坏、未知模式、读取失败时安全降级到 `strict`；偏好保存失败时不创建任务、不调用模型。
- CLI 默认只展示任务开始、工具开始和工具终态，完整结构化事件仍由运行时产生；`--no-progress` 保持关闭全部过程行。
- 运行时自动创建项目根 `data/` 和工作区 `script/`，系统提示与 Bash 工具说明均声明 `script/` 为默认脚本目录，Bash 当前目录仍为工作区根。
- 相对 `DATABASE_PATH` 改为相对项目根解析；默认数据库为项目根 `data/likai_nexus.db`，显式路径位于工作区内部时拒绝启动。
- 默认数据库首次启动时识别并迁移 `workspace/data/likai_nexus.db`、`workspace/.likai_nexus/tasks.db`；迁移前后校验 SQLite，旧库和 sidecar 移入 `data/legacy-backup-*`，冲突时选择固定顺序的权威库并保留另一份备份。
- strict/relaxed 模式下将项目根 `data/` 作为受保护目录，文件工具和递归 `rg` 均不能读取；full-access 保留既有能力边界。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_preferences_and_app_data.py tests/integration/test_cli_agent.py tests/unit/test_config_and_safety.py -q -rs：60 passed，2 skipped
.\.venv\Scripts\python.exe -m pytest tests/unit/test_next_phase.py tests/unit/test_storage_and_loop.py tests/unit/test_file_tools.py tests/unit/test_bash_and_backend.py -q -rs：83 passed
.\.venv\Scripts\ruff.exe check src tests：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
```

剩余限制：真实模型 HTTP 取消仍是既有的 best-effort 行为；Windows 当前权限不足时符号链接专项测试仍会跳过。Reviewer 需要重点复核 full-access 首次确认/偏好沿用、旧数据库迁移不丢失、默认 CLI 事件白名单和 `data/` 工作区隔离。

## Bash 指令、结果与模型轮次可见性实施记录

依据 `docs/Planner/BASH_COMMAND_RESULT_VISIBILITY_PLAN.md` 完成：

- 在现有 `Tool` 契约上增加安全的结果展示投影；默认工具不返回结果正文，Bash 单独提供 stdout、stderr、退出码和截断状态。
- `ToolExecutor` 通过通用展示契约写入 `RuntimeEvent.metadata`，不按工具名称分支；审计仍只保存参数摘要、状态和结果摘要，不保存原始 command 或完整输出。
- Bash 开始事件显示实际 command；默认 timeout 不重复展示，非默认 timeout 可见；超长指令使用独立预算并显示 `指令已截断`。
- Bash 终态区分成功、失败、超时和取消；CLI 显示耗时、退出码、截断状态以及独立的 stdout/stderr 预览。没有有效退出码时显示“不可用”，不伪造为 0。
- 模型调用开始/失败事件携带结构化 `turn_number`、`max_turns` 和状态；CLI 只显示开始轮次和失败轮次，不显示正常 `model_finished` 或内部安全事件。
- 界面文本按“终端控制字符清理 → 凭据脱敏 → UTF-8 字节截断”的顺序处理；保留换行和制表，阻止 ANSI、回车覆盖和退格影响终端。
- `--no-progress` 继续关闭全部过程事件；事件接收器异常仍由既有 `emit_safely` 隔离。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：148 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
```

2 个跳过项仍是当前 Windows 权限不足时的符号链接专项测试；未修改 `docs/Review/REVIEW.md`。

## Review-4 两个 P1 修复记录

依据最新版 `docs/Review/REVIEW.md` 的 `P1-1` 和 `P1-2` 完成修复：

- 工具结果展示投影的调用、结构清理、终端清理、脱敏和截断统一置于旁路保护中；展示异常统一降级为空展示，不再影响已经成功的 `ToolResult`、任务状态或工具审计，也不会在通用异常路径中重复调用失败的展示投影。
- 终端展示值增加循环引用、过深嵌套和不支持对象的安全占位；模型失败原因在 CLI 短文本出口统一折叠为有界单行，避免 CR/LF/CRLF 注入伪造的 `[工具]`、`[模型]` 或 `[任务]` 顶层进程行。
- 新增直接展示异常、循环展示结构、异常字符串对象、超深结构以及 CR/LF/CRLF 假前缀的回归测试。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：152 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

## Review P1 修复记录

依据 `docs/Review/REVIEW.md` 的 `P1-1` 和 `P1-2` 完成修复：

- `ToolOutput.effective_status()` 作为审计摘要、模型结果、运行事件和 SQLite 终态的唯一状态来源；默认工具、投影降级和四个内置工具的摘要均不再读取可能过时的 `is_error` 兼容字段。
- 工具状态增加统一中文标签；契约测试补齐显式 `TIMEOUT`、`CANCELLED` 和异常路径，并断言 `ToolResult`、`RuntimeEvent`、审计摘要和 SQLite 状态一致。
- 将原综合测试拆为 `test_executor_and_projections.py`、`test_review_modes_and_migrations.py` 和共享 `test_support.py`；`test_tool_contract.py` 继续作为可复用公共契约矩阵。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：164 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接专项测试；未修改 `docs/Review/REVIEW.md`，等待 Reviewer 复审本轮 P1 修复。

## Review-5 两个 P1 修复记录

依据最新版 `docs/Review/REVIEW.md` 的本轮 `P1-1` 和 `P1-2` 完成修复：

- `_display_arguments()` 将工具调用展示、值转换、终端清理、脱敏和字节截断统一置于一个异常隔离边界；任何第三方展示异常都降级为固定的 `[指令展示不可用]`，不阻止工具执行，也不改变成功结果和审计终态。
- `CliApprovalHandler` 在构造 `input()` 提示前，对动作类型、审批摘要和确认令牌分别进行终端控制字符清理、凭据脱敏、单行化和字节限制；审批指纹、确认比较和实际执行参数继续使用原始值，展示清理不改变审批绑定语义。
- 新增调用展示直接抛错/异常字符串对象测试，以及审批提示的 ANSI、CR/LF/CRLF、凭据和超长输入测试。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_next_phase.py tests/integration/test_cli_agent.py tests/unit/test_bash_and_backend.py -q -rs：74 passed
.\.venv\Scripts\ruff.exe check src tests：All checks passed!
.\.venv\Scripts\python.exe -m pytest -q -rs：156 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

未修改 `docs/Review/REVIEW.md`，等待 Reviewer 复审本轮 P1 修复。

## Review-6 一个 P1 修复记录

依据最新版 `docs/Review/REVIEW.md` 的 `P1-1` 完成修复：

- 审批摘要的安全清理函数现在保留截断状态；当摘要超过 4096 字节时，在预算内追加固定的“审批摘要已截断”标记，不再让用户误以为看到的是完整命令。
- 审批指纹、原始命令、确认 token 比较和实际执行参数均未改变；`--no-progress` 下也会直接在审批提示中显示截断告知。
- 长摘要回归测试已断言标记存在，同时继续验证终端控制字符和凭据不会泄露。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：156 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接专项测试；未修改 `docs/Review/REVIEW.md`，等待 Reviewer 复审本轮 P1 修复。

## Session/Git Review P1 修复记录

依据 `docs/Review/REVIEW.md` 当前 `CHANGES_REQUIRED` 结论，修复本轮 Session 树与 Git 关联问题：

- Git 只读查询同时使用 `--no-optional-locks` 和 `GIT_OPTIONAL_LOCKS=0`，避免 `git status` 刷新 index stat 缓存；测试保存 index 字节、时间、工作树内容和 refs 前后快照。
- Commit 关联改为显式资格判定：必须有审计记录的成功 `write`/`edit`、任务前后可比较且不同的完整 HEAD、任务结束时干净工作区；普通对话、非代码任务、未提交结果、相同 HEAD、Git 读取失败和关联保存失败均保留安全原因并返回未记录版本。
- user 消息先写入待执行状态，任务返回后回填稳定 Task ID 和 success/failed/cancelled 终态；失败或取消不创建伪 assistant，重试会保存来源消息 ID，历史展示显式状态与重试关系。
- `continue-from` 必须携带调用方当前 Session，并在跨 Session 时先拒绝再更新活动叶子；CLI 使用本机活动 Session，不再因历史消息反向切换活动偏好。
- Commit 查询和历史展示增加 Task ID、完整 SHA 与“仅覆盖已提交内容”的强制边界提示；未记录版本保存可诊断原因但不保存原始请求敏感内容。
- `sessions.last_message_at` 与 `updated_at` 分离，标题修改和分支指针切换不会冒充最近消息时间；旧数据库启动时增量补列并保留旧消息链。

本轮保留用户先前明确要求的 `MAX_TURNS=50`，未按 Review 中与该请求冲突的建议恢复为 20。

## Session/Git Review P1 二次修复记录

依据最新 `docs/Review/REVIEW.md` 的两个阻断问题完成：

- Session 在创建可见 user Message 前通过 `TaskStateStore.get()` 预检重复 `task_id`；重复请求只保存无 Task 的 `rejected` 消息并抛出“本次请求未创建新 Task”，不再把新失败消息回填到旧的 success/failed/cancelled Task。AgentLoop 边界仍保留专门的重复异常处理作为兜底。
- 版本附加链路统一置于旁路保护：Git 基线读取、成功代码修改资格查询、结束快照读取、CommitRepository 保存以及 assistant 版本原因写入分别失败时，只返回安全的“未记录版本”原因；assistant 和成功 Task 已保存后不会因版本能力故障向调用方抛异常。
- 新增成功、失败、取消三种旧 Task 状态下的重复 ID 回归测试，补充基线读取、审计资格、Commit 保存和版本原因落库四类故障注入测试。
- 新增 CLI `history`、`continue-from`、`commit`、`switch` 集成测试，覆盖 Task/SHA 展示、边界提示、跨 Session 拒绝和活动偏好不变。

## 终端 stdout/stderr 预览上限调整

依据用户要求，将终端结果预览上限从 4 KiB 调整为 1 KiB（1024 个 UTF-8 字节）：

- 仅影响 CLI 终端中 stdout/stderr 的展示预览；指令预览、Bash 实际采集上限和回填模型的 `MAX_OUTPUT_BYTES` 保持不变。
- 超过上限时继续显示 `[输出预览已截断]`，避免用户误以为终端展示了完整输出。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_bash_and_backend.py tests/integration/test_cli_agent.py -q -rs：36 passed
.\.venv\Scripts\ruff.exe check src tests：All checks passed!
.\.venv\Scripts\python.exe -m pytest -q -rs：156 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```

2 个跳过项仍是当前 Windows 权限不足导致的符号链接专项测试；未修改 `docs/Review/REVIEW.md`。

## 架构优化讨论方案执行记录

依据 `docs/Planner/ARCHITECTURE_OPTIMIZATION_DISCUSSION.md` 中已确认的两组方案，并按用户明确指示执行：

- 工具公共契约迁移到 `likai_nexus.tools`；新增 `ToolExecutionContext`，由内置工具注册点统一创建并传递路径、命令策略和审查模式。
- `ToolResult` 现在明确拆分执行终态、模型投影、界面投影和审计投影；CLI 只消费通用字段协议，不再按 Bash 或其他具体工具名称渲染。
- `ToolExecutor` 继续作为唯一执行门面，但将模型/界面/审计摘要职责分别委托给 `executor/projection.py` 与 `executor/audit.py`；执行顺序仍固定为查找、校验、安全、审批、执行、投影、终态审计。
- 运行事件契约提升到 `likai_nexus.events`；`ToolSpec`、`ToolCall` 和结构化工具结果归工具契约模块，任务与模型消息继续由各自模块持有。
- CLI 入口与控制台渲染器拆分；保留 `--no-progress`、审批提示和退出码行为。`Settings` 保持纯配置，应用数据准备由 `runtime.prepare_runtime()` 显式完成。
- 新增可复用的 `tests/unit/test_tool_contract.py`，并将原阶段综合测试拆为执行器/投影、审查模式/迁移和共享支持文件，覆盖稳定职责边界。

实现偏差与兼容说明：

- 为保持现有扩展调用方稳定，`ToolExecutor` 保留旧的静态投影辅助入口，内部实现已委托到新投影服务；`ToolResult` 也保留 `content`、`metadata`、`is_error` 和 `display_metadata` 只读兼容属性。
- 讨论方案中的 E/F/G 候选项没有达到触发条件，本轮未实现；未修改 Planner 文档，也未修改 Reviewer 产物。

本轮验证：

```text
.\.venv\Scripts\python.exe -m pytest -q -rs：164 passed，2 skipped
.\.venv\Scripts\ruff.exe check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过（仅有 Windows LF/CRLF 转换提示）
```
