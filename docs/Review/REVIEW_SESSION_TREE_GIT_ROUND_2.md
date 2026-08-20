# （归档）Session 树形会话与 Git Commit 关联第二轮复审报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-19
- 计划文档：`docs/Planner/SESSION_TREE_GIT_IMPLEMENTATION_PLAN.md`
- 审查基线：`dd6dda63413485d82a0e8963570ece91b0844eb1` 加当前未提交 Fixer 改动
- 上一轮归档：[`REVIEW_SESSION_TREE_GIT_ROUND_1.md`](./REVIEW_SESSION_TREE_GIT_ROUND_1.md)
- 用例附件：[`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_2.svg`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_2.svg) / [`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_2.png`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_2.png)
- Reviewer 边界：只更新 `docs/Review/`，未修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

上一轮 5 个 P1 的正常路径均已实质关闭，Git 索引零写入、Commit 资格、失败消息追踪、跨 Session 拒绝和版本查询提示已落地。但最新版在重复 Task 幂等和版本附加能力故障隔离上出现两个新的 P1，会破坏 Message↔Task 事实一致性或让成功 Task 对调用方表现为失败，因此暂不能 PASS。

## 新阻断问题

### P1-1：重复 task_id 会把新失败消息错误关联到旧成功 Task

位置：`src/likai_nexus/memory/session.py:177-182`、`src/likai_nexus/memory/session.py:320-335`

`AgentLoop` 对重复 Task ID 抛出 `TaskAlreadyExistsError` 后，Session 异常分支调用 `_ensure_failed_task()`；当旧 Task 已存在时 `create()` 返回 `False`，方法不创建新的失败执行事实，但随后仍把本次 user Message 回填为相同 `task_id + failed`。最终同一个旧 Task 保持 `success`，新 Message 却声称该 Task 为 `failed`。

Reviewer 独立复现：

```text
second_ask_raised=TaskAlreadyExistsError
original_task_status=success
second_user_task_id=duplicate-task
second_user_execution_status=failed
```

这违反计划第 22、68 行的 Message↔Task 可追踪和执行事实不可改写契约，也不满足项目 `AGENTS.md` 第 160 行的重复任务幂等要求。

修复要求：重复 ID 必须在关联本次 Message 前被识别；不得把新失败尝试绑定到旧 Task。可以拒绝并保留明确的“重复请求未创建 Task”状态，或建立独立执行尝试标识，但不能制造 Task=`success`、Message=`failed` 的矛盾事实。必须补成功 Task、失败 Task和取消 Task 的重复 ID 回归测试。

### P1-2：版本元数据写入或资格查询失败仍会让成功 ask() 抛异常

位置：`src/likai_nexus/memory/session.py:198-202`、`src/likai_nexus/memory/session.py:219-263`

CommitRepository 的 `record()` 已有降级保护，但完整版本附加链路没有统一隔离：`_has_successful_code_mutation()` 的审计查询和 assistant 保存后的 `set_message_version()` 都可能直接抛出。此时原 Task 已成功、assistant Message 也已保存，但 `SessionService.ask()` 向调用方抛异常，CLI 会把一次成功问答表现为启动失败。

Reviewer 对 `set_message_version()` 注入故障后的复现：

```text
ask_raised=RuntimeError
task_status=success
history_roles=['user', 'assistant']
assistant_content=已成功回答
```

这直接违反计划第 158 行“Commit 读取或保存失败不得影响原 Task、Message 或会话分支的既有结果”。同类风险也存在于任务前基线读取和审计资格查询异常。

修复要求：assistant 保存之后的整个版本资格、Git 读取、Commit 关联和原因持久化必须处于统一的旁路故障隔离中；任一环节失败都只能降级为安全的“未记录版本”诊断，不能覆盖或阻断成功回答。若连诊断原因也无法写入，仍应返回原成功结果。必须补基线读取、审计资格、CommitRepository 和版本原因写入四类故障注入测试。

## 上一轮 P1 复核

| 上一轮问题 | 当前结果 | 复核证据 |
|---|---|---|
| P1-1 Git 查询刷新索引 | 已关闭 | 同时使用 `--no-optional-locks` 和 `GIT_OPTIONAL_LOCKS=0`；Reviewer 以未来 mtime 独立复现，index 内容与 mtime 均不变 |
| P1-2 普通 Task 误绑 HEAD | 已关闭 | 需要成功 write/edit 审计、有效前后快照、HEAD 变化和干净工作区；普通问答明确返回未记录版本 |
| P1-3 失败/取消消息不可追踪 | 已关闭 | user Message 从 pending 回填 Task 与终态，失败重试保存来源 Message ID，历史展示状态与重试关系 |
| P1-4 跨 Session continue-from | 已关闭 | Service 强制接收当前 Session，跨 Session 在更新叶子前拒绝；两边活动叶子保持不变 |
| P1-5 查询缺 Task 与边界提示 | 已关闭 | commit/history 输出 Task、完整 SHA 或安全原因，并固定展示只覆盖已提交内容的限制说明 |

