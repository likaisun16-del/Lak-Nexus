"""配置与安全测试：验证工作区限制、命令拒绝和敏感信息脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest

from likai_nexus.config import Settings
from likai_nexus.errors import CommandDeniedError, ConfigError, PathAccessError
from likai_nexus.safety.command_policy import CommandPolicy
from likai_nexus.safety.paths import WorkspacePathResolver
from likai_nexus.safety.redaction import redact_arguments, redact_text


def test_config_requires_workspace_root() -> None:
    with pytest.raises(ConfigError, match="WORKSPACE_ROOT"):
        Settings.from_env({})


def test_config_loads_dotenv_and_process_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".env").write_text(
        f'WORKSPACE_ROOT="{workspace}"\nMODEL=from-dotenv\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MODEL", "from-process")

    settings = Settings.from_env()

    assert settings.workspace_root == workspace.resolve()
    assert settings.model == "from-process"
    assert settings.api_key is None


def test_config_reports_dotenv_line_number_for_invalid_syntax(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("WORKSPACE_ROOT=\nBROKEN_LINE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match=r"\.env 第 2 行"):
        Settings.from_env()


def test_config_rejects_non_directory_workspace(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是已存在的目录"):
        Settings(workspace_root=file_path)


def test_workspace_rejects_parent_escape(tmp_path: Path) -> None:
    resolver = WorkspacePathResolver(tmp_path)
    with pytest.raises(PathAccessError, match="工作区外"):
        resolver.resolve("../outside.txt")


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-likai-nexus.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    with pytest.raises(PathAccessError, match="工作区外"):
        WorkspacePathResolver(tmp_path).resolve("outside-link.txt", require_exists=True)


@pytest.mark.parametrize(
    "command,reason",
    [
        ("curl https://example.com", "网络"),
        ("pwd && git status", "组合"),
        ("rm -rf .", "允许列表"),
        ("git push", "不允许"),
        ("python -c 'print(1)'", "compileall"),
    ],
)
def test_command_policy_rejects_dangerous_or_unlisted(command: str, reason: str) -> None:
    decision = CommandPolicy().evaluate(command)
    assert not decision.allowed
    assert reason in decision.reason


def test_command_policy_allows_read_only_commands() -> None:
    policy = CommandPolicy()
    assert policy.check("pwd").allowed
    assert policy.check("git status --short").allowed
    assert policy.check("python -m compileall src").allowed
    assert policy.check("rg --files -g '*.py'").allowed


def test_command_policy_check_raises_specific_error() -> None:
    with pytest.raises(CommandDeniedError, match="网络"):
        CommandPolicy().check("wget https://example.com")


def test_sensitive_values_are_redacted() -> None:
    assert "secret-value" not in redact_text("token=secret-value")
    assert "api-secret" not in redact_arguments({"api_key": "api-secret", "path": "a.txt"})
