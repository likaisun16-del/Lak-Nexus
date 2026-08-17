# Lak-Nexus
仿hermes全能智能体

## 本地运行

项目启动时会自动读取项目根目录的 `.env` 文件，已有的进程环境变量会覆盖 `.env` 中的同名配置。

```powershell
Set-Location "D:\Desktop\Code\Lak-Nexus"
& ".\.venv\Scripts\Activate.ps1"
New-Item -ItemType Directory -Force ".\workspace" | Out-Null
likai-nexus "读取 README.md 并总结项目定位"
```

将真实 `OPENAI_API_KEY` 等密钥填写在本地 `.env` 中。`.env` 已加入 `.gitignore`，不得提交到 GitHub。
