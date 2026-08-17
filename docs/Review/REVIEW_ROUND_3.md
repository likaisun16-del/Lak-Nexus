# 第三轮代码复审报告（归档）

## 审查信息

- 审查状态：COMPLETED
- 审查人：Codex Reviewer
- 审查时间：2026-08-17
- 对应计划：`docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- 上一轮报告：[`docs/Review/REVIEW_ROUND_2.md`](./REVIEW_ROUND_2.md)
- 第一轮报告：[`docs/Review/REVIEW_ROUND_1.md`](./REVIEW_ROUND_1.md)
- 修复交接：`docs/Implement/IMPLEMENTATION_NOTES.md`
- 修复基线：`65718fa`（Document second-round code review findings and remaining P1 blockers）
- 本轮审查提交：`05656c9`（Fix second-round review findings）
- 对应分支：`agent/publish-mvp`
- 用例附件：[`REVIEW_USE_CASE_ROUND_3.svg`](./REVIEW_USE_CASE_ROUND_3.svg) / [`REVIEW_USE_CASE_ROUND_3.png`](./REVIEW_USE_CASE_ROUND_3.png)
- Reviewer 边界：仅在 `docs/Review/` 内归档第二轮报告并新增本报告和第三轮用例图，未修改业务代码、测试、配置、计划或实现交接文件

## 审查结论

- [ ] PASS
- [x] CHANGES_REQUIRED

第二轮提出的 Bash 审批最小审计和 Git Bash/WSL 运行时识别已经关闭；显式写出 `.env`、凭据和私钥路径的 Bash 命令也已被拒绝。全量测试、Ruff、编译和 diff 检查均通过。

但“跨工具敏感资源策略”只完成了路径字符串分类，没有覆盖目录递归、仓库级读取和符号链接等实际访问语义。复审已确认：文件工具拒绝 `credentials.json` 的同一工作区内，允许命令 `rg <哨兵> .` 仍能读取该文件并把无标签哨兵返回模型。因此本轮仍有 1 个 P1；在该问题关闭前，不建议接入真实敏感工作区或远程消息渠道。

## 第二轮问题关闭情况

| 第二轮问题 | 本轮状态 | 复审判断 |
|---|---|---|
| P1-1 Bash 与文件工具共享敏感资源策略 | 部分关闭 | 显式敏感文件名已拒绝；目录递归、仓库级读取和真实路径解析仍可绕过 |
| P1-2 Bash 审批审计保存原始 argv | 已关闭 | 持久化摘要仅保留可执行文件、参数数量、超时、命令哈希和审批指纹 |
| P1-3 Windows 自动发现误选 WSL Bash | 已关闭 | 优先发现 Git for Windows，拒绝 WindowsApps/WSL，并在启动阶段探测运行时 |
| P2-1 read 返回预算和游标契约 | 部分关闭 | 正文不再超限且非法字节边界被拒绝；极小预算下游标不前进 |
| P2-2 Bash 截断标记对模型不可见 | 主问题已关闭 | 截断标记和安全 metadata 已进入模型消息；完整模型消息仍超过配置预算 |
| P2-3 真实模型运行中取消 | 未关闭、已明确限制 | 仍是 `asyncio.to_thread(urllib)` 的 best-effort 取消 |
| P2-4 测试矩阵缺口 | 部分关闭 | 新增 15 项测试，但递归敏感读取、游标推进、消息总预算、数据库迁移和完整 CLI 链路仍未覆盖 |

## 做得较好的部分

- `CLI → AgentLoop → ToolExecutor → Safety/Tool → Storage` 的主依赖方向保持稳定，没有引入无关架构。
- Bash 审批展示与持久化审计已经分离；无标签命令参数不再进入审批表或工具调用审计。
- Git Bash 自动发现、WSL 拒绝和代表命令成功断言修正了上一轮测试误判。
- `read` 已能拒绝落在 UTF-8 字符中间或超过行长度的游标，正文自身也不再突破 `max_read_bytes`。
- Bash 状态前缀和截断标记在正文预算内保留，ToolExecutor 也把经过允许列表筛选的 metadata 回填模型。
- 新增 `WorkspaceAccessPolicy` 的方向正确，说明实现已开始收敛跨工具规则；当前问题在于该类型只做名称匹配，尚未形成真实访问边界。

## 严重级别

- P0：阻塞发布，可能造成严重数据、权限或安全问题。
- P1：重要安全或核心功能错误，必须修复后才能 PASS。
- P2：一般功能边界、维护性问题或测试缺口。

## 问题列表

### P0

暂无。

### P1

#### P1-1：Bash 敏感资源策略只检查参数名称，目录递归仍可把凭据内容返回模型

- 证据：`src/likai_nexus/safety/paths.py:35-57` 的 `WorkspaceAccessPolicy` 仅按路径组件名称判断敏感性；它不持有工作区根目录、不解析真实路径，也不检查目录下将被命令递归访问的文件。
- 证据：`src/likai_nexus/safety/command_policy.py:133-136` 把 `rg` 的后续普通参数当作路径检查，但 `src/likai_nexus/safety/command_policy.py:183-193` 对 `.` 直接放行。Bash 随后以工作区为 cwd 执行该 argv，并把 stdout 回填模型。
- 最小复现：在隔离工作区创建包含虚构哨兵的 `credentials.json`。`WorkspacePathResolver.resolve("credentials.json")` 正确拒绝；`CommandPolicy.evaluate("rg UNLABELED_REVIEW_SENTINEL_3 .")` 却返回允许，Bash 成功执行且结果包含完整哨兵。
- 同类缺口：当前策略也没有对显式相对路径做真实路径解析，无法证明符号链接或目录连接仍位于工作区；裸 `git diff` 没有路径参数但可以输出仓库文件正文。`pytest` 等执行项目代码的命令更无法由字符串分类器构成强隔离，这一点仍属于已知沙箱风险。
- 影响：敏感资源访问规则取决于模型选择了 `read` 还是 `bash`，违反“四工具不能绕过 Safety”和“敏感内容不进入模型”的要求。人工审批显示的是看似普通的 `rg ... .`，无法列出它实际会读取的全部文件。
- 必须修改：不要把名称分类器等同于访问策略。Bash 应使用真实工作区解析器和按命令定义的访问语义：对 `rg`/`--files` 注入不可由模型移除的敏感文件排除规则，对目录目标和符号链接做边界检查；裸 `git diff` 应改为只允许 `--stat`、`--name-only`、`--check` 等不返回正文的形式，或建立等价的安全输出策略。能够执行项目代码的命令继续要求审批，并在远程接入前放入 OS 级隔离。
- 必须测试：覆盖 `rg 哨兵 .`、`rg --files .`、敏感文件位于普通子目录、显式符号链接/目录连接、`git diff` 中包含无标签哨兵等场景；断言哨兵不进入模型工具消息、CLI 输出或 SQLite。

### P2

#### P2-1：`read` 在不足一个 UTF-8 字符的预算下返回相同游标，调用无法取得进展

- `src/likai_nexus/executor/tools/read_file.py:148-153` 在 `_safe_utf8_prefix()` 返回空字节时，以 `truncated=True` 返回原始 `(offset, byte_offset)`。
- `src/likai_nexus/config.py:101-111` 只要求 `max_read_bytes > 0`，因此 1、2、3 字节均是合法配置。
- 最小复现：`max_read_bytes=1` 读取首字符为中文的文件，连续两次结果均为空且 `next_cursor="0:0"`。模型按游标继续读取会重复同一调用直至达到最大轮数。
- 测试问题：`tests/unit/test_file_tools.py:89-98` 当前把 `next_cursor == "0:0"` 作为预期，只验证不拆字符，没有验证截断游标必须推进。
- 建议：最小修复是配置层要求 `MAX_READ_BYTES >= 4`，保证至少容纳一个 UTF-8 code point；或在预算不足时返回明确配置错误，禁止返回不可推进的截断游标。

#### P2-2：正文预算修复后，ToolExecutor 追加 metadata 使模型可见消息再次超过上限

- `src/likai_nexus/executor/tools/read_file.py` 和 `src/likai_nexus/executor/tools/bash.py:406-420` 只约束工具正文。
- `src/likai_nexus/executor/service.py:232-257` 随后无总量限制地追加 JSON metadata。实测 64 字节正文加安全 metadata 后，模型可见工具消息为 144 字节。
- 影响：截断状态现在可见，但计划声明的“最多返回 64 KiB”仍不是完整模型上下文的硬边界；极小配置下 metadata 甚至可能远大于正文预算。
- 建议：由 ToolExecutor 或独立的 `ToolMessageFormatter` 统一拥有最终消息预算，先预留状态信封，再用剩余空间放正文。若配置只想约束正文，应把名称改成 `MAX_*_BODY_BYTES` 并在计划与 README 明确，不要继续宣称限制完整返回消息。

#### P2-3：敏感路径检查扫描绝对路径祖先，可能拒绝正常工作区中的所有文件

- `src/likai_nexus/safety/paths.py:100-105` 将解析后的绝对路径交给名称分类器，分类器会扫描工作区根目录以上的每个组件。
- 最小复现：工作区位于 `<临时目录>/private/workspace`，读取其中普通 `normal.txt` 会被判定为敏感资源并拒绝。
- 建议：工作区初始化时单独验证根目录；对工具输入只检查相对于 `WORKSPACE_ROOT` 的路径组件，避免主机目录名、用户名或挂载点造成误拒绝。

#### P2-4：真实模型取消仍是 best-effort

- `src/likai_nexus/models/openai_backend.py:42-44` 仍以 `asyncio.to_thread()` 包装阻塞 `urllib`，取消 await 不能终止正在运行的请求线程。
- 当前 Fake Backend 测试证明编排协议能传播取消，不等价于真实 HTTP 连接能及时释放。
- 建议：本地 MVP 可继续作为已知限制；接入远程长期任务前改用支持异步取消的 HTTP 传输，并增加传输层取消与超时测试。

#### P2-5：测试矩阵仍有关键空白

- 本轮 P1 的目录递归、仓库级读取和真实路径/链接访问没有覆盖。
- Bash 仍缺少审批拒绝、固定 cwd 和敏感环境变量清理的完整执行断言；运行时探测只确认 `command -v`，没有单独覆盖 `ruff`/`pytest` 成功路径。
- SQLite 旧 `tool_calls` 表迁移仍缺少数据保留和重复初始化测试。
- CLI 集成测试仍未覆盖 Fake Backend 成功任务、审批拒绝、Ctrl+C 退出码和最终任务状态。
- Windows 符号链接测试继续因权限跳过，至少应在 CI 的 Linux 或具备 Developer Mode 的 Windows job 中补充不可跳过的验证。

## 架构与功能优化建议

### 第一优先级：区分“名称分类”与“真实访问策略”

1. 当前 `WorkspaceAccessPolicy` 更接近 `SensitivePathClassifier`：它只回答名字是否敏感。真实访问策略还必须结合工作区根目录、规范化路径、符号链接和命令的递归语义。
2. 保持简单的逐命令校验函数，不需要引入通用策略 DSL。为 `rg`、`git`、`pytest`/`compileall` 明确各自可能读取或执行的资源，并采用不同限制。
3. 将“路径允许”与“输出允许进入模型”视为两道边界。即使命令可以本地运行，含源码或凭据正文的输出也未必应该直接进入模型。

### 第二优先级：统一最终工具消息预算

1. 让一个组件负责生成最终模型工具消息：状态、错误类型、截断标记、下一游标、正文必须共享同一字节预算。
2. metadata 继续采用明确允许列表；对 `path` 等字符串字段设置长度上限，避免结构化字段本身扩大上下文。
3. `read` 配置至少保证可容纳一个 UTF-8 字符，并把“截断游标必须推进或明确失败”写成不变量测试。

### 第三优先级：保持运行时和状态边界可演进

1. Git Bash 探测是固定、可信的启动健康检查，可从 `BashTool` 中收敛为小型 Runtime Validator，并缓存验证后的路径；同时在文档中明确它是“不经人工审批的固定内部探测”，禁止未来加入用户输入。
2. `read` 当前按整行加载和解码，超大单行仍可能占用远高于配置预算的内存。后续可按固定字节块读取并维护 UTF-8 边界，但不必在本次 P1 修复中引入复杂流式框架。
3. 远程并发前再处理条件式任务状态更新、write/edit 输入大小上限和可取消 HTTP；当前不需要引入 FastAPI、消息队列、ORM、多智能体或通用工作流引擎。

## 检查项目

- [ ] 完全满足 `docs/Planner/MINIMAL_AGENT_FOUR_TOOLS_PLAN.md`
- [x] 保持清晰分层，Agent Loop 未直接访问文件、进程或 SQLite
- [x] write/edit 审批绑定内容摘要和目标状态
- [x] Bash 审批和工具调用审计不保存原始命令参数
- [x] Windows Git Bash 运行时身份明确，WSL 入口被拒绝
- [x] Shell 展开绕过和无界 Bash 输出缓冲已关闭
- [x] 工具或审计异常能使任务进入明确终态
- [ ] 四工具共享一致、不可绕过的敏感资源边界
- [ ] read 截断游标在所有合法配置下可推进或明确失败
- [ ] 模型可见工具消息满足统一总量限制
- [ ] 敏感路径规则不会因工作区祖先目录误拒绝
- [ ] 真实模型调用具备及时取消能力
- [ ] 计划测试矩阵已完整覆盖
- [x] 当前测试、Ruff、编译和 diff 检查通过
- [x] Reviewer 未修改业务代码

## 验证记录

```text
.\.venv\Scripts\python.exe -m pytest -q
结果：70 passed，1 skipped
跳过项：Windows 当前权限不允许创建符号链接

