# 模型轮次、Bash 指令与执行结果可见性 Review-6 复审报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-18
- 对应计划：`docs/Planner/BASH_COMMAND_RESULT_VISIBILITY_PLAN.md`
- 审查基线：`87c1f8c`（`main`）加当前未提交、未跟踪实现
- 实现交接：`docs/Implement/IMPLEMENTATION_NOTES.md` 中“Review-6 一个 P1 修复记录”
- 用例附件：[`REVIEW_BASH_COMMAND_RESULT_VISIBILITY_USE_CASE.svg`](./REVIEW_BASH_COMMAND_RESULT_VISIBILITY_USE_CASE.svg) / [`REVIEW_BASH_COMMAND_RESULT_VISIBILITY_USE_CASE.png`](./REVIEW_BASH_COMMAND_RESULT_VISIBILITY_USE_CASE.png)
- 审查口径：当前是单用户、本地 CLI、以学习为目的的 MVP；不以第三方恶意 Tool 的无限对抗或完整 UI 组合矩阵阻塞主线，接入远程渠道前再集中加固
- Reviewer 边界：只更新 `docs/Review/` 内报告和图示，没有修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查结论

- [x] PASS
- [ ] CHANGES_REQUIRED

Review-6 已修复最后一个阻断问题：长审批摘要现在会在 4096 字节预算内明确显示“审批摘要已截断”，`--no-progress` 下也能直接看到该提示；原始命令、审批指纹、确认 token 比较和实际执行参数均保持不变。

模型轮次、Bash 实际指令、终态、退出码、耗时、stdout/stderr、无输出、超时/取消、独立展示预算、控制字符清理、凭据脱敏、展示故障隔离、SQLite 摘要隔离及 `--no-progress` 主体链路均已实现。按当前学习型 MVP 口径，没有 P0/P1 阻断问题。

## 本轮复核结果

### 上一轮 P1：长审批摘要静默截断 —— 已关闭

位置：`src/likai_nexus/safety/approval.py:33-65`

`_safe_prompt_field()` 现在保留 `truncate_text()` 的截断状态，并为审批摘要预留固定标记预算。发生截断时返回“有限正文 + 审批摘要已截断”，不会再次越过 4096 字节字段上限。

Reviewer 使用 relaxed 模式的虚构长 Bash 命令独立复现，只构造审批请求，没有执行命令：

```text
APPROVED=False
TAIL_VISIBLE=False
TRUNCATION_VISIBLE=True
PROMPT_BYTES=4141
REQUEST_UNCHANGED=True
```

结果表明：用户明确知道摘要被截断，审批请求中的原始完整命令仍保持不变；展示清理没有改变审批绑定或实际执行对象。

### 既有 P1 回归 —— 均保持关闭

- 调用展示直接抛错、异常字符串对象或超深结构不会阻止工具执行，成功结果和审计终态保持 `success`。
- 结果展示的直接异常、循环、异常字符串对象和超深结构不会改变成功 ToolResult。
- 审批提示中的 ANSI、CR/LF/CRLF、伪造过程前缀和凭据在进入 `input()` 前已安全处理。
- 模型失败原因已折叠为有限单行，不能伪造新的 `[工具]`、`[模型]` 或 `[任务]` 顶层过程行。

## 非阻断观察

### P2-1：`tool_started` 早于参数校验和安全检查

位置：`src/likai_nexus/executor/service.py:70-79`、`src/likai_nexus/executor/service.py:125-130`

无效或被策略拒绝的 command 仍会先显示成“执行指令”，与计划“显示通过校验的 command”不完全一致。该问题只影响过程文案时机，不会绕过校验、审批或安全策略。当前不阻塞学习主线；以后调整执行生命周期时可移动到安全检查完成后、实际进程启动前。

### P2-2：真实终态到 CLI 的组合测试可继续补充

底层 Bash 已覆盖非零退出、真实超时和取消；结构化 Event/CLI 已覆盖成功 stdout+stderr 及合成超时/无输出。stderr-only、非零退出、真实超时和真实取消尚未全部做端到端组合。当前已有正常、失败、安全和故障隔离测试，建议在接入飞书、微信等远程渠道前补齐，不阻塞本轮 PASS。

## 计划实现矩阵

| 计划能力 | 当前实现 | 复审判断 |
|---|---|---|
| 模型任务内 `N/max_turns` | 结构化轮次字段；普通完成不重复显示；失败原因有限单行 | 已实现 |
| Bash 实际指令与 timeout | 通用 invocation 投影显示真实 command、非默认 timeout 和可见截断标记 | 已实现主体；事件时机留非阻断 P2 |
| Bash 终态和结果预览 | 区分成功、失败、超时、取消，显示退出码、耗时、截断状态、stdout/stderr 和无输出 | 已实现主体 |
| 调用/结果展示故障隔离 | 整条展示流水线异常时安全降级，不改变执行和审计语义 | 已实现 |
| 终端清理与脱敏 | RuntimeEvent、CLI、模型失败原因和审批提示均已覆盖 | 已实现 |
| 长内容用户知情 | 指令、结果和审批摘要都有有限预算与可见截断标记 | 已实现 |
| 模型/UI/审计三投影分离 | SQLite 仅保存 hash、状态和摘要，不保存原命令或完整输出 | 已实现 |
| `--no-progress` 与噪音过滤 | 关闭过程事件但保留安全审批；普通完成和内部安全事件不展示 | 已实现 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests/integration/test_cli_agent.py::test_cli_approval_prompt_sanitizes_untrusted_fields tests/unit/test_next_phase.py::test_display_arguments_failure_cannot_prevent_successful_tool tests/unit/test_next_phase.py::test_display_projection_failure_cannot_change_successful_tool -q -rs
结果：8 passed

.\.venv\Scripts\python.exe -m pytest -q -rs
结果：156 passed，2 skipped（22.23s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示

Reviewer 独立复现
长 Bash 审批摘要显示固定截断标记，原始审批请求保持不变，没有执行虚构命令

Reviewer 用例图
SVG/PNG 渲染成功；--check 为 0 errors、0 warnings、0 overflow、0 overlap；人工目视检查通过
```

独立复现使用虚构命令，没有读取项目 `.env`、真实凭据、网络或用户数据。

## 最终意见

**PASS**

当前实现已满足学习型本地 CLI 的主线目标，可以继续进入下一阶段。两个 P2 记录为接入远程渠道前的加固项，不要求现在继续围绕低收益边界反复开发。
