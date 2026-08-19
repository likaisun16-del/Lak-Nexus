"""本地 CLI：负责参数接收、审批交互和结果展示，不直接执行文件或进程。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from ..config import Settings
from ..errors import ConfigError, ModelBackendError
from ..events import NullEventSink
from ..orchestrator.schemas import TaskStatus
from ..runtime import build_runtime, prepare_runtime
from ..safety.redaction import redact_text, sanitize_terminal_text
from ..safety.review_mode import ReviewMode
from ..storage.preferences import LocalPreferenceStore
from .console_renderer import ConsoleEventSink


def build_parser() -> argparse.ArgumentParser:
    """构建一次性任务 CLI，参数解析与业务执行保持分离。"""

    parser = argparse.ArgumentParser(description="立凯中枢本地最小智能体")
    parser.add_argument(
        "--review-mode",
        choices=[mode.value for mode in ReviewMode],
        default=None,
        help="任务审查模式；未指定时沿用本机已保存偏好，首次使用默认 strict",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="关闭实时过程展示，但不关闭人工审批提示",
    )
    parser.add_argument("request", nargs="+", help="要执行的一次任务描述")
    return parser


def _select_review_mode(settings: Settings, explicit_value: str | None):
    """按显式参数、本地偏好、strict 的优先级选择任务模式。"""

    store = LocalPreferenceStore(settings.preference_path)
    stored = store.load_review_mode()
    mode = ReviewMode(explicit_value) if explicit_value is not None else stored.mode
    mode = mode or ReviewMode.STRICT
    if explicit_value is not None and mode is not ReviewMode.FULL_ACCESS:
        store.save_review_mode(mode)
    reused_full_access = mode is ReviewMode.FULL_ACCESS and stored.mode is ReviewMode.FULL_ACCESS
    callback = None
    if mode is ReviewMode.FULL_ACCESS and not reused_full_access:
        callback = lambda: store.save_review_mode(ReviewMode.FULL_ACCESS)
    return mode, reused_full_access, callback, stored.warning


def main(argv: list[str] | None = None) -> int:
    """CLI 进程入口，返回明确退出码并把可定位错误输出到 stderr。"""

    args = build_parser().parse_args(argv)
    request_text = " ".join(args.request).strip()
    task_id = uuid.uuid4().hex
    runtime = None
    try:
        settings = Settings.from_env()
        for notice in prepare_runtime(settings):
            print(
                f"[提示] {redact_text(sanitize_terminal_text(notice))}",
                file=sys.stderr,
            )
        mode, full_access_confirmed, save_confirmation, preference_warning = _select_review_mode(
            settings, args.review_mode
        )
        if preference_warning:
            print(
                f"[提示] {redact_text(sanitize_terminal_text(preference_warning))}",
                file=sys.stderr,
            )
        event_sink = NullEventSink() if args.no_progress else ConsoleEventSink()
        runtime = build_runtime(
            settings,
            review_mode=mode,
            event_sink=event_sink,
            full_access_confirmed=full_access_confirmed,
            on_full_access_confirmed=save_confirmation,
        )
        result = asyncio.run(runtime.agent.run(request_text, task_id=task_id))
    except KeyboardInterrupt:
        if runtime is not None:
            try:
                runtime.tasks.set_status(
                    task_id,
                    TaskStatus.CANCELLED,
                    error_type="KeyboardInterrupt",
                    error_message="任务已取消：用户按下 Ctrl+C",
                )
            except KeyError as exc:
                print(
                    redact_text(sanitize_terminal_text(f"取消状态记录失败：任务 {task_id} 不存在：{exc}")),
                    file=sys.stderr,
                )
        print(redact_text(sanitize_terminal_text("任务已取消：用户按下 Ctrl+C")), file=sys.stderr)
        return 130
    except (ConfigError, ModelBackendError) as exc:
        print(
            redact_text(sanitize_terminal_text(f"启动失败：{type(exc).__name__}: {exc}")),
            file=sys.stderr,
        )
        return 2
    # CLI 边界统一兜住未预期异常，输出异常类型和启动阶段作为具体报错点。
    except Exception as exc:  # noqa: BLE001
        print(
            redact_text(sanitize_terminal_text(f"任务启动失败：{type(exc).__name__}: {exc}")),
            file=sys.stderr,
        )
        return 1

    print(f"任务 {result.task_id} 状态：{result.status.value}，模型轮数：{result.turns}")
    if result.content:
        print(redact_text(sanitize_terminal_text(result.content)))
    if result.error_message:
        print(redact_text(sanitize_terminal_text(result.error_message)), file=sys.stderr)
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
