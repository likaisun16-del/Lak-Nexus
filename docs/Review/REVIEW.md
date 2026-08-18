# 下一阶段代码第四次复审报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-18
- 对应计划：`docs/Planner/NEXT_PHASE_OBSERVABILITY_REVIEW_MODES_TOOL_EXTENSIBILITY_PLAN.md`
- 审查基线：`d0724ed`（`main` / `origin/main`）加当前未提交、未跟踪实现
- 实现交接：`docs/Implement/IMPLEMENTATION_NOTES.md` 中“Review-3 P1 修复记录”
- 用例附件：[`REVIEW_NEXT_PHASE_USE_CASE.svg`](./REVIEW_NEXT_PHASE_USE_CASE.svg) / [`REVIEW_NEXT_PHASE_USE_CASE.png`](./REVIEW_NEXT_PHASE_USE_CASE.png)
- Reviewer 边界：仅修改 `docs/Review/` 内的报告和图示，没有修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查结论

- [x] PASS
- [ ] CHANGES_REQUIRED

上轮 P1 已关闭。审计启动失败和审批审计写入失败时，模型提供的原始工具名、调用 ID 不再进入任务返回、结构化事件或 SQLite；错误仍保留固定标签和稳定哈希，能够定位并关联故障。最新版没有发现新的 P0/P1，满足本阶段计划的核心功能、安全和扩展性验收，可以 PASS。

## 上轮 P1 复核

| 检查项 | 最新实现 | 复审结果 |
|---|---|---|
| 审批审计失败中的调用 ID | 使用 `call:<hash>`，不拼接模型原文 | 已关闭 |
| Agent Loop 工具异常中的工具名 | 已注册工具使用 Registry 规范名；未知工具使用 `unknown-tool:<hash>` | 已关闭 |
| 审计启动失败 | 工具名和调用 ID 均使用安全标签 | 已关闭 |
| 最终错误出口 | AgentResult、任务表、事件和 CLI 均沿用安全错误 | 已关闭 |
| 可诊断性 | 安全标签稳定，任务仍进入 `failed`，工具审计尽力补写终态 | 保持 |

## 代码核对

- `ToolExecutor._record_approval()` 在仓储异常中对调用 ID 使用 `safe_audit_identifier()`，工具名统一经过 `safe_tool_label()`（`src/likai_nexus/executor/service.py:301-306`）。
- `safe_tool_label()` 只保留 Registry 已确认的规范工具名，未知名称使用固定标签和哈希（`src/likai_nexus/executor/service.py:454-465`）。
- Agent Loop 的工具异常包装不再引用原始 `tool_call.name`，而是使用执行器提供的安全标签（`src/likai_nexus/orchestrator/agent_loop.py:205-210`）。
- 新增测试分别注入审计启动失败与审批审计写入失败，并扫描 AgentResult、任务表、工具/审批审计和结构化事件（`tests/unit/test_next_phase.py:267-339`）。

## 独立复现

Reviewer 使用临时目录、临时 SQLite、Fake Backend 和故障仓储重新执行了两条路径：

1. 未知工具名为虚构 `tool_live_…`、调用 ID 为虚构 `sk_live_…`，强制 `start_tool_call()` 抛出 `OSError`。
2. 已注册 `read` 工具使用同类虚构调用 ID，强制 `record_approval()` 抛出 `OSError`。

两条路径均得到：

- 任务状态为 `failed`。
- AgentResult、SQLite 任务/工具/审批记录和结构化事件中均不存在原始哨兵。
- 启动故障错误保留 `unknown-tool:<hash>` 与 `call:<hash>`。
- 审批故障错误保留 `call:<hash>`。

## 问题列表

### P0

暂无。

### P1

暂无。上轮审计故障信息泄露问题已关闭。

### P2（非阻断）

#### P2-1：无标签凭据格式覆盖仍可补强

- 当前无标签规则覆盖 OpenAI `sk-`、GitHub token 和 JWT；虚构 `sk_live_…`、`xoxb-…`、`glpat-…` 外形仍不会被通用文本正则识别。
- 不可信工具名和调用 ID 已不再依赖该正则，因此不影响本轮 P1 关闭。
- 建议后续扩充明确支持的常见格式，并对无需自由文本的 metadata 使用枚举、布尔、数值或工具显式安全字段。

#### P2-2：高风险系统边界仍缺少不可跳过门禁

- full-access 取消测试仍只验证直接进程，没有验证后台孙进程或完整进程树终止。
- 2 项符号链接安全测试在当前 Windows 环境因权限跳过，应由 Linux CI 或启用 Developer Mode 的 Windows job 提供不可跳过门禁。
- `CliApprovalHandler` 的 EOF/输入中断仍无专项测试；当前由 CLI 兜底安全退出。

这些事项保持非阻断，不改变本轮 PASS，但应在后续安全加固或 CI 建设中跟踪。

## 计划实现情况

| 计划能力 | 当前状态 | 复审判断 |
|---|---|---|
| CLI 实时过程展示与关闭开关 | 已实现 | 通过 |
| strict / relaxed / full-access | 已实现 | 通过 |
| full-access 文件与 Bash | 已实现主体 | 通过；进程树门禁列为 P2 |
| 任务模式、审批来源与旧库迁移 | 已实现 | 通过 |
| Tool 动态注册与扩展摘要契约 | 已实现 | 通过 |
| 敏感信息不进入审计和错误 | 已实现核心边界 | 通过；格式扩充列为 P2 |
| 审计故障安全失败 | 已实现 | 通过 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest -q -rs
结果：128 passed，2 skipped（9.19s）
跳过项：当前 Windows 权限不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示

git check-ignore -v --no-index .env
结果：命中 `.gitignore:1:.env`；`.env` 未被 Git 跟踪

Reviewer 用例图渲染与 --check
结果：SVG/PNG 已更新；0 errors，0 warnings，0 overlap，0 overflow；人工目视检查通过
```

独立复现没有读取项目 `.env`，没有使用真实凭据、网络或真实用户目录。

## 最终意见

**PASS**

上轮阻断问题已按“默认不信任模型字段”的原则修复，并有正常路径、故障路径和全量回归证据支撑。本阶段实现可以通过 Reviewer 审查。
