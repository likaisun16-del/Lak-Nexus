# 第五轮代码复审报告（归档）

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-17
- 对应计划：`docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- 上一轮报告：[`REVIEW_ROUND_4.md`](./REVIEW_ROUND_4.md)
- 修复交接：`docs/Implement/IMPLEMENTATION_NOTES.md`
- 上轮审查提交：`b05fbf0`（Document fourth-round code review findings）
- 本轮实现提交：`94c7051`（Fix fourth-round review findings）
- 对应分支：`agent/publish-mvp`
- 用例附件：[`REVIEW_USE_CASE_ROUND_5.svg`](./REVIEW_USE_CASE_ROUND_5.svg) / [`REVIEW_USE_CASE_ROUND_5.png`](./REVIEW_USE_CASE_ROUND_5.png)
- 工作区说明：审查开始前已有用户未提交的 `AGENTS.md` 修改；Reviewer 全程保留且未改动
- Reviewer 边界：仅归档上一轮报告并在 `docs/Review/` 新增第五轮报告和用例图；未修改业务代码、测试、配置、计划、实现交接或用户的 `AGENTS.md`

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

第四轮的两个 P1 已有效关闭：混合大小写的敏感文件、后缀和目录不再能通过 `rg` 搜索或列出，`git diff --check` 也已从模型可调用的允许列表移除。未知工具、参数/审批/执行异常与正常工具结果统一进入消息预算出口，81 项测试、Ruff、编译和差异检查均通过。

但统一消息预算引入了一个新的核心读取错误：`ReadFileTool` 先按原始正文计算 `next_cursor`，`ToolExecutor` 为状态信封再次截短正文，却继续发送原游标。复审确认，合法 64 字节配置下 64 字节文件只向模型展示 16 字节，游标已经到 EOF；默认 64 KiB 配置下也会永久跳过 191 字节。因此本轮有 1 个 P1，读取较大文件时模型可能在无报错的情况下遗漏内容。

## 第四轮问题关闭情况

| 第四轮问题 | 本轮状态 | 复审判断 |
|---|---|---|
| P1-1 `rg` 大小写绕过敏感资源策略 | 已关闭 | 排除模式由共享策略生成并启用 `--glob-case-insensitive`；文件与目录大小写变体专项复现通过 |
| P1-2 `git diff --check` 回显正文 | 已关闭 | 该选项被策略拒绝，`--stat` 和 `--name-only` 仍可用 |
| P2-1 工具消息预算未覆盖所有分支 | 部分关闭 | 所有返回分支已统一限长；但对 read 正文的二次截断没有同步游标和 metadata |
| P2-2 安全回归测试缺口 | 部分关闭 | 新增大小写、Git diff 和未知工具预算测试；仍缺少“经 ToolExecutor 分页重组原文”的不变量测试 |
| 真实模型取消与 OS 隔离 | 本地 MVP 接受 | 仍是远程渠道接入前必须关闭的已知限制 |

## 做得较好的部分

- `WorkspaceAccessPolicy.rg_exclude_globs()` 让文件工具判断和 `rg` 排除共享同一组名称、目录与后缀，避免两份安全名单继续漂移。
- 保护参数追加在模型参数之后，并包含 `--no-follow`、`--glob-case-insensitive`，模型无法通过前置 glob 重新包含敏感路径。
- `git diff` 只保留 `--stat`、`--name-only` 两种不返回行正文的形式；隔离复现确认 `--check` 被拒绝。
- `_tool_result()` 已成为成功、未知工具及普通异常结果的统一模型消息出口，修复了错误分支无限扩张上下文的问题。
- `MAX_OUTPUT_BYTES >= 64` 能让常见 read 游标和错误类型进入压缩状态信封；未知工具 1000 字符名称在 64 字节配置下仍受限且保留错误类型。
- 新增的安全测试直接经过 `ToolExecutor`、真实 Git Bash 与 SQLite 审计链路，较上一轮只检查静态 argv 更有价值。

## 严重级别

- P0：阻塞发布，可能造成严重数据、权限或安全问题。
- P1：重要安全或核心功能错误，必须修复后才能 PASS。
- P2：一般功能边界、维护性问题或测试缺口。

## 问题列表

### P0

暂无。

### P1

#### P1-1：最终消息二次截断后仍使用原 read 游标，分页会永久跳过未展示字节

- `src/likai_nexus/config.py:76-78` 默认把 `max_output_bytes` 和 `max_read_bytes` 都设为 64 KiB；`src/likai_nexus/executor/registry.py:30-35` 分别把这两个相同预算交给 `ReadFileTool` 和最终模型消息格式化器。
- `src/likai_nexus/executor/tools/read_file.py:148-159` 按 read 原始正文前缀计算 `next_byte_offset = start + len(prefix)`。
- `src/likai_nexus/executor/service.py:281-292` 随后为 metadata 状态信封预留空间并再次截短正文，但只修改局部 `safe_metadata["truncated"]`，没有根据实际保留正文回退 `next_cursor`，也没有更新返回的 `ToolResult.metadata`。
- `src/likai_nexus/orchestrator/agent_loop.py:134-140` 只把格式化后的 `result.content` 发送模型；模型下一次只能使用消息里的旧游标，因此被二次截掉的正文没有任何补读入口。

隔离复现：

1. `MAX_READ_BYTES=64`、`MAX_OUTPUT_BYTES=64`，文件恰好包含 64 个 ASCII 字节。
2. read 原始结果报告 `bytes=64`、`truncated=False`、`next_cursor=1:0`。
3. 最终工具消息受状态信封挤压，只保留 16 个文件字节并标记 `truncated=true`。
4. 按 `1:0` 继续读取已经到 EOF，剩余 48 字节永久不可见；SQLite 审计仍记录“截断=False”。
5. 使用默认 64 KiB 预算读取超长单行时，原游标为 `0:65536`，模型只看到前 65,345 字节，下一页从 65,536 开始，静默跳过 191 字节。

影响：

- 读取源码、配置或文档时会在无报错情况下丢失中间片段，模型可能基于不完整上下文给出错误分析或修改建议。
- `ToolResult.content`、`ToolResult.metadata` 和 SQLite 审计对“实际向模型展示了多少字节、是否截断”给出互相矛盾的答案。
- 该问题命中默认配置，不是仅存在于极小测试预算的理论边界。

必须修改：

1. read 分页正文只能由一个组件拥有字节预算和游标计算。优先方案是在调用 `ReadFileTool` 前预留固定、可证明足够的状态信封预算，让工具按“最终可展示正文预算”读取并生成游标；最终格式化器不得再次截短 read 正文。
2. 如果选择在最终格式化器中截断 read，则必须基于实际保留的 UTF-8 正文重新计算行号与行内字节游标，并同步更新 `ToolResult.metadata` 与审计摘要；不要只改局部 metadata 副本。
3. 明确配置关系，例如保证 `MAX_READ_BYTES + STATUS_ENVELOPE_BYTES <= MAX_OUTPUT_BYTES`，并更新默认值与 README。不要继续让两个默认上限相等后再二次截断。

必须测试：

- 通过完整 `ToolExecutor` 和 Fake Backend 连续读取并重组文件，断言重组结果与原文件逐字节一致，没有缺口或重复。
- 覆盖恰好等于预算、超过预算、超长单行、多行、中文 3 字节字符和 4 字节字符。
- 每一页断言：模型可见正文末端对应 `next_cursor`，完整消息不超过预算，`ToolResult.metadata` 与审计中的截断状态和最终消息一致。

### P2

#### P2-1：64 字节被描述为“可容纳工具状态信封”，但并非所有状态组合都能结构化保留

- `src/likai_nexus/config.py:112-113` 的错误文案声称 64 字节足以容纳工具状态信封。
- `src/likai_nexus/executor/service.py:302-315` 在压缩状态仍放不下时退化为单个 `!`。实测 Bash 的 `exit_code + timed_out + cancelled + truncated` 组合在 64 字节下会丢失所有结构化键，仅依赖 Bash 正文中的成功、退出码和截断文案。
- 常规 Bash 实际结果目前仍能从正文看到成功、退出码和截断标记，因此不构成本轮第二个 P1；但“所有状态信封都能保留”的配置契约并不成立。
- 建议为每种工具定义必要状态的最小紧凑信封，或把配置下限提高到能容纳最大必要组合；不要使用无语义的 `!` 作为最终降级协议。

#### P2-2：测试分层未验证“正文预算—状态信封—游标”联合不变量

- `tests/unit/test_file_tools.py:40-109` 直接测试 `ReadFileTool`，只能证明工具内部游标相对其原始正文正确。
- `tests/unit/test_bash_and_backend.py:109-118` 用人工构造的正文和游标测试 `_model_content()`，只断言预算与字段存在，没有断言游标对应最终保留正文。
- 两层测试分别通过，却没有覆盖组合后产生的默认配置数据缺口。应增加 ToolExecutor/Agent Loop 级分页重组测试。
- 2 个符号链接测试在当前 Windows 环境仍跳过；继续建议在 Linux CI 或启用 Developer Mode 的 Windows job 中提供不可跳过的链接/目录连接验证。

## 架构与功能优化建议

### 第一优先级：让分页生产者拥有最终正文预算

1. 将工具状态信封预留量定义为明确常量，由 Registry 在构造 read 工具时计算可用正文预算。
2. `ReadFileTool` 用该最终正文预算读取并计算游标；`ToolExecutor` 只封装，不再改变分页正文。
3. 如果不同工具需要不同状态字段，使用小型、按工具定义的格式化函数即可，不必引入通用策略 DSL 或复杂消息框架。

### 第二优先级：统一最终结果与审计事实

1. 格式化出口应返回“最终 content + 最终 metadata”，而不是只返回字符串。
2. 审计摘要应基于最终 metadata，明确区分“工具读取字节数”和“模型可见字节数”。
3. 对 read 记录起始游标、最终游标和模型可见正文长度，仍然不保存正文内容。

### 第三优先级：把不变量测试放在消费边界

1. 单测保留 read 的 UTF-8 与行游标算法。
2. 新增 ToolExecutor 测试验证预算封装不会改变分页语义。
3. 新增 Agent Loop 测试，让 Fake Backend 连续使用上一页消息中的游标，并验证最终重组结果。
4. 远程接入前再关闭真实 HTTP 及时取消和 OS 级隔离；当前不需要引入消息队列、ORM 或多智能体抽象。

## 检查项目

- [ ] 完全满足 `docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- [x] 保持清晰分层，Agent Loop 未直接访问文件、进程或 SQLite
- [x] write/edit 审批绑定内容摘要和目标状态
- [x] Bash 审批和工具调用审计不保存原始命令参数
- [x] `rg` 与文件工具对敏感名称使用一致的大小写语义
- [x] 模型可调用 Git 命令不会返回 diff 行正文
- [x] 成功与普通错误 ToolResult 均受最终消息字节上限约束
- [x] read 工具内部 UTF-8 游标可推进
- [ ] read 游标对应最终模型可见正文，分页无缺口或重复
- [ ] 最终 content、metadata 与审计截断状态一致
- [ ] 所有合法预算下必要状态使用明确、可解析的信封
- [ ] 真实模型调用具备及时取消能力
- [ ] 计划测试矩阵已完整覆盖
- [x] 当前测试、Ruff、编译和 diff 检查通过
- [x] Reviewer 未修改业务代码或用户的 `AGENTS.md`

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest -q
结果：81 passed，2 skipped
跳过项：Windows 当前权限不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check b05fbf0..HEAD
结果：通过

