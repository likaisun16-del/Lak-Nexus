"""配置与安全测试：验证 Settings、WorkspacePathResolver、CommandPolicy 和脱敏工具的边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from likai_nexus.config import Settings
from likai_nexus.errors import CommandDeniedError, ConfigError, PathAccessError
from likai_nexus.safety.command_policy import CommandPolicy
from likai_nexus.safety.paths import WorkspaceAccessPolicy, WorkspacePathResolver
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


def test_config_rejects_read_budget_that_cannot_advance_cursor(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MAX_READ_BYTES 至少为 4"):
        Settings(workspace_root=tmp_path, max_read_bytes=3)


def test_config_rejects_output_budget_without_status_envelope(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MAX_OUTPUT_BYTES 至少为 64"):
        Settings(workspace_root=tmp_path, max_output_bytes=63)


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


def test_workspace_root_parent_name_does_not_mark_normal_file_sensitive(tmp_path: Path) -> None:
    workspace = tmp_path / "private" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "normal.txt").write_text("normal", encoding="utf-8")

    resolved = WorkspacePathResolver(workspace).resolve("normal.txt", require_exists=True)

    assert resolved.relative_path == "normal.txt"


def test_command_policy_rejects_explicit_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")

    decision = CommandPolicy(WorkspacePathResolver(tmp_path)).evaluate("rg sentinel link.txt")

    assert not decision.allowed
    assert "符号链接" in decision.reason


@pytest.mark.parametrize("sensitive_name", [".env", ".env.local", "credentials.json", "private.pem"])
def test_workspace_rejects_sensitive_files(tmp_path: Path, sensitive_name: str) -> None:
    path = tmp_path / sensitive_name
    path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(PathAccessError, match="密钥或凭据"):
        WorkspacePathResolver(tmp_path).resolve(sensitive_name, require_exists=True)


@pytest.mark.parametrize("path", [".env", "credentials.json", "private.pem", ".ssh/id_rsa"])
def test_shared_workspace_policy_rejects_sensitive_bash_paths(path: str) -> None:
    assert WorkspaceAccessPolicy.is_sensitive_path(path)


@pytest.mark.parametrize(
    "command,reason",
    [
        ("curl https://example.com", "网络"),
        ("pwd && git status", "组合"),
        ("rm -rf .", "允许列表"),
        ("git push", "不允许"),
        ("git diff", "只允许只读展示选项"),
        ("git diff --check", "只允许只读展示选项"),
        ("python -c 'print(1)'", "compileall"),
        ("rg SENTINEL .env", "敏感资源"),
        ("rg SENTINEL credentials.json", "敏感资源"),
        ("ls private.pem", "敏感资源"),
        ("rg SENTINEL .ssh/known_hosts", "敏感资源"),
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
    decision = policy.check("rg --files")
    assert decision.allowed
    assert decision.argv[:2] == ("rg", "--files")
    assert "--no-follow" in decision.argv
    assert "--glob-case-insensitive" in decision.argv
    assert "!**/credentials.json" in decision.argv


def test_command_policy_protects_recursive_rg_root() -> None:
    decision = CommandPolicy().check("rg SENTINEL .")

    assert decision.argv[:3] == ("rg", "SENTINEL", ".")
    assert "!**/.env*" in decision.argv
    assert "!**/.ssh/**" in decision.argv
    assert "!**/private/**" in decision.argv


@pytest.mark.parametrize("command", ["rg --files -g '*.py'", "rg --files *.py", "pwd $PWD"])
def test_command_policy_rejects_shell_expansion(command: str) -> None:
    decision = CommandPolicy().evaluate(command)

    assert not decision.allowed
    assert "Shell" in decision.reason


def test_command_policy_check_raises_specific_error() -> None:
    with pytest.raises(CommandDeniedError, match="网络"):
        CommandPolicy().check("wget https://example.com")


def test_sensitive_values_are_redacted() -> None:
    assert "secret-value" not in redact_text("token=secret-value")
    assert "api-secret" not in redact_arguments({"api_key": "api-secret", "path": "a.txt"})
