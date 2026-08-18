# 第四轮代码复审报告（归档）

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-17
- 对应计划：`docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- 上一轮报告：[`REVIEW_ROUND_3.md`](./REVIEW_ROUND_3.md)
- 修复交接：`docs/Implement/IMPLEMENTATION_NOTES.md`
- 上轮审查提交：`9d60872`（Document third-round code review findings）
- 本轮实现提交：`874a578`（Close third-round review findings）
- 对应分支：`agent/publish-mvp`
- 用例附件：[`REVIEW_USE_CASE_ROUND_4.svg`](./REVIEW_USE_CASE_ROUND_4.svg) / [`REVIEW_USE_CASE_ROUND_4.png`](./REVIEW_USE_CASE_ROUND_4.png)
- Reviewer 边界：仅归档上一轮报告并在 `docs/Review/` 新增第四轮报告和用例图；未修改业务代码、测试、配置、计划或实现交接文件

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

第三轮提出的工作区祖先误拒绝、`read` 最小 UTF-8 预算和显式符号链接校验已经关闭；小写规范名称下的 `rg` 递归排除、裸 `git diff` 拒绝和正常成功结果的总预算也已实现。全量测试、Ruff、编译和提交差异检查均通过。

但 Bash 仍有两条可复现的敏感正文回传路径：`rg` 排除 glob 与共享路径策略的大小写语义不一致；允许的 `git diff --check` 会输出含尾随空格的完整违规行。两种场景都可把无标签哨兵放入 `ToolOutput.content`，随后由 Agent Loop 回填模型。因此本轮仍有 2 个 P1，不建议接入真实敏感工作区或远程消息渠道。

## 第三轮问题关闭情况

| 第三轮问题 | 本轮状态 | 复审判断 |
|---|---|---|
| P1-1 Bash 递归、仓库正文和真实路径访问 | 部分关闭 | 显式链接、规范小写敏感名称和裸 `git diff` 已处理；混合大小写名称及 `git diff --check` 仍可回传正文 |
| P2-1 `read` 小预算游标不前进 | 已关闭 | 配置和工具构造均要求至少 4 字节，最小合法预算下游标可推进 |
| P2-2 最终模型消息超过预算 | 部分关闭 | 正常成功结果已统一限长；极小合法预算丢失游标，未知工具和异常结果绕过格式化器 |
| P2-3 工作区祖先名称误拒绝 | 已关闭 | 敏感判断改为只检查工作区相对路径 |
| P2-4 真实模型取消 | 本地 MVP 接受 | 实现交接已明确为 best-effort；远程长期任务接入前仍须关闭 |
| P2-5 测试矩阵缺口 | 部分关闭 | 新增 7 项有效回归测试；本轮两个 P1 和错误消息预算仍无覆盖，2 个链接测试继续跳过 |

## 做得较好的部分

- `ToolRegistry.create()` 将真实 `WorkspacePathResolver` 注入 `CommandPolicy`，Bash 显式路径不再只做字符串判断。
- `WorkspacePathResolver` 先生成工作区相对路径再判断敏感性，修复了父目录名为 `private` 时拒绝所有普通文件的问题。
- `MAX_READ_BYTES >= 4` 同时在配置与 `ReadFileTool` 构造层设防，最小预算下能完整返回至少一个 Unicode code point 并推进游标。
- `ToolExecutor` 已开始统一拥有成功工具结果的模型消息预算，并优先保留安全 metadata；这是正确的职责收口方向。
- `rg` 自动注入 `--no-follow` 和保护性 glob，裸 `git diff` 被拒绝，说明实现已按命令访问语义收紧，而不是继续扩大通用字符串黑名单。
- 新增测试覆盖规范小写的递归敏感文件、`--files`、显式链接、祖先误拒绝、最小 read 预算和正常消息预算；77 项通过证明主链路未回归。

## 严重级别

- P0：阻塞发布，可能造成严重数据、权限或安全问题。
- P1：重要安全或核心功能错误，必须修复后才能 PASS。
- P2：一般功能边界、维护性问题或测试缺口。

## 问题列表

### P0

暂无。

### P1

#### P1-1：`rg` 保护 glob 区分大小写，可绕过共享敏感路径策略

- `src/likai_nexus/safety/paths.py:46-55` 会先把路径组件转成小写，因此文件工具正确拒绝 `Credentials.JSON`、`Private/notes.txt` 等大小写变体。
- `src/likai_nexus/safety/command_policy.py:36-76` 注入的保护 glob 全是小写，且没有 `--glob-case-insensitive`；`src/likai_nexus/safety/command_policy.py:131-133` 直接把它们附加到 `rg` argv。
- 隔离复现：在工作区创建只含虚构哨兵的 `nested/Credentials.JSON` 和 `nested/Private/notes.txt`。共享路径策略对两者均返回敏感；同一配置下实际 `BashTool` 执行 `rg <哨兵> .` 成功，返回正文包含完整哨兵。
- 影响：同一资源能否访问取决于文件名大小写和所选工具，违反“四工具不能绕过 Safety”的约束。在 Windows 的大小写不敏感文件系统上尤其容易由正常命名变化触发。
- 必须修改：让 `rg` 的保护规则与 `WorkspaceAccessPolicy` 使用同一大小写语义。最小方案是在不可移除的保护参数中加入 `--glob-case-insensitive`，并从同一常量源生成敏感名称/后缀/目录排除规则；无需引入通用策略 DSL。
- 必须测试：至少覆盖根目录与嵌套目录中的 `Credentials.JSON`、`SECRETS.Json`、`PRIVATE.PEM`、`Private/notes.txt`、`.SSH/id_rsa`，同时验证搜索和 `rg --files`，并断言哨兵不进入模型消息、CLI 或 SQLite。

#### P1-2：允许的 `git diff --check` 会把违规行正文返回模型

- `src/likai_nexus/safety/command_policy.py:193-197` 将 `--check` 与 `--stat`、`--name-only` 一起视为只读展示选项。
- `git diff --check` 不只返回状态：当新增行含尾随空格时，它会输出文件位置和该行正文。隔离仓库复现中，包含虚构哨兵与尾随空格的修改使命令退出码为 2，实际 `BashTool` 的错误结果仍包含完整哨兵。
- `src/likai_nexus/orchestrator/agent_loop.py:136-138` 不区分成功或失败结果，都会把 `result.content` 作为 tool 消息回填模型；非零退出码不能阻止泄露。
- 影响：第三轮要求的“仓库级正文输出不得绕过敏感资源策略”仍未满足，而且修复提交没有增加 `git diff` 哨兵回归测试。
- 必须修改：从模型可调用的 Bash 策略移除 `git diff --check`。Reviewer/CI 可继续直接运行该命令；若未来确需提供给模型，应由专用适配器只返回问题计数和脱敏位置，不返回违规行正文。
- 必须测试：在临时 Git 仓库修改普通文件、敏感名称文件及大小写变体，令新增行含无标签哨兵和尾随空格；断言命令被策略拒绝，哨兵不进入模型消息、CLI 或 SQLite。

### P2

#### P2-1：最终工具消息预算只覆盖正常执行路径，且极小合法预算会丢失续读状态

- `src/likai_nexus/config.py:102-111` 只要求 `max_output_bytes > 0`；1、16、32 字节均是合法配置。
- `src/likai_nexus/executor/service.py:272-285` 在状态信封放不下时把截断状态退化成单个 `!`。实测预算为 1、16、32 时消息虽未超限，但均不含 `next_cursor` 或 `truncated`，模型无法继续读取；64 字节时才保留两者。
- `_model_content()` 只在具体工具正常返回的路径调用（`src/likai_nexus/executor/service.py:86-93`）。未知工具和捕获异常分别在 `src/likai_nexus/executor/service.py:57`、`133-138` 直接构造结果，绕过总预算。隔离复现中配置预算 16 字节，1000 字符的未知工具名产生 1081 字节模型消息。
- 影响：README 所述“`MAX_OUTPUT_BYTES` 限制最终回填模型的完整工具消息”尚未成为不变量；极小配置还破坏 `read` 的分页协议。
- 建议：让所有 `ToolResult`（成功、工具失败、参数错误、审批拒绝、未知工具）经过同一个最终格式化出口；为状态信封定义最小合法预算，或使用固定短字段的 ASCII 信封并在配置层拒绝放不下游标的值。

#### P2-2：安全回归测试仍未覆盖本轮实际绕过与完整消费链路

- `tests/unit/test_file_tools.py:287-324` 只使用规范小写的 `credentials.json`、`.env.local`、`private.pem` 和 `private/`，未覆盖共享策略支持的大小写不敏感语义。
- `tests/unit/test_config_and_safety.py:117-156` 验证裸 `git diff` 被拒绝、保护参数存在，但没有执行 `git diff --check` 并检查正文。
- `tests/unit/test_bash_and_backend.py:109-118` 只验证 64 字节成功格式化，未覆盖合法极小预算和异常/未知工具路径。
- 2 个符号链接用例在当前 Windows 环境继续跳过；应在 Linux CI 或启用 Developer Mode 的 Windows job 中至少保留一条不可跳过的链接/目录连接验证。
- 建议增加 Fake Backend 驱动的 Agent Loop/CLI 集成测试，直接断言无标签哨兵不出现在 tool 消息、终端输出和 SQLite，而不只验证底层 `ToolOutput`。

## 架构与功能优化建议

### 第一优先级：建立单一敏感资源规则源

1. 保留当前简单常量方案，但由 `WorkspaceAccessPolicy` 同时提供文件路径判断和 `rg` 排除参数，避免两份名称列表随时间漂移。
2. 显式定义跨平台大小写语义；Windows 本地 MVP 建议对保护 glob 强制大小写不敏感，测试在 Linux 上也采用相同契约。
3. 把“进程可以执行”与“输出可以进入模型”分开判断。`git diff --check` 是只读命令，但其输出不是无正文输出。

### 第二优先级：统一所有工具结果的最终消息出口

1. `ToolExecutor.execute()` 的所有返回分支都只产生结构化状态，最终统一经过 `ToolMessageFormatter`；不要让未知工具和异常分支自行拼接模型正文。
2. 在配置层给 `MAX_OUTPUT_BYTES` 设置能容纳最小状态信封的下限，并为 `next_cursor`、`truncated`、`error_type` 定义固定优先级。
3. 对 path、工具名和错误详情分别设置长度上限，避免结构化字段自身突破上下文预算。

### 第三优先级：远程接入前关闭隔离与取消风险

1. 当前人工审批、命令允许列表和输出过滤只适合受信本地用户；`pytest` 等仍能执行项目代码，不构成 OS 级沙箱。
2. 飞书/微信接入前使用独立低权限账户、容器或 Windows Sandbox，并在隔离层关闭网络和工作区外文件访问。
3. 将 `urllib + asyncio.to_thread()` 替换为支持异步取消的 HTTP 传输；在此之前保持“真实模型取消为 best-effort”的明确文档和发布门槛。

## 检查项目

- [ ] 完全满足 `docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- [x] 保持清晰分层，Agent Loop 未直接访问文件、进程或 SQLite
- [x] write/edit 审批绑定内容摘要和目标状态
- [x] Bash 审批和工具调用审计不保存原始命令参数
- [x] Windows Git Bash 运行时身份明确，WSL 入口被拒绝
- [x] 显式 Bash 路径使用真实解析器并拒绝链接
- [x] 工作区祖先名称不会误拒绝普通相对路径
- [x] read 在所有合法正文预算下推进游标或结束
- [ ] `rg` 与文件工具对敏感名称使用一致的大小写语义
- [ ] Git 命令不会把 diff 正文返回模型
- [ ] 所有模型可见工具消息满足统一总量限制并保留必要状态
- [ ] 真实模型调用具备及时取消能力
- [ ] 计划测试矩阵已完整覆盖
- [x] 当前测试、Ruff、编译和 diff 检查通过
- [x] Reviewer 未修改业务代码

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest -q
结果：77 passed，2 skipped
跳过项：Windows 当前权限不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check 9d60872..HEAD
结果：通过

