# 第二轮代码复审报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-17
- 对应计划：`docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- 上一轮报告：[`docs/Review/REVIEW_ROUND_1.md`](./REVIEW_ROUND_1.md)
- 修复交接：`docs/Implement/IMPLEMENTATION_NOTES.md`
- 修复基线：`09f539d`（Implement minimal four-tool local agent）
- 本轮审查提交：`eede675`（Harden execution safety and audit flow）
- 对应分支：`agent/publish-mvp`
- 用例附件：[`REVIEW_USE_CASE_ROUND_2.svg`](./REVIEW_USE_CASE_ROUND_2.svg) / [`REVIEW_USE_CASE_ROUND_2.png`](./REVIEW_USE_CASE_ROUND_2.png)
- Reviewer 边界：仅在 `docs/Review/` 内归档第一轮报告并新增本报告和第二轮用例图，未修改业务代码、测试、计划或实现交接文件

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

上一轮 6 个 P1 中，Shell 二次展开、审计异常导致任务悬空、超长行游标不前进、审批信息不足和 Bash 无界缓冲等 5 项已形成有效修复；“敏感信息最小化”仅部分关闭。全量测试、Ruff、编译和 diff 检查均通过，但复审确认仍有 3 个 P1：Bash 与文件工具没有共享敏感资源策略、Bash 审批审计仍持久化原始 argv、默认 Bash 自动发现会在当前 Windows 环境选中 WSL `bash.exe` 并导致允许命令失败。

因此，当前代码可继续沿用现有架构修复，不需要推倒重来；在上述 P1 关闭前，不建议接入真实敏感工作区或远程消息渠道。

## 上一轮问题关闭情况

| 上一轮问题 | 本轮状态 | 复审判断 |
|---|---|---|
| P1-1 Shell 二次展开绕过 | 已关闭 | 命令先固化为 argv，再经 `shlex.join()` 安全引用；`$`、glob、brace 等语法已有拒绝测试 |
| P1-2 敏感内容进入模型或 SQLite | 部分关闭 | task、write、edit 和工具结果审计已改为摘要；Bash 敏感路径与审批 argv 仍有缺口 |
| P1-3 审计异常使任务停在 running | 已关闭 | 内部 `audit_id`、task 内调用 ID 唯一约束、`AuditError` 终止路径和回归测试已补齐 |
| P1-4 超长单行游标不前进 | 主问题已关闭 | 新增行内字节游标并覆盖 ASCII/UTF-8；返回预算与非法游标仍有 P2 问题 |
| P1-5 write/edit 审批信息不足 | 已关闭 | 预览或 diff、内容摘要、目标摘要、审批指纹与漂移校验均已实现 |
| P1-6 Bash 输出先完整缓冲 | 已关闭 | stdout/stderr 已改为有界并发读取，超限后继续排空，不再无界保留 |
| P2-1 真实模型请求取消 | 未关闭、已明确限制 | 仍是 `asyncio.to_thread(urllib)` 的 best-effort 取消 |
| P2-2 无效配置和 `.env` 文档差异 | 已关闭 | 未生效开关已删除，当前目录 `.env` 语义已同步 |
| P2-3 测试矩阵缺口 | 部分关闭 | 已从 39/1 增至 55/1，但本轮复现路径尚无回归测试 |

## 做得较好的部分

- 主依赖仍保持 `CLI → AgentLoop → ToolExecutor → Safety/Tool → Storage`，没有把执行逻辑塞回 Agent Loop。
- `ToolExecutor` 对参数、审批、执行和审计异常建立了统一终结路径，供应商调用 ID 也不再要求跨任务全局唯一。
- write/edit 的展示摘要与持久化摘要已经分离，审批绑定目标状态和内容指纹，能够拒绝审批后的目标漂移。
- `read` 的“行号 + 行内字节偏移”解决了上一轮超长单行死循环。
- Bash 使用固定 argv、禁用 profile/rc、清理影响执行的环境变量并采用有界流读取，安全性和稳定性均明显提升。
- 代码仍保持 MVP 规模，没有提前引入 Webhook、队列、ORM、多智能体或通用策略引擎。

## 严重级别

- P0：阻塞发布，可能造成严重数据、权限或安全问题。
- P1：重要安全或核心功能错误，必须修复后才能 PASS。
- P2：一般功能边界、维护性问题或测试缺口。

## 问题列表

### P0

暂无。

### P1

#### P1-1：Bash 没有复用敏感资源策略，可读取文件工具明确拒绝的路径

- 证据：`WorkspacePathResolver` 在 `src/likai_nexus/safety/paths.py:76-94` 拒绝 `.env*`、凭据文件和私钥；Bash 的 `CommandPolicy` 在 `src/likai_nexus/safety/command_policy.py:115-133`、`178-186` 只检查命令形态、绝对路径和 `..`，不检查敏感文件名，也没有注入工作区资源策略。
- 最小复现：`CommandPolicy().evaluate("rg SENTINEL credentials.json").allowed`、`CommandPolicy().evaluate("rg SENTINEL .env").allowed` 和 `CommandPolicy().evaluate("ls credentials.json").allowed` 均为 `True`。
- 影响：模型不能通过 `read` 读取凭据，但能改用 `bash` 的 `rg`、`ls` 或可执行项目代码的命令触达同一资源，安全能力取决于选择了哪个工具。Bash 输出会作为工具消息回填模型，因此这不是单纯的命令行显示问题。
- 必须修改：抽出一个公开、可复用的工作区资源策略，文件工具和 Bash 的显式路径参数都必须调用同一策略。对于 `pytest` 等能执行项目代码的命令，应明确它们无法靠字符串策略获得强隔离；接入远程渠道前使用低权限账户、容器或沙箱，并禁止挂载真实凭据。
- 必须测试：Bash 显式访问 `.env`、`credentials.json`、私钥后缀和敏感目录时被拒绝；哨兵内容不进入模型工具消息、CLI 输出或 SQLite。

#### P1-2：Bash 审批审计仍保存原始 argv，可持久化无标签敏感参数

- 证据：`src/likai_nexus/executor/tools/bash.py:82-84` 把 `argv={argv!r}` 写入 `ApprovalRequest.audit_summary`；`src/likai_nexus/executor/service.py:172-180` 只经过通用 `redact_text()` 后持久化该摘要。通用正则无法识别无标签搜索词、口令或业务敏感参数。
- 最小复现：审批命令 `rg UNLABELED_SECRET_SENTINEL README.md` 时，`request.audit_summary` 仍包含完整 `UNLABELED_SECRET_SENTINEL`。
- 影响：上一轮“工具参数和审批记录只保存最小摘要”的修复目标尚未完全兑现；本地 SQLite 仍可能长期保留用户不希望落盘的命令参数。
- 必须修改：审批界面继续显示经脱敏的完整命令，持久化审计只保存 executable、参数数量、超时、命令 SHA-256、审批指纹和经过资源策略确认的非敏感路径摘要，不保存原始 argv 或搜索模式。
- 必须测试：对 Bash 审批使用无标签哨兵，断言 `approvals.request_summary`、`tool_calls.arguments_redacted`、任务结果摘要和错误摘要均不包含哨兵。

#### P1-3：默认 Bash 自动发现会选中 WSL，导致核心允许命令在当前 Windows 环境失败

- 证据：`src/likai_nexus/executor/tools/bash.py:182-189` 在未配置 `BASH_PATH` 时直接使用 `shutil.which("bash")`，没有验证是否为 Git Bash；`tests/conftest.py:23-27` 使用同一发现方式。当前机器解析到 `C:\Users\likai\AppData\Local\Microsoft\WindowsApps\bash.exe`，它启动的是 WSL，而不是计划要求的 Git Bash。
- 最小复现：自动发现的 Bash 下，`pwd` 成功，但 `rg Nexus README.md` 和 `python -m compileall src` 均以 127 失败并报告命令不存在；显式配置 `C:\Program Files\Git\bin\bash.exe` 后，`pwd`、`rg`、`git status --short` 和 `python -m compileall src` 均成功。
- 测试误判：`tests/unit/test_bash_and_backend.py:73-89` 只断言大输出 `truncated=True`，没有断言该允许命令执行成功，因此 WSL 下 `rg` 失败仍能通过测试。
- 影响：默认配置与 `.env.example` 的“留空则从 PATH 查找”承诺不可靠，四个核心工具之一在支持的 Windows 环境中可能实际不可用；WSL 与 Windows Git 对路径、换行和工作树状态的解释也可能不一致。
- 必须修改：在 Windows 上优先要求显式 `BASH_PATH`，或实现仅发现 Git for Windows 的逻辑；初始化时验证运行时身份和最小命令能力，发现 WindowsApps/WSL 时给出明确配置错误。若计划支持 WSL，应建立独立运行时适配器和路径转换，不能与 Git Bash 共用当前实现。
- 必须测试：对 `pwd`、`rg`、`git`、`pytest`/`python` 的代表命令断言 `is_error=False` 和 `exit_code=0`；增加“PATH 只有 WSL bash.exe”时受控失败的测试。

### P2

#### P2-1：`read` 的返回预算和字节游标契约仍不完整

- `src/likai_nexus/executor/tools/read_file.py:90-93` 在文件内容达到字节上限后再追加继续读取提示，因此配置 `max_bytes=5` 时实测文件字节数为 5，但最终工具内容为 72 字节，不符合计划“最多返回 64 KiB”的总量语义。
- `src/likai_nexus/executor/tools/read_file.py:161-174` 在剩余预算小于首个 UTF-8 字符时会返回完整字符；`max_bytes=1` 读取中文字符时会返回 3 个文件字节。
- 调用方可以提交落在 UTF-8 字符中间的 `byte_offset`。对有效 UTF-8 文件使用该游标时，工具会误报“文件不是有效 UTF-8”，而不是拒绝非法游标或返回可继续的位置。
- 建议：明确限制的是“文件正文”还是“完整工具消息”；若按计划限制完整消息，应预留游标信封预算。优先把两个整数换成工具生成的不透明 cursor，或严格验证 `(offset, byte_offset)` 位于字符边界。

#### P2-2：Bash 截断标记会被第二次截断，模型不知道输出不完整

- `src/likai_nexus/executor/tools/bash.py:144-154` 先截断输出再追加标记，随后 `src/likai_nexus/executor/tools/bash.py:167-179` 又对带状态前缀的完整消息截断，末尾标记通常被删除。
- 使用显式 Git Bash 和 `max_output_bytes=64` 复现：`metadata.truncated=True`、最终内容 62 字节，但内容中没有“输出已截断”。
- `src/likai_nexus/orchestrator/agent_loop.py:134-140` 回填模型时只传 `result.content`，不传 `metadata`，所以模型无法从结构化元数据获知截断状态。
- 建议：统一定义有界工具结果信封，例如先保留状态、`truncated` 和 `next_cursor`，剩余预算再放正文；或在 ToolExecutor 中把安全 metadata 序列化进工具消息，避免每个工具手工拼接易丢失的提示。

#### P2-3：真实模型运行中取消仍为 best-effort

- `src/likai_nexus/models/openai_backend.py:42-44` 在 `asyncio.to_thread()` 前后检查取消信号，但无法中止正在执行的 `urllib` 请求线程。
- 当前新增测试使用会主动等待 `cancel_event` 的假 Backend，证明了 Agent Loop 协议能传播取消，但没有证明真实 HTTP 能及时释放连接和后台线程。
- 建议：本地 MVP 可以继续把该限制写入交接；接入远程长期任务前改用支持异步取消的 HTTP 客户端，并补充真实传输层的取消与超时测试。

#### P2-4：新增修复的测试仍缺少关键反例

- 缺少本轮 3 个 P1 的回归测试：跨工具敏感路径、Bash 审批哨兵、Git Bash/WSL 运行时识别。
- Bash 仍缺少审批拒绝、固定 cwd、敏感环境变量清理和各允许命令真实成功的断言。
- `read` 缺少完整响应预算、非法字节游标和极小预算下多字节字符测试；现有长行测试用 `startswith()`，没有约束响应总大小。
- 数据库新增了旧 `tool_calls` 表迁移逻辑，但没有旧库升级、数据保留和重复初始化测试。
- CLI 集成测试仍只覆盖参数解析和缺少配置，没有覆盖 Fake Backend 下的成功任务、审批拒绝、Ctrl+C 退出码与最终任务状态。

## 架构与功能优化建议

### 第一优先级：形成跨工具一致的安全边界

1. 将 `WorkspacePathResolver` 中的敏感资源判断提升为公开的 `WorkspaceAccessPolicy`（名称可按现有风格调整）。路径规范化、工作区边界和敏感资源拒绝只保留一份规则，由 read/write/edit 和 Bash 的路径型参数共同调用。
2. 明确两层保证：字符串和路径策略负责 MVP 允许列表；OS 级隔离负责阻止允许命令执行项目代码后读取凭据或联网。不要让审批或通用脱敏正则承担沙箱职责。
3. 延续当前 `ApprovalRequest.summary` 与 `audit_summary` 分离设计，但将持久化摘要改为类型化投影；所有工具都用无标签哨兵测试“不能落盘”。

### 第二优先级：把 Bash 运行时当作显式依赖

1. 启动阶段完成 Git Bash 路径发现、身份验证和最小能力自检，错误应直接说明找到的是 WSL、路径不存在，还是允许命令不可用。
2. 若继续保留 Bash，只支持一个清晰运行时；若未来同时支持 Git Bash 和 WSL，分别实现路径、PATH 和进程树终止适配器。
3. 更小、更安全的后续方案是让对外工具名称仍叫 `bash`，内部直接按绝对可执行路径启动已批准 argv；`pwd` 可由 Python 返回工作区路径，从而完全去掉 Shell 层。该调整需由 Planner 明确后再实施。

### 第三优先级：统一工具结果和审批动作契约

1. 引入轻量、不可变的 Prepared Action 数据：规范化参数、规范化路径、风险类型、展示摘要、审计投影、目标状态和指纹。当前多次调用 `approval_request()` 的逻辑可逐步收敛到这一对象，减少重复读取和阶段漂移。
2. 为模型可见的工具结果定义有界信封，固定保留成功/失败、是否截断、下一游标和正文长度；正文只能使用扣除信封后的预算。
3. 给 write/edit 参数增加合理字节上限；未来远程并发前，把任务状态更新改为带当前状态条件的单条 SQL，避免读取后更新的竞态。

### 暂不建议增加的复杂度

- 当前不需要引入 FastAPI、消息队列、ORM、多智能体框架、插件市场或通用策略 DSL。
- 先关闭本轮 P1、补齐结果契约和真实运行时测试，再推进飞书/微信接入。

## 检查项目

- [ ] 完全满足 `docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- [x] 保持清晰分层，Agent Loop 未直接访问文件、进程或 SQLite
- [x] write/edit 审批绑定内容摘要和目标状态
- [x] Shell 展开绕过已关闭
- [x] Bash stdout/stderr 内存保留有界
- [x] 工具或审计异常能使任务进入明确终态
- [ ] 四工具共享一致的敏感资源边界
- [ ] 审批和工具审计均不持久化原始敏感参数
- [ ] 默认 Windows Bash 运行时可用且身份明确
- [ ] read/Bash 的模型可见截断信息和总量契约可靠
- [ ] 真实模型调用具备及时取消能力
- [ ] 计划测试矩阵已完整覆盖
- [x] 当前测试、Ruff、编译和 diff 检查通过
- [x] Reviewer 未修改业务代码

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest
结果：55 passed，1 skipped
跳过项：Windows 当前权限不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall src
结果：通过