Git 零写入独立复现结果：

```text
commit_available=True
index_content_changed=False
index_mtime_changed=False
```

## 上一轮 P2 复核

### P2-1：最近消息时间语义 —— 已关闭

`sessions.last_message_at` 已与 `updated_at` 分离；标题修改和活动叶子切换不再改变最近消息时间，列表改按 `last_message_at, session_id` 确定性排序。旧树形数据库启动时会补列并回填旧值。

### P2-2：MAX_TURNS 范围 —— 不再作为本轮问题

Fixer 交接明确记录 `MAX_TURNS=50` 来自用户先前的独立要求，因此不按 Session/Git 计划回退。Reviewer 不再把该项作为范围偏差。

### P2-3：自动化验收矩阵 —— 部分改善，仍需补齐

Session/Git 专项从 8 个增加到 12 个单元测试，并补充了跨 Session、失败重试、普通问答不关联、有效结果 Commit 和 Git 零写入覆盖；但两个新 P1 均未被测试发现，CLI 集成测试仍只覆盖 new/list/delete，缺少 history/continue-from/commit/switch 和强制边界提示验证。

## 新非阻断观察

### P2-1：SessionService 通过私有对象链读取审计仓储

位置：`src/likai_nexus/memory/session.py:256-263`

`self.agent.executor.audit.repository` 把 Session 领域服务绑定到 AgentLoop、ToolExecutor、AuditLifecycle 的内部形态；当前结构可运行，但实现替换或审计故障会静默变成不合格或直接抛异常。建议把“Task 是否有成功代码修改”收敛为显式注入的稳定查询端口，而不是跨四层取私有属性。

## 达成矩阵

| 计划能力 | 当前判断 | 说明 |
|---|---|---|
| Session 树、活动路径与旧分支保留 | 通过 | 当前路径和兄弟分支隔离正常 |
| 失败/取消 Message↔Task 追踪 | 部分通过 | 正常失败/重试已修复；重复 Task ID 会制造矛盾关联 |
| 跨 Session 操作保护 | 通过 | 先校验 Session 归属再更新叶子 |
| 标题生成与失败隔离 | 通过 | 首轮成功后串行生成，失败保留默认标题 |
| 最近消息时间与列表排序 | 通过 | 独立 `last_message_at` |
| Git 索引、工作树和引用零写入 | 通过 | Reviewer 独立复现无变化 |
| Commit 关联资格 | 通过 | 普通对话、相同 HEAD、dirty 和无写入均不关联 |
| Commit 查询 Task/SHA/边界提示 | 通过 | CLI 已实现，仍缺集成测试 |
| Commit 失败不影响成功结果 | 不通过 | 版本原因或资格查询异常会向外传播 |
| 自动化验收覆盖 | 部分通过 | 178 项全量通过，但两个新 P1 未覆盖 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_session_tree_and_git.py tests/integration/test_session_cli.py -q -rs
结果：13 passed（3.41s）

.\.venv\Scripts\python.exe -m pytest -q -rs
结果：178 passed，2 skipped（27.38s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\python.exe -m ruff check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示
```

所有独立复现只使用自动清理的临时目录、临时 SQLite、本地临时 Git 仓库、Fake Backend 和虚构请求；没有读取 `.env`、真实凭据、网络或用户数据。

## 复审门槛

1. 修复重复 Task ID 的 Message 关联，确保 Message 状态与其关联 Task 终态一致。
2. 把完整版本附加链路改为旁路失败，任何版本能力故障都不得让成功问答抛错。
3. 增加两个 P1 的故障注入测试，并补 CLI history/continue-from/commit/switch 集成覆盖。
4. 重新运行专项与全量 `pytest`、`ruff check .`、`python -m compileall src`、`git diff --check`。

## 最终意见

**CHANGES_REQUIRED**

上一轮修复方向正确，5 个旧 P1 已关闭；当前只剩两个新 P1。关闭重复 Task 关联和版本附加故障隔离后再进行第三轮复审。
