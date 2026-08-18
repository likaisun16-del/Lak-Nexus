"""最小 CLI 集成测试：验证入口参数和缺失配置时的明确退出码。"""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest

from likai_nexus.channels.cli import ConsoleEventSink, build_parser, main
from likai_nexus.errors import ConfigError, ModelBackendError
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.events import NullEventSink, RuntimeEvent
from likai_nexus.orchestrator.schemas import AssistantTurn
from likai_nexus.runtime import build_runtime as real_build_runtime
from likai_nexus.safety.approval import ApprovalRequest, CliApprovalHandler, StaticApprovalHandler


def test_cli_parser_accepts_unquoted_task_words() -> None:
    args = build_parser().parse_args(["读取", "README.md"])
    assert args.request == ["读取", "README.md"]
    assert args.review_mode == "strict"
    assert args.no_progress is False


def test_cli_parser_accepts_review_mode_and_progress_switches() -> None:
    args = build_parser().parse_args(
        ["--review-mode", "relaxed", "--no-progress", "执行", "任务"]
    )
    assert args.review_mode == "relaxed"
    assert args.no_progress is True


def test_cli_reports_missing_workspace(monkeypatch, tmp_path: Path) -> None:
    # 测试必须避开项目根目录 .env，确保验证的确是缺少配置的入口错误。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert main(["执行", "任务"]) == 2


def test_cli_invalid_numeric_config_redacts_credential(monkeypatch, tmp_path: Path, capsys) -> None:
    secret = "sk-proj-AbC123xYz789Qwe"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("MAX_OUTPUT_BYTES", secret)

    assert main(["启动配置测试"]) == 2

    stderr = capsys.readouterr().err
    assert "MAX_OUTPUT_BYTES" in stderr
    assert "必须是正整数" in stderr
    assert secret not in stderr


def test_cli_progress_sink_renders_structured_event() -> None:
    stream = StringIO()

    ConsoleEventSink(stream).emit(RuntimeEvent("task_started", "task", "任务开始"))

    assert stream.getvalue() == "[过程] 任务开始\n"


def test_cli_full_access_denial_stops_before_model(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)

    def build_denied_runtime(config, **kwargs):
        return real_build_runtime(
            config,
            backend=FakeModelBackend([AssistantTurn("不应调用")]),
            approvals=StaticApprovalHandler(False),
            **kwargs,
        )

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_denied_runtime)

    assert main(["--review-mode", "full-access", "拒绝任务"]) == 1
    assert "状态：cancelled" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "exit_code"),
    (
        (ConfigError("配置错误：token=CLI_CONFIG_SECRET"), 2),
        (ModelBackendError("后端错误：Bearer CLI_BEARER_SECRET"), 2),
        (
            RuntimeError(
                "启动异常：password=CLI_PASSWORD_SECRET "
                "-----BEGIN PRIVATE KEY-----\nCLI_PRIVATE_SECRET"
            ),
            1,
        ),
    ),
)
def test_cli_startup_errors_redact_sensitive_text(
    monkeypatch, settings, capsys, error, exit_code
) -> None:
    def fail(*args, **kwargs):
        raise error

    if isinstance(error, ConfigError):
        monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", fail)
    else:
        monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
        monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", fail)

    assert main(["启动错误测试"]) == exit_code
    output = capsys.readouterr().err
    assert type(error).__name__ in output
    for sentinel in (
        "CLI_CONFIG_SECRET",
        "CLI_BEARER_SECRET",
        "CLI_PASSWORD_SECRET",
        "CLI_PRIVATE_SECRET",
    ):
        assert sentinel not in output


def test_cli_no_progress_uses_null_sink_for_real_task(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)

    def build_no_progress_runtime(config, **kwargs):
        runtime = real_build_runtime(
            config,
            backend=FakeModelBackend([AssistantTurn("任务完成")]),
            approvals=StaticApprovalHandler(True),
            **kwargs,
        )
        assert isinstance(runtime.agent.event_sink, NullEventSink)
        return runtime

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_no_progress_runtime)

    assert main(["--no-progress", "执行任务"]) == 0
    assert "[过程]" not in capsys.readouterr().out


def test_cli_full_access_confirmation_requires_exact_token(monkeypatch) -> None:
    request = ApprovalRequest(
        action_type="full_access_session",
        summary="确认完全访问",
        confirmation_token="FULL-ACCESS",
    )
    handler = CliApprovalHandler()
    monkeypatch.setattr("builtins.input", lambda prompt: "full-access")

    assert asyncio.run(handler.request(request)) is False

    monkeypatch.setattr("builtins.input", lambda prompt: "FULL-ACCESS")
    assert asyncio.run(handler.request(request)) is True
