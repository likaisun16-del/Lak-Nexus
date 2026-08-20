# 架构优化已确认方案第二轮复审报告（归档）

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-19
- 对应文档：`docs/Planner/ARCHITECTURE_OPTIMIZATION_DISCUSSION.md`
- 审查基线：`3bbad75`（`main`）加当前未提交、未跟踪实现及 Fixer 修复
- 上一轮归档：[`REVIEW_ARCHITECTURE_OPTIMIZATION_ROUND_1.md`](./REVIEW_ARCHITECTURE_OPTIMIZATION_ROUND_1.md)
- 实现交接：`docs/Implement/IMPLEMENTATION_NOTES.md` 中“架构优化讨论方案执行记录”及本轮 P1 修复记录
- 用例附件：[`REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE_ROUND_2.svg`](./REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE_ROUND_2.svg) / [`REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE_ROUND_2.png`](./REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE_ROUND_2.png)
- Reviewer 边界：只更新 `docs/Review/` 内报告、归档和图示，没有修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查口径

本轮继续只核对讨论文档中的两组“已确认”方案和上一轮两个 P1；条件触发候选 E/F/G 不属于当前实施范围。

## 审查结论

- [x] PASS
- [ ] CHANGES_REQUIRED

上一轮两个 P1 均已关闭。最新版没有发现新的 P0/P1：结构化 Tool 终态已经成为默认摘要、降级摘要、内置工具、事件和 SQLite 的统一状态来源；原 1167 行综合测试已按稳定职责拆分，原有 33 个回归测试全部保留，Tool 契约矩阵补齐了取消、异常和跨投影终态一致性。

## 上一轮 P1 复核

### P1-1：结构化终态与审计摘要状态不一致 —— 已关闭

代码证据：

- `ToolStatus` 提供统一稳定标签（`src/likai_nexus/tools/contracts.py:28-53`）。
- Tool 默认审计摘要只读取 `effective_status()`（`src/likai_nexus/tools/base.py:105-110`）。
- 审计摘要生成失败的降级分支同样读取 `effective_status()`（`src/likai_nexus/executor/projection.py:130-138`）。
- Bash、read、write、edit 的覆盖摘要均改用相同状态来源。
- 契约测试断言 TIMEOUT/CANCELLED 在 ToolResult、RuntimeEvent、审计投影和 SQLite 中一致（`tests/unit/test_tool_contract.py:128-179`）。

Reviewer 独立复现两条路径：

```text
默认摘要路径：Result=timeout，Event=timeout，SQLite=timeout，摘要包含“超时”且不包含“成功”
摘要故障降级：Result=timeout，SQLite=timeout，降级摘要包含“超时”且不包含“成功”
```

两条路径都使用显式 `ToolStatus.TIMEOUT` 且保留旧 `is_error` 默认值，确认权威终态不再受兼容字段干扰。

### P1-2：测试职责拆分和 Tool 契约矩阵不完整 —— 已关闭

结构复核：

| 文件 | 当前规模 | 职责 |
|---|---:|---|
| `test_executor_and_projections.py` | 570 行，22 个测试定义 | Registry、Executor、审计安全、事件和投影 |
| `test_review_modes_and_migrations.py` | 372 行，11 个测试定义 | strict/relaxed/full-access、Runtime 和数据库迁移 |
| `test_support.py` | 202 行 | 两组测试共享的扩展 Tool、Sink 和组装支持 |
| `test_tool_contract.py` | 212 行，8 个测试定义 | 名称/规格、状态、取消、异常、展示、模型与审计契约 |

Reviewer 从 Git 基线提取原 `test_next_phase.py` 的 33 个测试名，与两个拆分测试文件逐项比较：数量均为 33，没有删除或改名的原有场景。契约文件新增 TIMEOUT、CANCELLED、执行异常和终态/审计一致性覆盖，全量测试数由上一轮 161 增加到 164。

## 非阻断观察

### P2-1：调用展示仍早于参数校验和安全检查

位置：`src/likai_nexus/executor/service.py:78-93`、`src/likai_nexus/executor/service.py:142-146`

`display_arguments()` 与 `tool_started` 仍在 `validate()`、`check_safety()` 之前发生。展示路径已有异常隔离、控制字符清理、脱敏和字节限制，实际执行仍严格经过校验、安全检查和审批，因此没有形成权限绕过或敏感信息阻断项。

该项沿用上一轮 P2：后续调整过程事件语义时，可移动到安全检查通过后、执行前；也可以在正式计划中明确调用展示不属于结果投影阶段。本项不阻塞当前本地 CLI 架构优化 PASS。

## 已确认方案复审矩阵

| 已确认能力 | 当前实现 | 复审判断 |
|---|---|---|
| `likai_nexus.tools` 一级扩展域 | 契约、上下文、Registry 和内置 Tool 已归位 | 通过 |
| 单一 Tool 执行上下文 | 内置工具共享能力和限制，不再各自解释模式 | 通过 |
| 结构化 Tool 结果 | status、model、display、audit 分离；终态来源一致 | 通过 |
| ToolExecutor 职责拆分 | 唯一门面保留，审计和投影职责已委托 | 通过 |
| 显式注册和无工具名分支 | Runtime 显式组装；Executor、Agent Loop、CLI 无具体 Tool 分支 | 通过 |
| 中立事件与通用展示 | 顶层事件契约及通用字段 Renderer 已实现 | 通过 |
| CLI 入口与渲染拆分 | `--no-progress`、审批和退出码兼容测试通过 | 通过 |
| 配置层纯净 | Settings 不触发 Storage；Runtime/Storage 各负其责 | 通过 |
| 测试拆分与契约集合 | 原场景无损拆分，必需契约矩阵已补齐 | 通过 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_tool_contract.py tests/unit/test_executor_and_projections.py tests/unit/test_review_modes_and_migrations.py -q -rs
结果：46 passed（5.02s）

.\.venv\Scripts\python.exe -m pytest -q -rs
结果：164 passed，2 skipped（20.61s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示

Reviewer 独立复现
默认摘要和摘要故障降级路径均保持 TIMEOUT/超时一致

测试拆分对比
原综合文件 33 个测试名与拆分后两个职责文件的 33 个测试名完全一致

Reviewer 用例图
SVG 通过 XML 解析；使用本机 Edge 以 1500×1080 渲染 PNG，人工目视确认无节点重叠、连线穿越或文字溢出
```

独立复现只使用自动清理的临时目录、临时 SQLite 和虚构 Tool，没有读取项目 `.env`、真实凭据、网络或用户数据。

## 最终意见

**PASS**

两个 P1 已按上一轮复审门槛实质关闭，架构优化的已确认范围已经实现，可以结束本轮 Fixer/Reviewer 循环。P2-1 作为后续过程事件语义优化项保留，不要求继续阻塞当前阶段。