审查开始、全量验证结束时 git status --short
结果：clean

附加隔离最小复现：
- shared_policy_uppercase_sensitive=True
- rg_uppercase_sensitive_output=True
- bash_tool_error=False
- bash_tool_contains_uppercase_sensitive_sentinel=True
- policy_allows_git_diff_check=True
- git_diff_check_exit=2
- bash_git_check_error=True
- bash_git_check_contains_sentinel=True
- budget=1/16/32 时 next_cursor_visible=False、truncated_visible=False
- budget=64 时 next_cursor_visible=True、truncated_visible=True
- configured_budget=16，unknown_tool_message_bytes=1081
```

说明：复审没有读取项目根目录 `.env` 内容，也没有使用、输出或构造真实密钥；敏感资源和 Git diff 复现均在自动清理的临时目录中使用虚构文件与哨兵文本。

## 下次复审门槛

1. 关闭 P1-1：`rg` 对敏感文件、后缀和目录的大小写变体均不可搜索或列出。
2. 关闭 P1-2：模型可调用命令不得返回 Git diff 正文；增加 `git diff --check` 无标签哨兵回归。
3. 修复 P2-1：所有 ToolResult 统一受完整消息预算约束，所有合法预算下分页状态可见或配置明确失败。
4. 增加从 Bash/ToolExecutor 到 Agent Loop、CLI、SQLite 的端到端无泄露断言，并在可创建链接的 CI 环境运行链接测试。
5. 重新执行 pytest、Ruff、compileall 和 `git diff --check`，在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复证据。

## 最终意见

**CHANGES_REQUIRED**

本轮修复方向正确，第三轮的路径误拒绝和 read 游标问题已经实质关闭；剩余阻塞不需要重做架构。优先统一 `rg` 的大小写策略并移除 `git diff --check`，随后把消息预算覆盖到所有返回分支，即可进入下一轮复审。
