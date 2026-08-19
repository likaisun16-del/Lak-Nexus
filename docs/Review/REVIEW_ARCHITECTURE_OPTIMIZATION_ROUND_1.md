# 架构优化已确认方案代码审查报告

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-19
- 对应文档：`docs/Planner/ARCHITECTURE_OPTIMIZATION_DISCUSSION.md`
- 审查基线：`3bbad75`（`main`）加当前未提交、未跟踪实现
- 实现交接：`docs/Implement/IMPLEMENTATION_NOTES.md` 中“架构优化讨论方案执行记录”
- 用例附件：[`REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE.svg`](./REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE.svg) / [`REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE.png`](./REVIEW_ARCHITECTURE_OPTIMIZATION_USE_CASE.png)
- Reviewer 边界：只更新 `docs/Review/` 内报告和图示，没有修改业务代码、测试、配置、计划、实现交接或 `AGENTS.md`

## 审查口径

规划文档状态为 `DISCUSSION`，并明确说明它不是完整执行计划。因此本轮只核对两组“已确认”方案及其中标记为“必须保持”的职责边界；条件触发候选 E/F/G 不属于当前实施范围，不作为缺失项。

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

当前实现已经完成 Tool 一级域、执行上下文、公共契约归属、Executor 职责委托、通用展示、CLI 渲染拆分和配置层纯化等主体改造，但尚不能认定完整实现计划：

1. 结构化执行终态与默认审计投影仍可能互相矛盾。
2. 已确认的超大测试文件拆分和可复用 Tool 契约测试集合没有完成。

## 阻断问题

### P1-1：结构化终态与审计摘要仍使用两套状态来源

位置：

- `src/likai_nexus/tools/base.py:26-35`
- `src/likai_nexus/tools/base.py:104-110`
- `src/likai_nexus/executor/projection.py:130-138`
- `src/likai_nexus/executor/service.py:192-205`

`ToolOutput.effective_status()` 将显式 `ToolStatus` 作为权威终态，Executor 也按该终态写入工具审计状态；但 Tool 默认审计摘要和投影故障降级摘要仍根据兼容字段 `output.is_error` 判断成功或失败。

Reviewer 使用显式返回 `ToolStatus.TIMEOUT`、保持 `is_error` 默认值的虚构扩展 Tool 独立复现，结果为：

```text
ToolResult.status = timeout
ToolResult.is_error = true
ToolResult.audit.result_summary = “probe 成功：……”
SQLite tool_calls.status = timeout
SQLite tool_calls.result_summary = “probe 成功：……”
```

这会让同一条可审计记录同时表达“超时”和“成功”，违反“结构化 Tool 结果明确区分执行状态和审计投影”以及固定审计终态要求。现有契约测试恰好构造了同类 `TIMEOUT` 输出，但只断言 `ToolResult.status/is_error`，没有断言审计投影和持久化摘要的一致性。

修复门槛：

- 默认审计摘要、异常降级摘要及适用的内置覆盖实现统一以权威结构化终态判断结果，或在 ToolOutput 边界强制校验状态不变量。
- 增加非成功显式终态与 ToolResult、RuntimeEvent、审计投影、SQLite 终态一致的契约测试。

### P1-2：测试文件架构与 Tool 契约测试集合只完成了部分工作

位置：

- `tests/unit/test_runtime_review_modes.py:1-1167`
- `tests/unit/test_tool_contract.py:1-142`

计划要求按 Registry、Executor、展示、安全模式、Runtime 和数据库迁移等稳定职责拆分测试，并要求新 Tool 可复用的契约测试至少覆盖名称/规格、状态、取消、异常、展示隔离、模型投影和审计脱敏。

当前实际状态：

- 原 `test_next_phase.py` 主要改名为 `test_runtime_review_modes.py`；该文件仍有 1167 行、33 个测试，同时包含 Registry、审计故障、过程展示、投影隔离、审查模式、Runtime、full-access 和数据库迁移。
- 新增 `test_tool_contract.py` 只有 5 个测试，覆盖名称/规格、显式状态、展示隔离、模型投影和审计脱敏，但没有覆盖计划明确列出的取消与异常。
- 显式状态测试没有覆盖 P1-1 暴露的审计一致性。

