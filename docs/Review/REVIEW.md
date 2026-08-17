# 代码审查报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-17
- 对应计划：`docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- 审查范围：四工具 MVP 提交 `09f539d` 的业务代码、配置、测试和实现交接
- 对应分支：`agent/publish-mvp`
- 工作区说明：审查开始时实现尚未提交，审查期间由外部流程提交并切换到当前分支；Reviewer 未修改业务代码
- 用例附件：[`docs/Review/REVIEW_USE_CASE.svg`](./REVIEW_USE_CASE.svg) / [`docs/Review/REVIEW_USE_CASE.png`](./REVIEW_USE_CASE.png)

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

当前实现的分层方向与计划基本一致，39 项测试通过、1 项符号链接测试因 Windows 权限跳过，Ruff 和编译检查通过。但审查复现确认仍存在工作区边界绕过、敏感内容持久化、任务状态残留、读取分页停滞、审批信息不足和 Bash 输出无界缓冲等阻塞问题。修复 P1 并补齐相应回归测试前，不应进入真实模型或远程渠道使用阶段。

## 总体评价

### 做得较好的部分

- `CLI → AgentLoop → ToolExecutor → Safety/Tool → Storage` 的主依赖方向符合计划。
- Agent Loop 没有直接访问文件、进程或 SQLite，具体模型协议也被限制在 Backend 层。
- 四个工具统一经过 `ToolExecutor`；可预期工具错误会以 `ToolResult` 回填模型。
- 文件路径解析、原子替换、编辑唯一匹配、SQLite 参数化 SQL、模型异常脱敏摘要等基础能力已经形成。
- 代码结构保持了 MVP 所需的简洁度，没有提前引入 FastAPI、ORM、插件市场或多智能体框架。

### 当前功能覆盖

| 能力 | 结论 | 主要原因 |
|---|---|---|
| CLI 单次任务 | 部分通过 | 可启动和返回退出码，但真实取消链路未完整验证 |
| 可替换模型后端 | 基本通过 | 协议边界清晰，但阻塞 HTTP 工作线程不能被及时取消 |
| Agent Loop | 不通过 | 工具/审计异常可逃逸并让任务残留为 `running` |
| `read` | 不通过 | 超长单行触发字节截断后无法推进 offset |
| `write` / `edit` | 不通过 | 审批预览不足，完整内容仍可能进入审计库 |
| `bash` | 不通过 | 校验后的原始字符串仍交给 Shell 展开，可绕过工作区路径限制 |
| 审批与审计 | 不通过 | 审批未绑定实际内容；敏感信息和源码可能持久化 |
| 测试与静态检查 | 部分通过 | 已有检查全绿，但没有覆盖本次确认的失败场景和计划中的若干矩阵项 |

## 严重级别

- P0：阻塞发布，可能造成严重数据、权限或安全问题。
- P1：重要功能或安全错误，必须修复。
- P2：一般问题、维护性问题或测试缺口。

## 问题列表

### P0

暂无。

### P1

#### P1-1：Bash 校验对象与实际执行对象不一致，可绕过工作区限制

- 证据：`CommandPolicy` 使用 `shlex` 校验解析后的 token，但 `BashTool` 最终把未经规范化的原始字符串传给 `bash -lc`。
- 最小复现：`CommandPolicy().evaluate("ls $HOME").allowed` 和 `CommandPolicy().evaluate("ls {../*,.}").allowed` 均为 `True`。实际 Shell 会继续执行变量、brace、glob 和启动脚本展开，可能访问工作区外路径。
- 影响：字符串允许列表不能兑现“Bash 只访问工作区”的安全承诺，也可能受 `BASH_ENV`、登录脚本、PATH 或命令包装影响。
- 必须修改：校验与执行必须使用同一份规范化 argv；优先直接执行绝对路径绑定的允许命令，不再把原始字符串交给 Shell。若仍保留 Bash，应禁用 profile/rc、清理影响执行语义的环境变量，并为变量展开、brace 展开和父目录 glob 增加拒绝测试。
- 位置：`src/likai_nexus/safety/command_policy.py:30`、`src/likai_nexus/executor/tools/bash.py:82`

#### P1-2：敏感文件和完整工具内容可进入模型上下文或 SQLite

- 证据：工具执行器把完整 arguments 交给通用 `redact_arguments()`；`content`、`old_text`、`new_text` 不是敏感键，因此普通源码或无标签密钥会原样入库。任务仓储也直接保存原始 `request_text`。路径策略允许读取工作区内 `.env`。
- 最小复现：无标签哨兵文本在一次 `write` 后仍出现在 `tool_calls.arguments_redacted`，也原样出现在 `tasks.request_text`；工作区内 `.env` 可通过路径解析和 `read` 检查。
- 影响：违反“密钥、Token、Cookie 和密码不出现在日志或数据库中”的验收要求；读取结果还可能被发送给模型并在最终答案中回显。
- 必须修改：为每种工具定义显式审计投影，只保存路径、动作、长度、摘要哈希和结果状态，不保存 write 正文或 edit 原文/新文；对任务文本采用明确的脱敏/最小化策略；增加敏感路径策略，默认拒绝 `.env*`、私钥、凭据文件等内容，并验证哨兵不出现在数据库、模型消息和 CLI 输出中。
- 位置：`src/likai_nexus/executor/service.py:34`、`src/likai_nexus/safety/redaction.py:24`、`src/likai_nexus/storage/task_repository.py:24`、`src/likai_nexus/safety/paths.py:30`

#### P1-3：工具或审计异常会逃逸 Agent Loop，使任务残留在 `running`

- 证据：`AgentLoop.run()` 只兜底 `CancelledError`；`ToolExecutor.start_tool_call()` 又位于工具级 try 块之外。`tool_call_id` 是全局主键，模型在不同任务中复用 ID 会触发 `sqlite3.IntegrityError`。
- 最小复现：先写入 `duplicate-id`，再让第二个任务返回相同调用 ID，异常直接离开 Agent Loop，第二个任务状态保持 `running`。
- 影响：当前 CLI 进程内任务状态不真实，必须等下次启动恢复；也破坏了“模型调用失败、工具失败和取消都有明确状态”的验收标准。
- 必须修改：使用内部审计主键或 `(task_id, tool_call_id)` 复合唯一约束；在编排边界捕获工具/审计系统异常并尽力将任务终结为 `failed`；为工具开始、工具结束和任务状态建立一致的事务/补偿语义。
- 位置：`src/likai_nexus/orchestrator/agent_loop.py:77`、`src/likai_nexus/executor/service.py:34`、`src/likai_nexus/storage/database.py:35`

#### P1-4：`read` 对超长单行的分页游标不会前进

- 证据：当一行超过剩余字节上限时，代码追加部分字节并立即退出，但没有增加 `current_offset`；之后追加的继续读取提示又会被第二次字节截断移除。
- 最小复现：11 字节单行、`max_bytes=5` 时，首次 `next_offset=0`；使用该 offset 再读会得到完全相同的内容。
- 影响：模型无法继续读取该文件，可能重复调用直至达到最大轮数。
- 必须修改：明确采用字节游标或“行号 + 行内字节偏移”游标；继续读取信息必须保留在响应预算内；覆盖超长 ASCII 行、多字节 UTF-8 行和恰好达到上限的测试。
- 位置：`src/likai_nexus/executor/tools/read_file.py:92`

#### P1-5：人工审批信息不足，无法判断实际写入或修改内容

- 证据：write 审批只展示路径和字节数；edit 审批只展示文本长度、匹配次数以及固定文案“将替换唯一匹配块”，不展示安全受限的 diff 或内容摘要。
- 最小复现：edit 的 `new_text` 使用哨兵内容后，审批 summary 中完全没有该内容或内容哈希。
- 影响：两个长度相同但语义完全不同的修改会显示相同审批信息，人工审批不能构成有效安全边界。
- 必须修改：在审批前生成不可变的 Prepared Action，展示规范化相对路径、新建/覆盖动作、受限 diff/预览、字节数和内容摘要哈希；审批决定绑定该动作摘要，执行前若目标状态或摘要变化则重新审批。
- 位置：`src/likai_nexus/executor/tools/write_file.py:48`、`src/likai_nexus/executor/tools/edit_file.py:55`

#### P1-6：Bash 输出限制发生在完整缓冲之后，不能防止内存耗尽

- 证据：`process.communicate()` 会先把 stdout/stderr 全部读入内存，进程结束后才调用 `truncate_text()`。
- 影响：允许命令在超时窗口内产生大量输出时，64 KiB 配置只限制返回文本，不限制进程内存，存在本地拒绝服务风险。
- 必须修改：并发流式读取 stdout/stderr，只保留有界缓冲；达到上限后继续安全排空或终止进程，并保证超时、取消和进程树清理仍然生效。
- 位置：`src/likai_nexus/executor/tools/bash.py:152`

### P2

#### P2-1：真实模型请求无法被取消信号及时中止

- `OpenAICompatibleBackend` 使用 `asyncio.to_thread()` 包装阻塞 `urllib`。取消 await 不会终止底层线程或 HTTP 请求，最坏仍需等待模型超时。
- CLI 也没有显式创建并贯穿统一 cancel event；现有测试只覆盖“运行前已取消”，没有覆盖模型请求中、审批中或 Bash 运行中的取消。
- 建议在保持 Backend 抽象的前提下使用可取消的异步 HTTP 传输，或明确记录“请求取消为 best-effort”，并补充 Ctrl+C 退出码和任务最终状态集成测试。
- 位置：`src/likai_nexus/models/openai_backend.py:26`、`src/likai_nexus/channels/cli.py:24`

#### P2-2：配置模板包含未生效开关，`.env` 查找语义与 README 不一致

- `.env.example` 提供 `REQUIRE_APPROVAL_FOR_WRITE`、`REQUIRE_APPROVAL_FOR_COMMAND`，但 Settings 没有读取它们；`ALLOW_NETWORK_ACCESS` 虽被读取，却没有参与命令策略。
- README 声称读取“项目根目录 `.env`”，实现实际读取进程当前目录的 `.env`。
- 建议删除暂不支持的开关或完整接线；网络隔离未实现前不要提供看似能开放网络的配置。明确选择“当前目录”或可定位的应用配置目录，并同步文档和测试。
- 位置：`.env.example:47`、`src/likai_nexus/config.py:115`、`README.md:6`

#### P2-3：测试矩阵未覆盖计划中的关键边界

- 缺少：超长单行分页、write 原子失败后原文件完整、edit 审批拒绝、实际 diff 内容、Bash 变量/brace 绕过、子进程敏感环境清理、运行中取消、重复 tool_call_id、审计写入失败、完整 CLI + Fake Backend 链路。
- 当前符号链接逃逸测试在 Windows 被跳过，尚没有不依赖本机开发者权限的替代验证环境。
- `tests/integration/test_cli_agent.py` 当前只覆盖参数解析和缺少配置，并未跑通计划声明的三条端到端验收场景。

## 架构与功能优化建议

### 第一优先级：收紧安全执行边界

1. 把 `bash` 从“验证字符串后交给 Shell”改为“解析一次、执行同一 argv 的受控命令运行器”。这仍可对模型暴露名为 `bash` 的工具，但内部不应依赖 Shell 二次解释。
2. 在 `ToolExecutor` 中引入轻量 `PreparedToolAction`：包含规范化参数、规范化路径、风险类型、审批预览、内容摘要和安全审计字段。Safety、审批、执行和审计共享同一份不可变数据，减少重复解析与审批后漂移。
3. 将“脱敏”拆成两层：敏感资源访问策略决定“能不能读”，审计投影决定“允许记录什么”。通用正则只能作为最后兜底，不能承担主要安全职责。

### 第二优先级：强化任务和审计一致性

1. 为任务状态增加合法迁移检查，例如 `pending → running → terminal`，终态不可重新进入 running。
2. 工具调用使用内部主键，并保留供应商 `tool_call_id` 作为普通字段；至少将唯一范围限制在 task 内。
3. 对“工具调用开始、审批决定、执行结果、任务最终状态”定义清楚的事务或失败补偿规则，确保任何异常都有可诊断终态。

### 第三优先级：补齐可取消、可限流的 I/O

1. 模型 HTTP、Bash stdout/stderr 和未来网络工具都应采用有界、可取消的异步 I/O。
2. `read` 明确分页游标契约，不要用单一行号同时承担行数截断和字节截断。
3. 给 write/edit 输入增加合理大小上限，避免模型请求和 SQLite 审计被超大参数拖垮。

### 暂不建议增加的复杂度

- 当前不需要引入 FastAPI、消息队列、ORM、多智能体、插件系统或通用工作流引擎。
- 先修复安全闭环、状态一致性和测试矩阵，再评估远程渠道与 OS 级沙箱。

## 检查项目

- [ ] 是否满足 `docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- [ ] 是否不存在重要逻辑错误
- [x] 是否保持清晰分层
- [ ] 是否正确处理关键边界条件
- [ ] 是否正确处理所有系统级异常
- [ ] 是否不存在权限绕过
- [ ] 是否对四工具统一限制工作区路径
- [ ] 是否不会泄露或持久化敏感信息
- [ ] 是否不存在重复执行/重复调用 ID 风险
- [ ] 是否具备完整超时和取消机制
- [ ] 是否补充计划要求的必要测试
- [x] 是否无明显无关业务代码改动
- [x] 是否通过当前 lint、测试和编译检查

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest
结果：39 passed，1 skipped

.\.venv\Scripts\python.exe -m ruff check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall src
结果：通过

git diff --check
结果：通过

附加最小复现：
- bash_env_path_allowed=True
- bash_brace_escape_allowed=True
- sensitive_filename_read_allowed=True
- long_line_next_offset=0
- long_line_repeats=True
- approval_exposes_new_content=False
- write_content_persisted=True
- request_text_persisted=True
- duplicate_id_task_status=running
```

说明：未读取项目根目录 `.env` 内容；仅通过 `git check-ignore` 确认该文件已被忽略且未被 Git 跟踪。

## 复审门槛

1. 修复全部 P1。
2. 为每个 P1 增加能够在修复前失败、修复后通过的回归测试。
3. 补齐 P2-1 的运行中取消验证，并明确配置开关语义。
4. 重新执行 pytest、Ruff、compileall 和 `git diff --check`。
5. Fixer 在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复范围和验证结果后，再请求 Reviewer 复审。

## 最终意见

**CHANGES_REQUIRED**

架构骨架可以保留，不需要推倒重来；当前阻塞点集中在安全执行边界、审计数据最小化、任务终态一致性和边界测试。完成上述修复后再进行复审。
