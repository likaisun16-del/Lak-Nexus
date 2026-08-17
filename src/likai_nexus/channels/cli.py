"""本地 CLI：负责参数接收、审批交互和结果展示，不直接执行文件或进程。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from ..config import Settings
from ..errors import ConfigError, ModelBackendError
from ..orchestrator.schemas import TaskStatus
from ..runtime import build_runtime


def build_parser() -> argparse.ArgumentParser:
    """构建一次性任务 CLI，参数解析与业务执行保持分离。"""

    parser = argparse.ArgumentParser(description="立凯中枢本地最小四工具智能体")
    parser.add_argument("request", nargs="+", help="要执行的一次任务描述")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 进程入口，返回明确退出码并把可定位错误输出到 stderr。"""

    args = build_parser().parse_args(argv)
    request_text = " ".join(args.request).strip()
    task_id = uuid.uuid4().hex
    runtime = None
    try:
        settings = Settings.from_env()
        runtime = build_runtime(settings)
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
                print(f"取消状态记录失败：任务 {task_id} 不存在：{exc}", file=sys.stderr)
        print("任务已取消：用户按下 Ctrl+C", file=sys.stderr)
        return 130
    except (ConfigError, ModelBackendError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    # CLI 边界统一兜住未预期异常，输出异常类型和启动阶段作为具体报错点。
    except Exception as exc:  # noqa: BLE001
        print(f"任务启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"任务 {result.task_id} 状态：{result.status.value}")
    if result.content:
        print(result.content)
    if result.error_message:
        print(result.error_message, file=sys.stderr)
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
