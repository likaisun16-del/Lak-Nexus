"""本地 CLI：负责参数接收、审批交互和结果展示，不直接执行文件或进程。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from ..config import Settings
from ..errors import ConfigError, ModelBackendError, SessionError
from ..events import NullEventSink
from ..orchestrator.schemas import TaskStatus
from ..runtime import build_runtime, build_session_service, prepare_runtime
from ..safety.redaction import redact_text, sanitize_terminal_text
from ..safety.review_mode import ReviewMode
from ..storage.preferences import LocalPreferenceStore
from .console_renderer import ConsoleEventSink

_COMMIT_BOUNDARY_NOTICE = (
    "注意：Commit SHA 只代表已提交的 Git 内容，不会撤销未提交文件、数据库、网络、"
    "系统或外部服务副作用。"
)


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
    parser.add_argument(
        "--session-id",
        default=None,
        help="将本次任务加入指定 Session；未指定时沿用本机活动 Session",
    )
    parser.add_argument("request", nargs="+", help="要执行的一次任务描述")
    return parser


def _build_session_parser() -> argparse.ArgumentParser:
    """构建 Session 管理子命令解析器，业务规则仍由 SessionService 承担。"""

    parser = argparse.ArgumentParser(prog="likai-nexus session", description="会话树管理")
    commands = parser.add_subparsers(dest="command", required=True)
    new_parser = commands.add_parser("new", help="新建会话")
    new_parser.add_argument("--title", default="新会话", help="会话初始标题")
    commands.add_parser("list", help="列出会话")
    history = commands.add_parser("history", help="查看当前活动分支")
    history.add_argument("session_id")
    branches = commands.add_parser("branches", help="查看分支点和叶子")
    branches.add_argument("session_id")
    continue_from = commands.add_parser("continue-from", help="从历史消息切换活动分支")
    continue_from.add_argument("message_id")
    commit = commands.add_parser("commit", help="查看 assistant 消息的 Commit SHA")
    commit.add_argument("message_id")
    switch = commands.add_parser("switch", help="切换本机活动 Session")
    switch.add_argument("session_id")
    delete = commands.add_parser("delete", help="删除 Session 及消息树")
    delete.add_argument("session_id")
    delete.add_argument(
        "--confirm",
        action="store_true",
        help="明确确认删除；不提供时交互输入 DELETE_SESSION",
    )
    return parser


def _session_command_index(argv: list[str]) -> int | None:
    """识别位于全局选项之后的 session 命令，保留旧版自然语言入口。"""

    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--no-progress":
            index += 1
            continue
        if token in {"--review-mode", "--session-id"}:
            index += 2
            continue
        return index if token == "session" else None
    return None


def _run_session_command(argv: list[str]) -> int:
    """执行无需模型调用的 Session 管理命令。"""

    command_index = _session_command_index(argv)
    if command_index is None:
        return 2
    args = _build_session_parser().parse_args(argv[command_index + 1 :])
    try:
        settings = Settings.from_env()
        for notice in prepare_runtime(settings):
            print(f"[提示] {redact_text(sanitize_terminal_text(notice))}", file=sys.stderr)
        service = build_session_service(settings)
        preferences = LocalPreferenceStore(settings.preference_path)
        if args.command == "new":
            session = service.create(args.title)
            preferences.save_active_session_id(session["session_id"])
            print(f"Session {session['session_id']}：{session['title']}")
            return 0
        if args.command == "list":
            for session in service.list():
                print(
                    f"{session['session_id']}\t{session['title']}\t最近消息："
                    f"{session['last_message_at']}"
                )
            return 0
        if args.command == "history":
            return _print_session_history(service, args.session_id)
        if args.command == "branches":
            return _print_session_branches(service, args.session_id)
        if args.command == "continue-from":
            session_id = preferences.load_active_session_id()
            if not session_id:
                raise SessionError("继续会话失败：本机尚未选择活动 Session")
            service.continue_from(session_id, args.message_id)
            print(f"活动分支已切换：Session={session_id}，消息={args.message_id}")
            return 0
        if args.command == "commit":
            message = service.repository.get_message(args.message_id)
            if message is None:
                raise SessionError(f"Commit 查询失败：消息不存在：{args.message_id}")
            association = service.commit_for_message(args.message_id)
            if association is None:
                reason = message.get("version_reason") or "未记录版本：没有可用的结果 Commit"
                task_text = f"Task={message['task_id']}，" if message.get("task_id") else ""
                print(f"消息 {args.message_id}：{task_text}{reason}")
            else:
                print(
                    f"消息 {args.message_id}：Task={association['task_id']}，"
                    f"Commit SHA={association['commit_sha']}"
                )
            print(_COMMIT_BOUNDARY_NOTICE)
            return 0
        if args.command == "switch":
            session = service.switch(args.session_id)
            preferences.save_active_session_id(session["session_id"])
            print(f"活动 Session 已切换：{session['session_id']}：{session['title']}")
            return 0
        if args.command == "delete":
            if not _confirm_session_delete(args.session_id, args.confirm):
                print("已取消删除")
                return 1
            deleted = service.delete(args.session_id)
            if not deleted:
                raise SessionError(f"会话删除失败：Session 不存在：{args.session_id}")
            if preferences.load_active_session_id() == args.session_id:
                preferences.clear_active_session()
            print(f"Session 已删除：{args.session_id}")
            return 0
    except (ConfigError, SessionError) as exc:
        print(
            redact_text(sanitize_terminal_text(f"Session 命令失败：{type(exc).__name__}: {exc}")),
            file=sys.stderr,
        )
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Session 命令已取消：未完成确认", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(
            redact_text(sanitize_terminal_text(f"Session 命令启动失败：{type(exc).__name__}: {exc}")),
            file=sys.stderr,
        )
        return 1
    return 2


def _print_session_history(service, session_id: str) -> int:
    """只展示当前活动路径中的可见消息和稳定标识。"""

    session = service.get(session_id)
    if session is None:
        raise SessionError(f"历史读取失败：Session 不存在：{session_id}")
    print(f"Session {session_id}：{session['title']}")
    for message in service.history(session_id):
        task_text = f"，Task={message['task_id']}" if message.get("task_id") else ""
        status_text = (
            f"，状态={message['execution_status']}"
            if message.get("execution_status")
            else ""
        )
        retry_text = (
            f"，重试自={message['retry_of_message_id']}"
            if message.get("retry_of_message_id")
            else ""
        )
        print(
            f"[{message['role']}] {message['message_id']}{task_text}"
            f"{status_text}{retry_text}：{message['content']}"
        )
        if message["role"] == "assistant":
            association = service.commit_for_message(message["message_id"])
            if association:
                print(
                    f"  版本：Task={association['task_id']}，"
                    f"Commit SHA={association['commit_sha']}"
                )
            else:
                print(
                    f"  版本：{message.get('version_reason') or '未记录版本：没有可用的结果 Commit'}"
                )
            print(f"  {_COMMIT_BOUNDARY_NOTICE}")
    return 0


def _print_session_branches(service, session_id: str) -> int:
    """展示分支点和可切换叶子，不复制隐藏工具轨迹。"""

    branches = service.branches(session_id)
    for branch in branches["branch_points"]:
        print(f"分支点 {branch['message_id']}：{branch['content']}")
        for child in service.repository.list_children(branch["message_id"]):
            print(f"  子节点 {child['message_id']}：{child['role']}：{child['content']}")
    print("叶子：")
    for leaf in branches["leaves"]:
        marker = "（活动）" if leaf["is_active"] else ""
        print(f"  {leaf['message_id']}：{leaf['role']}：{leaf['content']}{marker}")
    return 0


def _confirm_session_delete(session_id: str, confirmed: bool) -> bool:
    """删除必须经过显式标志或精确交互口令。"""

    if confirmed:
        return True
    answer = input(f"确认删除 Session {session_id}？请输入 DELETE_SESSION：")
    return answer.strip() == "DELETE_SESSION"


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

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _session_command_index(raw_argv) is not None:
        return _run_session_command(raw_argv)
    args = build_parser().parse_args(raw_argv)
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
        if runtime.sessions is None:
            raise SessionError("任务启动失败：运行时未配置 Session 服务")
        session_id = _resolve_session_id(runtime.sessions, settings, args.session_id)
        session_result = asyncio.run(
            runtime.sessions.ask(session_id, request_text, task_id=task_id)
        )
        result = session_result.result
        if result.status is not TaskStatus.CANCELLED:
            LocalPreferenceStore(settings.preference_path).save_active_session_id(session_id)
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
    except (ConfigError, ModelBackendError, SessionError) as exc:
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
    if 'session_result' in locals():
        print(f"Session {session_result.session_id}，消息 {session_result.user_message_id}")
        if session_result.commit_sha:
            print(f"Commit SHA：{session_result.commit_sha}")
        elif session_result.commit_reason:
            print(session_result.commit_reason)
        if session_result.commit_sha or session_result.commit_reason:
            print(_COMMIT_BOUNDARY_NOTICE)
    if result.content:
        print(redact_text(sanitize_terminal_text(result.content)))
    if result.error_message:
        print(redact_text(sanitize_terminal_text(result.error_message)), file=sys.stderr)
    return 0 if result.status.value == "success" else 1


def _resolve_session_id(service, settings: Settings, explicit_session_id: str | None) -> str:
    """选择显式、偏好或新建的 Session；调用方在任务完成后持久化选择。"""

    preferences = LocalPreferenceStore(settings.preference_path)
    if explicit_session_id:
        return service.switch(explicit_session_id)["session_id"]
    stored_id = preferences.load_active_session_id()
    if stored_id and service.get(stored_id) is not None:
        return stored_id
    session = service.create()
    return session["session_id"]


if __name__ == "__main__":
    raise SystemExit(main())
