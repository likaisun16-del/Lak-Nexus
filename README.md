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

将真实 `OPENAI_API_KEY` 等密钥填写在本地 `.env` 中。`.env` 已加入 `.gitignore`，不得提交到 GitHub。

Windows 运行 Bash 工具时请在 `.env` 配置 Git Bash 的 `BASH_PATH`；程序会拒绝误用 WSL 的 `bash.exe`。

`MAX_READ_BYTES` 限制 read 正文且必须至少为 4；最终模型消息会为 read 状态信封预留 128 字节，因此 read 的实际正文上限为 `min(MAX_READ_BYTES, MAX_OUTPUT_BYTES - 128)`。`MAX_OUTPUT_BYTES` 必须至少为 132。
