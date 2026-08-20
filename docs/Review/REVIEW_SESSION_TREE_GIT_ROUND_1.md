# Session 树形会话与 Git Commit 关联首轮审查报告（归档）

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-19
- 计划文档：`docs/Planner/SESSION_TREE_GIT_IMPLEMENTATION_PLAN.md`
- 审查基线：`dd6dda63413485d82a0e8963570ece91b0844eb1`（`main`）
- 实现提交：`dd6dda6 实现 Session 树与 Git 版本关联`
- 上一份主报告归档：[`REVIEW_ARCHITECTURE_OPTIMIZATION_ROUND_2.md`](./REVIEW_ARCHITECTURE_OPTIMIZATION_ROUND_2.md)
- 用例附件：[`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_1.svg`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_1.svg) / [`REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_1.png`](./REVIEW_SESSION_TREE_GIT_USE_CASE_ROUND_1.png)
- Reviewer 边界：只更新 `docs/Review/`，未修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

Session、可见消息树、分支上下文、标题、删除保留审计、完整 SHA 校验和脏工作区拒绝等主体能力已经落地；但 Git 零写入、Commit 关联资格、失败 Task 追踪、跨 Session 继续保护和版本查询安全提示仍违反计划中的“必须”契约，因此当前只能判定为部分达成。

## 阻断问题

### P1-1：Git“只读”检查会刷新并改写索引

位置：`src/likai_nexus/git.py:44-58`

`GitReadOnly._run_readonly()` 直接调用 `git status`，没有设置 `GIT_OPTIONAL_LOCKS=0`。`git status` 可以刷新索引 stat cache；命令名称虽然是查询，`.git/index` 仍可能被写入。这违反计划第 205 行“不得执行任何会改变索引”的安全底线，也没有满足第 265 行要求的索引、工作树和引用前后不变验证。

Reviewer 在临时仓库提交文件后只改变文件 mtime，再调用 `read_clean_commit()`：返回了有效 Commit，但 `.git/index` 的 SHA-256 和 mtime 均发生变化。

修复要求：Git 子进程必须显式禁用可选锁/索引刷新，并新增对索引字节、工作树内容和引用前后不变的自动化测试；不能只断言命令名位于允许列表。

### P1-2：所有干净仓库中的成功 Task 都会误绑既有 HEAD

位置：`src/likai_nexus/memory/session.py:160-169`、`src/likai_nexus/memory/session.py:185-201`

`ask()` 对每个成功 Task 无条件调用 `_try_record_commit()`；后者只检查任务结束时仓库是否干净，不判断 Task 是否为代码/Git Task，也不证明当前 HEAD 能代表该 Task 的结果。普通纯问答会绑定任务开始前已经存在的 Commit。

Reviewer 使用“请解释一下什么是递归”的无工具纯问答在干净临时仓库复现：`plain_chat_commit_recorded=True`。现有测试 `test_successful_session_records_clean_git_commit_for_assistant` 也在没有任何代码或 Git Tool 行为时要求记录初始 Commit，和计划第 20、90、249 行的资格契约相反。

修复要求：建立显式、可审计的版本关联资格和结果版本判定；普通对话、非 Git Task、没有结果 Commit 或无法证明 HEAD 代表结果的 Task 必须返回“未记录版本”。

### P1-3：失败或取消的 user Message 无法追溯 Task 或重试状态

位置：`src/likai_nexus/memory/session.py:145-180`

user Message 在 Task 执行前写入且没有 `task_id`；只有成功路径创建的 assistant Message 才关联 Task。Task 失败或取消时，历史中只剩一条没有 Task、执行状态或重试关系的 user Message，无法从消息树稳定追溯对应执行事实。

Reviewer 独立复现结果：Task 为 `failed`，但 user Message 的 `task_id=None`，也不存在执行状态或重试字段。现有失败测试只断言保留了 user 角色，没有验证计划第 127 行要求的状态/重试展示。

修复要求：为失败、取消和成功路径建立稳定的 Message↔Task/执行尝试关联，历史展示必须能明确显示状态或可重试关系；不得为失败伪造 assistant 最终消息。

### P1-4：continue-from 会跨 Session 改写活动叶子并切换当前会话

位置：`src/likai_nexus/memory/session.py:95-102`、`src/likai_nexus/channels/cli.py:117-120`

`continue_from()` 只接收 Message ID，并从该消息反查 Session；CLI 随后直接把反查出的 Session 保存为当前活动会话。当前活动 Session 为 A 时，传入 Session B 的历史消息不会被拒绝，而是改写 B 的活动叶子并把当前会话切换到 B。

Reviewer 在两个临时 Session 中复现：操作前活动会话为 A，传入 B 的旧消息后返回 Session B、B 的活动叶子被改变、活动偏好也变为 B。这违反计划第 133-142 行“当前 Session”与“跨 Session Message 必须拒绝且不能改变活动叶子”的契约。

修复要求：continue-from 必须携带或解析调用方当前 Session，并在同一领域操作内校验 Message 归属；跨 Session 时不得修改任一活动叶子或活动会话偏好。

### P1-5：Commit 查询缺少 Task 标识和必需的恢复边界提示

位置：`src/likai_nexus/channels/cli.py:122-131`、`src/likai_nexus/channels/cli.py:170-179`

