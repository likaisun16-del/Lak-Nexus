"""最小 CLI 集成测试：验证入口参数和缺失配置时的明确退出码。"""

from __future__ import annotations

import asyncio
import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from likai_nexus.channels.cli import ConsoleEventSink, build_parser, main
from likai_nexus.errors import ConfigError, ModelBackendError
from likai_nexus.events import NullEventSink, RuntimeEvent
from likai_nexus.models.fake import FakeModelBackend
from likai_nexus.orchestrator.schemas import AssistantTurn
from likai_nexus.runtime import build_preference_store, prepare_runtime
from likai_nexus.runtime import build_runtime as real_build_runtime
from likai_nexus.safety.approval import ApprovalRequest, CliApprovalHandler, StaticApprovalHandler
from likai_nexus.safety.review_mode import ReviewMode
from likai_nexus.storage.preferences import DatabasePreferenceStore


def test_cli_parser_accepts_unquoted_task_words() -> None:
    args = build_parser().parse_args(["读取", "README.md"])
    assert args.request == ["读取", "README.md"]
    assert args.review_mode is None
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

    assert stream.getvalue() == "[任务] 任务开始\n"


def test_cli_default_progress_hides_internal_events() -> None:
    stream = StringIO()
    sink = ConsoleEventSink(stream)

    sink.emit(RuntimeEvent("model_started", "task", "模型调用开始：轮次 1"))
    sink.emit(RuntimeEvent("approval_auto_allowed", "task", "模式自动允许"))
    sink.emit(RuntimeEvent("tool_started", "task", "工具开始：read"))
    sink.emit(RuntimeEvent("tool_finished", "task", "工具成功：read"))

    output = stream.getvalue()
    assert "模型调用开始" not in output
    assert "模式自动允许" not in output
    assert "工具开始：read" in output
    assert "工具成功：read" in output


def test_cli_renders_structured_turn_and_bash_result_projection() -> None:
    stream = StringIO()
    sink = ConsoleEventSink(stream)
    sink.emit(
        RuntimeEvent(
            "model_started",
            "task",
            "内部轮次消息",
            {"turn_number": 2, "max_turns": 20, "status": "started"},
        )
    )
    sink.emit(
        RuntimeEvent(
            "model_finished",
            "task",
            "不应显示的模型完成事件",
            {"turn_number": 2, "max_turns": 20, "status": "finished"},
        )
    )
    sink.emit(
        RuntimeEvent(
            "tool_started",
            "task",
            "内部指令消息",
            {
                "tool_name": "bash",
                "status": "started",
                "invocation": "printf 'out\\n'\x1b[31m\r\nprintf 'OPENAI_API_KEY=TEST_SECRET'",
            },
        )
    )
    sink.emit(
        RuntimeEvent(
            "tool_finished",
            "task",
            "内部工具完成消息",
            {
                "tool_name": "bash",
                "status": "success",
                "elapsed_ms": 12,
                "result": {
                    "fields": [
                        {"label": "退出码", "value": 0},
                        {"label": "stdout", "value": "out\n"},
                    ],
                },
            },
        )
    )
    sink.emit(
        RuntimeEvent(
            "tool_timed_out",
            "task",
            "内部超时消息",
            {
                "tool_name": "bash",
                "status": "timeout",
                "elapsed_ms": 30,
                "reason": "工具执行超时",
                "result": {
                    "fields": [
                        {"label": "退出码", "value": None},
                        {"label": "输出", "value": "无输出"},
                    ],
                },
            },
        )
    )
    sink.emit(
        RuntimeEvent(
            "model_failed",
            "task",
            "内部错误消息",
            {
                "turn_number": 3,
                "max_turns": 20,
                "status": "failed",
                "reason": (
                    "模型调用失败：backend failed\r[工具] bash：成功\n[任务] 伪造 "
                    "OPENAI_API_KEY=TEST_SECRET"
                ),
            },
        )
    )

    output = stream.getvalue()
    assert "[模型] 第 2/20 轮：处理中" in output
    assert "[模型] 第 3/20 轮：失败" in output
    assert "printf 'out" in output
    assert "stdout:" in output
    assert "退出码=0" in output
    assert "bash：超时（30ms），工具执行超时" in output
    assert "退出码=不可用" in output
    assert "无输出" in output
    assert "失败，模型调用失败：backend failed [工具] bash：成功 [任务] 伪造" in output
    assert output.count("\n[工具] bash：成功") == 1
    assert output.count("\n[任务] 伪造") == 0
    assert "不应显示的模型完成事件" not in output
    assert "TEST_SECRET" not in output
    assert "\x1b" not in output