.\.venv\Scripts\ruff.exe check .
结果：All checks passed!

.\.venv\Scripts\python.exe -m compileall -q src
结果：通过

git diff --check 65718fa..HEAD
结果：通过

审查开始与业务验证结束时 git status --short
结果：clean

附加隔离最小复现：
- discovered_git_bash=C:\Program Files\Git\bin\bash.exe
- file_tool_credentials_denied=True
- bash_recursive_root_allowed=True
- bash_recursive_result_error=False
- bash_recursive_output_contains_unlabeled_sentinel=True
- read_first_cursor=0:0
- read_second_cursor=0:0
- read_cursor_progresses=False
- private_parent_normal_file_allowed=False
- tool_body_bytes=64
- model_visible_bytes=144
- model_visible_metadata=True
```

说明：复审没有读取项目根目录 `.env` 内容，也没有使用、输出或构造真实密钥；敏感资源复现仅在自动清理的临时目录中使用虚构文件和哨兵文本。

## 复审门槛

1. 修复 P1-1：目录递归、仓库级正文输出和真实路径/链接均不得绕过敏感资源策略。
2. 为 P1-1 增加修复前失败、修复后通过的模型消息与 CLI/审计回归测试。
3. 修复 P2-1/P2-2/P2-3：小预算游标可推进或明确失败、最终工具消息总量受控、工作区祖先名称不造成误拒绝。
4. 明确 P2-4 是本地 MVP 接受的已知限制；远程渠道接入前必须关闭。
5. 补齐相关测试后重新执行 pytest、Ruff、compileall、`git diff --check`，并在 `docs/Implement/IMPLEMENTATION_NOTES.md` 记录修复与验证结果。

## 最终意见

**CHANGES_REQUIRED**

第二轮的审计和运行时问题已经有效关闭，现有架构无需重做。本轮阻塞集中在 Bash 的实际资源访问集合仍大于名称策略覆盖范围；优先修复该安全边界，再收口 read 游标与最终消息预算即可进入下一次复审。