git diff --check
结果：通过；仅提示用户已有 AGENTS.md 的 LF/CRLF 转换警告

审查开始时 git status --short
结果：M AGENTS.md（用户已有修改，Reviewer 未触碰）

第四轮 P1 专项复现：
- shared_policy_all_sensitive=True
- rg_search_contains_sentinel=False
- rg_files_contains_sensitive_path=False
- rg_files_contains_normal=True
- git_diff_allowed=False
- git_diff_check_allowed=False
- git_diff_stat_allowed=True
- git_diff_name_only_allowed=True

消息预算专项复现：
- actual_bash_message_bytes=64
- actual_bash_success_visible=True
- actual_bash_exit_code_visible=True
- actual_bash_truncated_visible=True
- unknown_tool_message_bytes=64
- unknown_tool_error_type_visible=True
- unknown_tool_truncated_visible=True

read 游标缺口复现：
- 64 字节文件：model_visible_source_bytes=16，next_cursor=1:0，follow_up_contains_source=False
- 64 字节文件：ToolResult.metadata.truncated=False，model_message.truncated=True，audit_truncated=False
- 默认 64 KiB：raw_bytes_reported=65536，model_visible_source_bytes=65345
- 默认 64 KiB：next_cursor=0:65536，permanently_skipped_bytes=191
```

说明：复审没有读取项目根目录 `.env` 内容，也没有使用、输出或构造真实密钥；安全与分页复现均在自动清理的临时目录中使用虚构文件和哨兵文本。

## 下次复审门槛

1. 关闭 P1-1：最终模型可见 read 正文与 `next_cursor` 必须严格连续，默认及最小合法预算均不得跳字节。
2. 同步最终 `ToolResult.metadata` 和审计摘要，避免模型消息标记截断而 metadata/SQLite 记录未截断。
3. 增加 ToolExecutor 与 Agent Loop 级分页重组测试，覆盖 ASCII、多行和多字节 UTF-8。
4. 为最小状态信封建立可验证下限，移除无语义 `!` 降级或把它替换成可解析的紧凑状态。
5. 重新执行 pytest、Ruff、compileall 和 `git diff --check`，并在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复证据。

## 最终意见

**CHANGES_REQUIRED**

第四轮安全绕过已经有效关闭，架构不需要重做。当前唯一阻塞集中在 read 分页生产者与最终消息格式化器各自截断一次，导致游标和模型实际所见正文脱节；让 read 直接按最终正文预算生成游标，即可进入下一轮复审。