git diff --check 09f539d..HEAD
结果：通过

审查开始与业务验证结束时 git status --short
结果：clean

附加最小复现：
- bash_rg_credentials_allowed=True
- bash_rg_dotenv_allowed=True
- bash_ls_credentials_allowed=True
- bash_approval_audit_contains_unlabeled_sentinel=True
- auto_bash_path=C:\Users\likai\AppData\Local\Microsoft\WindowsApps\bash.exe
- auto_bash_rg_exit_code=127
- auto_bash_python_exit_code=127
- explicit_git_bash_pwd/rg/git/python_success=True
- read_configured_max_bytes=5
- read_reported_file_bytes=5
- read_actual_result_bytes=72
- read_utf8_character_bytes_with_limit_1=3
- read_mid_codepoint_cursor_reports_invalid_utf8=True
- bash_truncated_metadata=True
- bash_truncation_marker_visible_to_model=False
```

说明：复审没有读取项目根目录 `.env` 的内容，也没有输出或构造真实密钥；敏感路径复现只评估命令策略，使用的是虚构文件名和哨兵文本。

## 复审门槛

1. 修复本轮全部 P1：跨工具敏感资源策略、Bash 审批最小审计、Git Bash 运行时发现与验证。
2. 为每个 P1 增加修复前失败、修复后通过的回归测试。
3. 修复 P2-1/P2-2，确保模型可见的 read/Bash 结果在预算内且保留截断/游标信息。
4. 明确 P2-3 是当前版本接受的已知限制，或改为可取消 HTTP 实现；远程渠道接入前必须关闭。
5. 重新执行 pytest、Ruff、compileall、`git diff --check`，并在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复与验证结果。

## 最终意见

**CHANGES_REQUIRED**

现有架构方向正确，上一轮多数高优先级问题已关闭。本轮阻塞集中在 Bash 的跨工具安全一致性、审计数据最小化和 Windows 运行时选择；这些问题范围明确，适合在当前结构内做小步修复。