Commit 关联记录本身包含 `task_id`，但 `session commit` 只输出 Message ID 和 SHA；历史输出也没有在版本行建立 Task+SHA 的明确关联。同时，所有用户可见查询都没有说明 SHA 只覆盖已提交内容，不代表数据库、网络、系统或外部副作用已撤销。

这不满足计划第 155 行“返回关联 Task 和 Commit SHA”以及第 209、252 行的强制安全提示。`GitCommitSnapshot.reason` 还会在 Session 层被丢弃，用户只能看到泛化的“未记录版本”。

修复要求：查询结果同时展示 Task ID、完整 SHA 和强制边界提示；无记录时保留安全的可定位原因，但不得泄露仓库凭据或敏感正文。

## 非阻断问题

### P2-1：`updated_at` 不是严格的“最近消息时间”

位置：`src/likai_nexus/storage/session_repository.py:59-67`、`src/likai_nexus/storage/session_repository.py:186-198`

修改标题和切换活动叶子都会更新 `updated_at`，CLI 却把它展示为“最近消息”。单纯查看旧分支会改变会话列表排序，和计划第 98、180 行的字段语义不一致。建议拆分最近消息时间与最近会话活动时间，或至少让展示名称与实际语义一致。

### P2-2：实现提交夹带未在计划中的最大轮次变更

位置：`.env.example:34`、`src/likai_nexus/config.py:87`、`src/likai_nexus/config.py:189`

同一提交把 `MAX_TURNS` 默认值从 20 提高到 50，扩大了模型调用次数、耗时和成本边界，但 Session/Git 计划没有授权该行为变化。建议独立说明来源并拆分提交；若没有单独需求，应恢复原值，避免把无关运行语义混入本计划。

### P2-3：自动化验收矩阵明显少于计划要求

位置：`tests/unit/test_session_tree_and_git.py`、`tests/integration/test_session_cli.py`

现有 8 个专项单元测试和 1 个 CLI 集成测试没有覆盖：重启读取与列表排序、会话切换、活动叶子跨 Session、取消路径、标题只生成一次、非 Git Task 不关联、Commit 失败诊断、Task+SHA 查询、恢复边界提示，以及索引/工作树/引用前后不变。P1-1 至 P1-4 均是测试通过但独立复现失败的例子。

## 达成矩阵

| 计划能力 | 当前判断 | 主要证据 |
|---|---|---|
| Session 与树形 Message 持久化 | 基本通过 | 自引用消息、活动叶子、可见角色和迁移已实现 |
| 当前路径与兄弟分支隔离 | 通过 | `SessionService.ask()` 先解析活动路径，专项测试验证兄弟消息不进入模型 |
| 从历史节点分支并保留旧分支 | 部分通过 | 同 Session 分支正常；CLI 跨 Session 保护缺失 |
| 首轮标题生成与失败隔离 | 基本通过 | 成功回答后串行调用 Backend；失败保留默认标题 |
| 删除 Session、保留执行审计 | 通过 | 事务级联消息，Task 和 Commit 保留 |
| 完整 SHA 与脏工作区拒绝 | 通过 | 40/64 位校验，dirty status 不记录 |
| Git 零写入 | 不通过 | 独立复现 `.git/index` 被刷新 |
| 仅合格代码 Task 关联 Commit | 不通过 | 普通纯问答也绑定已有 HEAD |
| 失败/取消消息可追踪 | 不通过 | user Message 无 Task/状态/重试关联 |
| 查询返回 Task、SHA 与边界提示 | 不通过 | CLI 只打印 SHA，未显示强制限制说明 |
| 自动化验收覆盖 | 不通过 | 多项必须契约未测试且已有反例 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest -q -rs
结果：174 passed，2 skipped（26.35s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\python.exe -m ruff check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过

Reviewer 独立复现：Git 索引零写入
commit_available=True
index_content_changed=True
index_mtime_changed=True

Reviewer 独立复现：Commit 资格与失败追踪
plain_chat_commit_recorded=True
failed_task_status=failed
failed_user_message_task_id=None
failed_user_message_has_retry_state=False

Reviewer 独立复现：跨 Session continue-from
操作前活动 Session=A；传入 B 的历史消息后返回 B
活动偏好变为 B，且 B 的活动叶子被改写
```

所有独立复现只使用自动清理的临时目录、临时 SQLite、本地临时 Git 仓库和虚构请求；没有读取 `.env`、真实凭据、网络或用户数据。

## 复审门槛

1. 关闭 P1-1 至 P1-5，并为每项增加可重复自动化测试。
2. Git 测试必须比较索引字节、工作树内容和引用前后状态，不能只检查命令名称。
3. 增加非 Git、无结果 Commit、dirty、Git 失败和有效结果 Commit 的资格矩阵。
4. 增加失败/取消 Message↔Task 追踪、同/跨 Session continue-from 和用户查询输出测试。
5. 重新运行全量 `pytest`、`ruff check .`、`python -m compileall src` 和 `git diff --check`。

## 最终意见

**CHANGES_REQUIRED**

当前实现完成了 Session 树的主体结构，但 Git 安全边界和关键可追踪契约尚未达到计划。修复 P1 并补齐验收矩阵后再复审。
