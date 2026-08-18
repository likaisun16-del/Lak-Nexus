# Lak-Nexus
仿hermes全能智能体

## 本地运行

项目启动时会自动读取当前工作目录的 `.env` 文件，已有的进程环境变量会覆盖 `.env` 中的同名配置。

```powershell
Set-Location "D:\Desktop\Code\Lak-Nexus"
& ".\.venv\Scripts\Activate.ps1"
New-Item -ItemType Directory -Force ".\workspace" | Out-Null
likai-nexus "读取 README.md 并总结项目定位"
```

CLI 默认使用 `strict` 严格审查并实时展示任务过程；可用 `--no-progress` 关闭过程行，
也可用 `--review-mode relaxed` 启用逐次审批的原始 Shell，或用 `--review-mode full-access`
启用一次强确认后的完全访问模式。`full-access` 只允许本地 CLI 选择，不能通过远程渠道开启。
完全访问等价于把当前操作系统用户本身可用的文件和命令权限交给模型，不提供管理员权限或 OS 级沙箱。

将真实 `OPENAI_API_KEY` 等密钥填写在本地 `.env` 中。`.env` 已加入 `.gitignore`，不得提交到 GitHub。

Windows 运行 Bash 工具时请在 `.env` 配置 Git Bash 的 `BASH_PATH`；程序会拒绝误用 WSL 的 `bash.exe`。

`MAX_READ_BYTES` 限制 read 正文且必须至少为 4；最终模型消息会为 read 状态信封预留 128 字节，因此 read 的实际正文上限为 `min(MAX_READ_BYTES, MAX_OUTPUT_BYTES - 128)`。`MAX_OUTPUT_BYTES` 必须至少为 132。
