# Lak-Nexus
仿hermes全能智能体

## 本地运行

项目启动时会自动读取当前工作目录的 `.env` 文件，已有的进程环境变量会覆盖 `.env` 中的同名配置。
应用数据默认保存到当前项目根目录的 `data/`，工作区内会自动创建 `script/` 作为脚本默认目录。

```powershell
Set-Location "D:\Desktop\Code\Lak-Nexus"
& ".\.venv\Scripts\Activate.ps1"
New-Item -ItemType Directory -Force ".\workspace" | Out-Null
likai-nexus "读取 README.md 并总结项目定位"
```

CLI 默认使用 `strict` 严格审查并实时展示任务过程；可用 `--no-progress` 关闭过程行，
也可用 `--review-mode relaxed` 启用逐次审批的原始 Shell，或用 `--review-mode full-access`
启用一次强确认后的完全访问模式。首次确认成功后会保存为本机默认模式，后续未指定模式时沿用；
`--review-mode strict` 或 `--review-mode relaxed` 可切换默认模式。`full-access` 只允许本地 CLI 选择，不能通过远程渠道开启。
完全访问等价于把当前操作系统用户本身可用的文件和命令权限交给模型，不提供管理员权限或 OS 级沙箱。

将真实 `OPENAI_API_KEY` 等密钥填写在本地 `.env` 中。`.env` 已加入 `.gitignore`，不得提交到 GitHub。

Windows 运行 Bash 工具时请在 `.env` 配置 Git Bash 的 `BASH_PATH`；程序会拒绝误用 WSL 的 `bash.exe`。

相对 `DATABASE_PATH` 以项目根目录为基准，不再拼接到 `WORKSPACE_ROOT`；历史的
`workspace/data/likai_nexus.db` 和 `workspace/.likai_nexus/tasks.db` 会在默认数据库首次启动时安全迁移并保留备份。

`MAX_READ_BYTES` 限制 read 正文且必须至少为 4；最终模型消息会为 read 状态信封预留 128 字节，因此 read 的实际正文上限为 `min(MAX_READ_BYTES, MAX_OUTPUT_BYTES - 128)`。`MAX_OUTPUT_BYTES` 必须至少为 132。

SQLite 版本支持用户显式管理长期记忆：

```powershell
likai-nexus memory add --type project "本项目先使用 SQLite 验证"
likai-nexus memory list
likai-nexus memory show <memory-id>
likai-nexus memory update <memory-id> --content "更新后的记忆"
likai-nexus memory disable <memory-id>
```

当前 ContextBuilder 会把当前 Session 活动分支、有效 SQLite 偏好和本地相似度达标的长期记忆组装给模型；本地相似度检索器是后续接入真实向量数据库前的可替换验证实现。

迁移准备层提供 `SQLiteSnapshotExporter`、`PortableSnapshot` 和 `restore_sqlite_snapshot()`，可在不安装外部数据库的情况下验证 SQLite 数据快照导出与恢复。`storage/postgres.py` 和 `memory/postgres_vector.py` 提供可选的 PostgreSQL/pgvector 适配器，当前默认仍使用 SQLite。

本机已用 Docker 启动 PostgreSQL 16 + pgvector 0.8.6：容器名为 `lak-nexus-postgres`，数据卷为 `lak-nexus-pgdata`，端口只绑定 `127.0.0.1:5432`。Python 驱动安装在项目虚拟环境中，可用 `psycopg[binary]` 和 `pgvector`。

Embedding API 入口为 `models.embedding.create_embedding_provider()` 和 `DoubaoEmbeddingProvider`。在 `.env` 设置 `EMBEDDING_PROVIDER=doubao`、火山方舟 `EMBEDDING_API_KEY`、模型和维度后即可注入 `runtime.build_postgres_context_builder()`；未配置时不会发起外部请求，默认 SQLite Runtime 不变。模型地址、模型名和维度均可配置，后续更换火山方舟接入点不需要改 ContextBuilder。
