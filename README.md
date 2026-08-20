# Lak-Nexus
仿hermes全能智能体

## 本地运行

项目启动时会自动读取当前工作目录的 `.env` 文件，已有的进程环境变量会覆盖 `.env` 中的同名配置。
任务、会话、记忆和审计默认保存到 PostgreSQL；工作区内会自动创建 `script/` 作为脚本默认目录。
原 SQLite 数据已归档到 `data/legacy-backup-postgres-cutover-*`，默认 PG 启动不会再次读取该归档。

```powershell
Set-Location "D:\Desktop\Code\Lak-Nexus"
& ".\.venv\Scripts\Activate.ps1"
New-Item -ItemType Directory -Force ".\workspace" | Out-Null
likai-nexus "读取 README.md 并总结项目定位"
```

CLI 默认使用 `strict` 严格审查并实时展示任务过程；可用 `--no-progress` 关闭过程行，
也可用 `--review-mode relaxed` 启用逐次审批的原始 Shell，或用 `--review-mode full-access`
启用一次强确认后的完全访问模式。首次确认成功后会保存为 PostgreSQL 默认模式，后续未指定模式时沿用；
`--review-mode strict` 或 `--review-mode relaxed` 可切换默认模式。`full-access` 只允许本地 CLI 选择，不能通过远程渠道开启。
完全访问等价于把当前操作系统用户本身可用的文件和命令权限交给模型，不提供管理员权限或 OS 级沙箱。

将真实 `OPENAI_API_KEY` 等密钥填写在本地 `.env` 中。`.env` 已加入 `.gitignore`，不得提交到 GitHub。

Windows 运行 Bash 工具时请在 `.env` 配置 Git Bash 的 `BASH_PATH`；程序会拒绝误用 WSL 的 `bash.exe`。

相对 `DATABASE_PATH` 以项目根目录为基准，不再拼接到 `WORKSPACE_ROOT`；历史的
`workspace/data/likai_nexus.db` 和 `workspace/.likai_nexus/tasks.db` 不会被默认 PG 启动自动读取；
`data/preferences.json` 仅在数据库偏好首次组装时导入一次，成功后改名归档并保留恢复备份。

`MAX_READ_BYTES` 限制 read 正文且必须至少为 4；最终模型消息会为 read 状态信封预留 128 字节，因此 read 的实际正文上限为 `min(MAX_READ_BYTES, MAX_OUTPUT_BYTES - 128)`。`MAX_OUTPUT_BYTES` 必须至少为 132。

默认 PostgreSQL 版本支持用户显式管理长期记忆：

```powershell
likai-nexus memory add --type project "本项目先使用 SQLite 验证"
likai-nexus memory list
likai-nexus memory show <memory-id>
likai-nexus memory update <memory-id> --content "更新后的记忆"
likai-nexus memory disable <memory-id>
```

当前 ContextBuilder 会把当前 Session 活动分支、有效偏好和相似度达标的长期记忆组装给模型；未配置 Embedding API 时使用本地词项检索，配置豆包 Embedding 后使用 PostgreSQL/pgvector 检索。

迁移准备层提供 `SQLiteSnapshotExporter`、`PortableSnapshot` 和 PostgreSQL 快照恢复入口；`storage/postgres.py` 和 `memory/postgres_vector.py` 提供 PostgreSQL/pgvector 适配器。

本机已用 Docker 启动 PostgreSQL 16 + pgvector 0.8.6：容器名为 `lak-nexus-postgres`，数据卷为 `lak-nexus-pgdata`，端口只绑定 `127.0.0.1:5432`。Python 驱动安装在项目虚拟环境中，可用 `psycopg[binary]` 和 `pgvector`。

通过 `STORAGE_BACKEND=sqlite` 可以显式回退 SQLite。Embedding API 入口为 `models.embedding.create_embedding_provider()` 和 `DoubaoEmbeddingProvider`。在 `.env` 设置 `EMBEDDING_PROVIDER=doubao`、火山方舟 `EMBEDDING_API_KEY`、模型和维度后，主运行时会自动接入 pgvector；未配置时不会发起外部请求。模型地址、模型名和维度均可配置，后续更换火山方舟接入点不需要改 ContextBuilder。
