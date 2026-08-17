# Implementer 实现交接记录

## 已实现范围

- 建立 Python 3.12 工程骨架和 `pyproject.toml`。
- 实现 `read`、`write`、`edit`、`bash` 四个工具。
- 实现工作区路径限制、符号链接逃逸检查、严格 Bash 命令策略和人工审批接口。
- 实现统一 `ToolExecutor`，保证工具调用先校验、安全检查、审批，再执行并写入脱敏审计。
- 实现 SQLite 任务、工具调用、审批记录和启动时 `running` 任务恢复。
- 实现可替换 `ModelBackend`、Fake Backend、OpenAI 兼容后端和最小 Agent Loop。
- 实现本地 CLI 入口及 Ctrl+C 取消处理。
- 增加运行时组装层，保持 CLI 和 Agent Loop 不直接耦合具体模型或 SQLite 实现。
- 配置入口自动读取当前工作目录 `.env`，并让进程环境变量覆盖文件配置。
- 补充安全、工具、存储、Agent Loop、模型协议和 CLI 集成测试。

## 实现取舍

- 真实模型后端使用 Python 标准库 `urllib` 调用 OpenAI Chat Completions 兼容接口，避免把供应商 SDK 类型带入编排层，也不增加运行时依赖。
- 审计只保存工具参数和结构化结果摘要，不保存 read 正文、edit diff 或 Bash 完整输出。
- Bash 使用允许列表和进程终止逻辑降低风险，但仍不等价于 Docker、Windows Sandbox 等 OS 级隔离。

## 验证记录

已完成 Python 3.12.10 和项目虚拟环境配置：

```text
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

已执行并通过：

```text
.\.venv\Scripts\python.exe -m pytest：39 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall src
```

其中 1 个跳过项是当前 Windows 权限不允许创建符号链接；其余 39 项测试全部通过。

## Review 修复记录

依据 `docs/Review/REVIEW.md` 的 `CHANGES_REQUIRED` 结论完成以下修复：

- Bash 命令只使用策略解析后的 argv，禁用 profile/rc、Shell 展开和敏感环境变量，并以并发有界流读取 stdout/stderr；补充变量、brace、glob、超时和运行中取消测试。
- 文件工具默认拒绝 `.env*`、私钥和凭据文件；任务请求、工具参数、审批记录和工具结果改为长度、路径、动作和 sha256 摘要，避免正文进入 SQLite。
- 工具审计改用内部 `audit_id`，并将供应商 `tool_call_id` 限制在任务内唯一；审计异常会补写失败终态并交给 Agent Loop 终结任务。
- `read` 增加“行号 + 行内字节偏移”游标，支持超长 ASCII 和多字节 UTF-8 行继续读取。
- write/edit 审批增加受限预览或 diff、内容摘要、目标状态摘要和审批指纹；执行前检测动作或目标状态漂移。
- 删除未接线配置开关，明确 `.env` 使用当前工作目录语义；模型阻塞 HTTP 的取消行为明确为 best-effort，并补充运行中取消验证。

本轮修改只涉及实现、测试、配置说明和实现交接文档；未读取或提交真实 `.env` 内容。

## 本轮复审前验证

```text
.\.venv\Scripts\python.exe -m pytest -q：55 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
git check-ignore -v --no-index .env：命中 .gitignore:1:.env，文件未被 Git 跟踪
```

## 第二轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第二轮 `CHANGES_REQUIRED` 结论，继续完成：

- 新增公开的 `WorkspaceAccessPolicy`，由文件工具和 Bash 的显式路径参数共享 `.env*`、凭据、私钥和敏感目录拒绝规则。
- Bash 审批界面仍展示脱敏后的完整命令，但持久化审批摘要只保存可执行文件、参数数量、超时、命令摘要哈希和审批指纹，不保存原始 argv。
- Windows Bash 自动发现优先 Git for Windows；显式或自动发现 WSL `bash.exe` 会被拒绝，运行时启动阶段会探测受控 Bash 和 Git 能力。
- read 对总正文预算、UTF-8 字节边界和非法游标进行校验；截断游标通过安全 metadata 传入模型。
- Bash 结果在单次预算内保留状态前缀和截断标记；ToolExecutor 将安全的截断、退出码和续读游标状态附加到模型工具消息。
- 增加跨工具敏感路径、Bash 审批哨兵、WSL 运行时、结果预算和非法游标回归测试。

## 第二轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：70 passed，1 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
```

其中 1 个跳过项仍是当前 Windows 权限不允许创建符号链接。

## 第三轮 Review 修复记录

依据 `docs/Review/REVIEW.md` 的第三轮 `CHANGES_REQUIRED` 结论，继续完成：

- 工作区敏感判断改为只检查工作区相对路径，显式 Bash 路径复用真实解析器并拒绝符号链接和 Windows 目录连接。
- `rg` 自动注入 `--no-follow` 与不可移除的敏感文件/目录排除规则，覆盖递归根目录和 `--files`；裸 `git diff` 及可能返回正文的变体不再允许。
- `MAX_READ_BYTES` 至少为 4，保证 UTF-8 截断游标可推进；`ToolExecutor` 使用 `MAX_OUTPUT_BYTES` 统一限制最终模型工具消息，并优先保留游标、退出码和截断状态。
- 增加递归敏感文件、显式符号链接、工作区祖先目录、最小 read 预算和最终消息总预算回归测试。

## 第三轮修复最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q：77 passed，2 skipped
.\.venv\Scripts\python.exe -m ruff check .：All checks passed!
.\.venv\Scripts\python.exe -m compileall -q src：通过
git diff --check：通过
```

2 个跳过项均与当前 Windows 环境不允许创建符号链接有关；真实模型 HTTP 取消仍按 Review 记录为本地 MVP 的 best-effort 限制。