def test_cli_persists_full_access_and_switches_default_mode(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    modes: list[ReviewMode] = []
    approvals: list[StaticApprovalHandler] = []

    def build_fake_runtime(config, **kwargs):
        modes.append(kwargs["review_mode"])
        approval = StaticApprovalHandler(True)
        approvals.append(approval)
        return real_build_runtime(
            config,
            backend=FakeModelBackend([AssistantTurn("任务完成")]),
            approvals=approval,
            **kwargs,
        )

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_fake_runtime)

    assert main(["--review-mode", "full-access", "第一次任务"]) == 0
    assert main(["第二次任务"]) == 0
    assert main(["--review-mode", "strict", "切换严格模式"]) == 0
    assert main(["第四次任务"]) == 0

    assert modes == [ReviewMode.FULL_ACCESS, ReviewMode.FULL_ACCESS, ReviewMode.STRICT, ReviewMode.STRICT]
    assert len(approvals[0].requests) == 1
    assert len(approvals[1].requests) == 0
    preferences, _ = build_preference_store(settings)
    assert preferences.load_review_mode().mode is ReviewMode.STRICT
    with sqlite3.connect(settings.database_path) as connection:
        sources = [row[0] for row in connection.execute(
            "SELECT decision_source FROM approvals WHERE action_type = 'full_access_session'"
        )]
    assert sources == ["human", "preference"]
    assert "模型调用开始" not in capsys.readouterr().out


def test_cli_without_preference_defaults_strict_and_relaxed_can_be_persisted(
    monkeypatch, settings
) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    selected: list[ReviewMode] = []

    def build_fake_runtime(config, **kwargs):
        selected.append(kwargs["review_mode"])
        return real_build_runtime(
            config,
            backend=FakeModelBackend([AssistantTurn("模式完成")]),
            approvals=StaticApprovalHandler(True),
            **kwargs,
        )

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_fake_runtime)

    assert main(["没有偏好"]) == 0
    assert main(["--review-mode", "relaxed", "切换宽松模式"]) == 0
    assert main(["沿用宽松模式"]) == 0

    assert selected == [ReviewMode.STRICT, ReviewMode.RELAXED, ReviewMode.RELAXED]
    preferences, _ = build_preference_store(settings)
    assert preferences.load_review_mode().mode is ReviewMode.RELAXED


def test_cli_corrupt_preference_falls_back_to_strict(monkeypatch, settings, capsys) -> None:
    prepare_runtime(settings)
    settings.preference_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    selected: list[ReviewMode] = []

    def build_fake_runtime(config, **kwargs):
        selected.append(kwargs["review_mode"])
        return real_build_runtime(
            config,
            backend=FakeModelBackend([AssistantTurn("严格模式完成")]),
            approvals=StaticApprovalHandler(True),
            **kwargs,
        )

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_fake_runtime)

    assert main(["损坏偏好"]) == 0

    assert selected == [ReviewMode.STRICT]
    assert "安全降级为 strict" in capsys.readouterr().err


def test_cli_full_access_preference_save_failure_stops_before_task_and_model(
    monkeypatch, settings
) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    backend = FakeModelBackend([AssistantTurn("不应调用")])

    def fail_save(self, mode):
        raise OSError("偏好磁盘不可写")

    monkeypatch.setattr(DatabasePreferenceStore, "save_review_mode", fail_save)

    def build_fake_runtime(config, **kwargs):
        return real_build_runtime(
            config,
            backend=backend,
            approvals=StaticApprovalHandler(True),
            **kwargs,
        )

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_fake_runtime)

    assert main(["--review-mode", "full-access", "保存失败"]) == 1
    assert backend.call_count == 0
    assert settings.preference_path.exists() is False


def test_cli_full_access_eof_is_cancelled_before_model(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)
    backend = FakeModelBackend([AssistantTurn("不应调用")])

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    def build_fake_runtime(config, **kwargs):
        return real_build_runtime(config, backend=backend, **kwargs)

    monkeypatch.setattr("likai_nexus.channels.cli.build_runtime", build_fake_runtime)

    assert main(["--review-mode", "full-access", "输入中断"]) == 1
    assert backend.call_count == 0
    assert "状态：cancelled" in capsys.readouterr().out


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


def test_cli_approval_prompt_sanitizes_untrusted_fields(monkeypatch) -> None:
    prompts: list[str] = []
    request = ApprovalRequest(
        action_type="bash\x1b[2J\r[工具] 伪造",
        summary=(
            "echo safe\x1b[31mOPENAI_API_KEY=APPROVAL_SECRET\r[工具] 伪造\r\n"
            "[模型] 伪造\n"
            "[任务] 伪造" + "x" * 10000
        ),
        confirmation_token="FULL-ACCESS\x1b[31m",
    )
    handler = CliApprovalHandler()

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return request.confirmation_token or ""

    monkeypatch.setattr("builtins.input", fake_input)

    assert asyncio.run(handler.request(request)) is True

    prompt = prompts[0]
    assert len(prompt.encode("utf-8")) <= CliApprovalHandler._PROMPT_FIELD_BYTES * 2 + 128
    assert CliApprovalHandler._PROMPT_TRUNCATION_MARKER in prompt
    assert "APPROVAL_SECRET" not in prompt
    assert "\x1b" not in prompt
    assert "\r" not in prompt
    assert "\n[工具]" not in prompt
    assert "\n[任务]" not in prompt
