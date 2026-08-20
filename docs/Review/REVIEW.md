# Session 树形会话与 Git Commit 关联第三轮复审报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-19
- 计划文档：`docs/Planner/SESSION_TREE_GIT_IMPLEMENTATION_PLAN.md`
- 审查基线：`dd6dda63413485d82a0e8963570ece91b0844eb1` 加当前未提交 Fixer 改动
- 上一轮归档：[`REVIEW_SESSION_TREE_GIT_ROUND_2.md`](./REVIEW_SESSION_TREE_GIT_ROUND_2.md)
- 用例附件：[`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_3.svg`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_3.svg) / [`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_3.png`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_3.png)
- Reviewer 边界：只更新 `docs/Review/`，未修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查结论

- [x] PASS
- [ ] CHANGES_REQUIRED

上一轮报告提出的两个 P1 已关闭：重复 `task_id` 会在创建可见 user Message 前被拒绝，版本附加链路已统一降级隔离。旧版 5 个 P1 也已在上一轮复核中关闭。本轮没有发现新的阻断性问题，Session 树与 Git 版本关联计划达到当前验收范围。

## 本轮复核

### P1-1：重复 Task ID 的 Message 关联 —— 已关闭

`SessionService.ask()` 在写入正常 user Message 前调用 TaskStore 的 `get()` 预检。发现已有 Task 时只保存一个没有 `task_id` 的 `rejected` user Message，并抛出“本次请求未创建新 Task”；不再把本次请求绑定到旧的 success、failed 或 cancelled Task。

AgentLoop 边界仍保留 `TaskAlreadyExistsError` 兜底：并发窗口内若创建 Task 失败，pending Message 会回填为 `rejected` 且保持无 Task 关联。

回归覆盖：旧 Task 为 success、failed、cancelled 三种终态时，重复请求均保持旧 Task 终态不变，新 Message 的 `task_id` 为空、`execution_status=rejected`。

### P1-2：版本附加能力故障隔离 —— 已关闭

assistant Message 和成功 Task 保存后，Git 基线读取、审计资格查询、结束快照读取、CommitRepository 保存、版本原因写入均位于旁路降级边界内。任一环节异常只返回安全的“未记录版本”原因，不会让成功的 `ask()` 抛错，也不会覆盖 Task、Message 或会话分支结果。

回归覆盖：基线读取故障、审计资格查询故障、Commit 保存故障和版本原因落库故障四类注入场景。

## 既有问题闭环

| 问题 | 当前结果 | 复核证据 |
|---|---|---|
| Git 查询刷新索引 | 已关闭 | `--no-optional-locks` 与 `GIT_OPTIONAL_LOCKS=0` 同时启用；索引字节、mtime、工作树和 refs 前后不变 |
| 普通 Task 误绑既有 HEAD | 已关闭 | 仅成功 write/edit 审计、可比较且变化的前后 HEAD、结束干净工作区才允许关联 |
| 失败/取消消息不可追踪 | 已关闭 | user Message 从 pending 回填 Task 与终态；失败重试保存来源消息 ID |
| 跨 Session continue-from | 已关闭 | 先校验当前 Session 归属，再更新活动叶子；跨 Session 拒绝且两边叶子不变 |
| 查询缺 Task 与边界提示 | 已关闭 | history/commit 输出 Task、完整 SHA 或安全原因，并固定展示已提交内容边界 |
| 最近消息时间语义 | 已关闭 | `last_message_at` 与 `updated_at` 分离；标题和分支指针更新不冒充新消息 |
| 重复 Task ID 错误关联 | 已关闭 | 三种旧 Task 终态回归测试通过 |
| 版本附加异常阻断成功结果 | 已关闭 | 四类故障注入测试通过 |

## 非阻断观察

### P2-1：CommitRepository.record() 未在仓储边界校验 Task 终态

位置：[`src/likai_nexus/storage/commit_repository.py:22`](../../src/likai_nexus/storage/commit_repository.py:22)

当前 `record()` 只校验 Task 存在和 SHA 格式，不校验 Task 是否为 `success`。生产调用路径由 `SessionService` 在成功 AgentResult 且资格检查通过后调用，因此本轮没有形成可由 CLI 触发的阻断路径；但直接调用仓储仍可给 pending、failed 或 cancelled Task 写入 Commit 关联，破坏计划第 150、152 行的存储不变量。

建议在仓储边界要求 Task 状态为 `success`，或收紧该方法的可见范围，并把 CLI 集成测试中的手工 Task 置为 success 后再记录 Commit。

### P2-2：SessionService 通过私有对象链读取审计仓储

位置：[`src/likai_nexus/memory/session.py:270`](../../src/likai_nexus/memory/session.py:270)

`self.agent.executor.audit.repository` 将 Session 领域服务绑定到 AgentLoop、ToolExecutor 和 AuditLifecycle 的内部结构。当前异常被安全降级，功能没有阻断；后续可注入显式的“Task 成功代码修改资格”查询端口，降低替换 AgentLoop 或审计实现时的耦合。

### P2-3：CLI 矩阵仍可补充未记录版本的展示

本轮 CLI 集成测试覆盖了 history、continue-from、commit、switch 的成功关联、边界提示和跨 Session 拒绝。未记录版本、失败消息和取消消息的完整终端文本仍主要由单元/服务测试覆盖，建议后续补一条 CLI 端到端展示测试，但不影响当前计划验收。

## 达成矩阵

| 计划能力 | 当前判断 | 说明 |
|---|---|---|
| Session 树、活动路径与旧分支保留 | 通过 | 当前路径和兄弟分支隔离正常 |
| 失败/取消 Message↔Task 追踪 | 通过 | pending 回填、重复拒绝和失败重试关系均有覆盖 |
| 跨 Session 操作保护 | 通过 | 先校验 Session 归属再更新活动叶子 |
| 标题生成与失败隔离 | 通过 | 首次成功后串行生成，失败保留默认标题 |
| 最近消息时间与列表排序 | 通过 | `last_message_at` 独立维护 |
| Git 索引、工作树和引用零写入 | 通过 | 临时仓库独立复核，索引和 refs 均无变化 |
| Commit 关联资格 | 通过 | 普通对话、相同 HEAD、dirty 和无写入均不关联 |
| Commit 查询 Task/SHA/边界提示 | 通过 | CLI 已实现并有集成覆盖 |
| Commit 失败不影响成功结果 | 通过 | 四类故障注入均返回成功结果 |
| 自动化验收覆盖 | 通过（仍可扩展） | Session/Git 专项 21 项，全量门禁通过；P2 测试缺口不阻塞 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_session_tree_and_git.py tests/integration/test_session_cli.py -q -rs
结果：21 passed（4.38s）

.\.venv\Scripts\python.exe -m pytest -q -rs
结果：186 passed，2 skipped（29.95s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示
```

独立验证使用临时 SQLite、临时 Git 仓库和虚构请求；没有读取 `.env`、真实凭据、网络或用户数据。

## 交接建议

1. Fixer 可在后续维护中补充 P2-1 的 Task 终态边界校验。
2. 若继续扩展 CLI 覆盖，优先增加未记录版本、失败和取消消息的终端断言。
3. 本计划当前无需继续阻塞，可以进入提交前审查或下一项需求。

## 最终意见

**PASS**

两个新增 P1 已关闭，上一轮阻断问题全部通过复核；Session 树与 Git Commit 关联计划在当前代码和验收范围内达成。