因此“拆分超大测试文件并建立 Tool 契约测试”尚未达到已确认方案。修复不要求机械增加文件数量，但应把当前混合职责按稳定边界做最小拆分，并补齐可复用契约矩阵。

## 非阻断观察

### P2-1：调用展示仍早于参数校验和安全检查

位置：`src/likai_nexus/executor/service.py:78-93`、`src/likai_nexus/executor/service.py:142-146`

`display_arguments()` 和 `tool_started` 当前发生在 `validate()`、`check_safety()` 之前。展示流水线已有异常隔离和脱敏，未发现安全绕过；但这与文档列出的“查找、校验、安全检查、审批、执行、脱敏投影、审计终态”字面顺序仍不完全一致。

后续可以把调用展示移动到校验和安全检查完成之后、实际执行之前；如果计划中的“脱敏投影”仅指结果投影，则应在正式计划中澄清调用展示所处阶段。

## 已落实能力矩阵

| 已确认能力 | 当前实现证据 | 判断 |
|---|---|---|
| `likai_nexus.tools` 一级扩展域 | 契约、上下文、Registry 和内置工具已迁入一级包 | 已实现 |
| 单一 Tool 执行上下文 | 内置工具共享 `ToolExecutionContext`，模式判断收敛为能力属性 | 已实现主体 |
| 结构化 Tool 结果 | `ToolResult` 已分离 status、model、display、audit | 部分实现；被 P1-1 阻断 |
| ToolExecutor 稳定职责拆分 | 审计生命周期和投影分别委托给 `AuditLifecycle`、`ToolProjectionService` | 已实现主体 |
| 唯一门面与显式注册 | Agent Loop 只调用 ToolExecutor；Runtime 通过显式内置工具集合组装 | 已实现 |
| 工具无关展示协议 | Tool 产生通用字段，Console Renderer 不按 Tool 名称分支 | 已实现 |
| 公共契约按归属收敛 | Tool 类型归 `tools`，运行事件提升到 `likai_nexus.events` | 已实现 |
| CLI 入口与渲染拆分 | `cli.py` 与 `console_renderer.py` 已分离，兼容测试通过 | 已实现 |
| 配置层纯净 | Settings 不导入 Storage；目录和迁移由 Runtime/AppDataManager 执行 | 已实现 |
| 测试拆分和 Tool 契约集合 | 新增契约文件，但原综合文件仍超大且契约矩阵缺项 | 未完成 |

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests/unit/test_tool_contract.py tests/unit/test_runtime_review_modes.py tests/integration/test_cli_agent.py -q -rs
结果：62 passed

.\.venv\Scripts\python.exe -m pytest -q -rs
结果：161 passed，2 skipped（20.05s）
跳过项：当前 Windows 环境不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check
结果：通过；仅有 Windows LF/CRLF 转换提示

Reviewer 独立复现
显式 TIMEOUT ToolResult 与 SQLite status 均为 timeout，但默认审计摘要错误写为“成功”

Reviewer 用例图
SVG 通过 XML 解析；使用本机 Edge 以 1500×1080 渲染 PNG，人工目视确认无节点重叠、连线穿越或文字溢出
```

独立复现只使用自动清理的临时目录、临时 SQLite 和虚构 Tool，没有读取项目 `.env`、真实凭据、网络或用户数据。

## 复审门槛

1. 关闭 P1-1，使结构化终态、模型/界面可见状态、审计投影和持久化终态一致。
2. 关闭 P1-2，完成最小职责拆分并补齐取消、异常和审计一致性契约测试。
3. 重新运行专项测试、全量测试、Ruff、compileall 和 `git diff --check`。
4. Fixer 在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复范围与验证结果后请求 Reviewer 复审。

## 最终意见

**CHANGES_REQUIRED**

主体架构方向正确，无需推倒重来；当前缺口集中在结构化终态的审计一致性，以及已确认测试架构交付未完成。关闭两个 P1 后即可进入复审。
