"""bash 工具：在固定工作区以非交互 Git Bash 执行通过策略和审批的命令。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from typing import Any

from ...config import Settings
from ...errors import ToolExecutionError, ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.command_policy import CommandPolicy
from ...safety.redaction import is_sensitive_key, redact_text, truncate_text
from ..base import ToolOutput
from .common import require_arguments, require_string


class BashTool:
    """受控 Bash 工具，命令策略在进程启动前执行，输出和环境变量均做安全处理。"""

    name = "bash"
    spec = ToolSpec(
        name=name,
        description="在工作区内执行通过严格命令策略的 Git Bash 命令。",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "单条受控 Bash 命令"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "超时时间，不能超过配置上限",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    def __init__(self, settings: Settings, policy: CommandPolicy) -> None:
        self.settings = settings
        self.policy = policy

    def validate(self, arguments: object) -> dict[str, Any]:
        values = require_arguments(arguments, self.name)
        command = require_string(values, "command", self.name)
        timeout = values.get("timeout_seconds", self.settings.default_bash_timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValidationError("工具 bash 参数校验失败：timeout_seconds 必须是大于 0 的整数")
        if timeout > self.settings.max_bash_timeout_seconds:
            raise ValidationError(
                f"工具 bash 参数校验失败：timeout_seconds={timeout} 超过配置上限 {self.settings.max_bash_timeout_seconds}"
            )
        return {"command": command, "timeout_seconds": timeout}

    def check_safety(self, arguments: dict[str, Any]) -> None:
        self.policy.check(arguments["command"])

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(
            action_type="bash",
            summary=(
                f"在工作区 {self.settings.workspace_root} 执行命令："
                f"{redact_text(arguments['command'])}（超时 {arguments['timeout_seconds']} 秒）"
            ),
        )

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        bash_path = self._find_bash()
        environment = {
            key: value for key, value in os.environ.items() if not is_sensitive_key(key)
        }
        environment["CI"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            process = await asyncio.create_subprocess_exec(
                str(bash_path),
                "-lc",
                arguments["command"],
                cwd=self.settings.workspace_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise ToolExecutionError(
                f"Bash 启动失败：路径 {bash_path}，原因：{type(exc).__name__}: {exc}"
            ) from exc

        try:
            stdout, stderr, reason = await self._communicate(
                process, arguments["timeout_seconds"], cancel_event
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        output = self._format_output(stdout, stderr)
        output, truncated = truncate_text(output, self.settings.max_output_bytes)
        metadata: dict[str, Any] = {
            "exit_code": process.returncode,
            "truncated": truncated,
            "timed_out": reason == "timeout",
            "cancelled": reason == "cancelled",
        }
        if truncated:
            output += "\n[命令输出已截断：超过配置字节上限]"
        if reason == "timeout":
            message, _ = truncate_text(
                f"Bash 执行超时：超过 {arguments['timeout_seconds']} 秒\n{output}",
                self.settings.max_output_bytes,
            )
            return ToolOutput(
                message,
                is_error=True,
                metadata=metadata,
            )
        if reason == "cancelled":
            return ToolOutput("Bash 已取消：收到任务取消信号", is_error=True, metadata=metadata)
        if process.returncode != 0:
            message, _ = truncate_text(
                f"Bash 执行失败：退出码 {process.returncode}\n{output}",
                self.settings.max_output_bytes,
            )
            return ToolOutput(
                message,
                is_error=True,
                metadata=metadata,
            )
        message, _ = truncate_text(
            f"Bash 执行成功：退出码 0\n{output}", self.settings.max_output_bytes
        )
        return ToolOutput(message, metadata=metadata)

    def _find_bash(self):
        bash_path = self.settings.bash_path or shutil.which("bash")
        if not bash_path:
            raise ToolExecutionError("Bash 执行失败：未找到 bash，请设置 BASH_PATH")
        path = os.fspath(bash_path)
        if not os.path.exists(path):
            raise ToolExecutionError(f"Bash 执行失败：配置的 BASH_PATH 不存在：{path}")
        return path

    async def _communicate(self, process, timeout: int, cancel_event):
        communication = asyncio.create_task(process.communicate())
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event else None
        wait_set = {communication}
        if cancellation:
            wait_set.add(cancellation)
        try:
            done, _ = await asyncio.wait(wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                await self._terminate(process)
                stdout, stderr = await communication
                return stdout, stderr, "timeout"
            if cancellation and cancellation in done and cancellation.result():
                await self._terminate(process)
                stdout, stderr = await communication
                return stdout, stderr, "cancelled"
            stdout, stderr = await communication
            return stdout, stderr, ""
        finally:
            if cancellation and not cancellation.done():
                cancellation.cancel()

    async def _terminate(self, process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _format_output(stdout: bytes, stderr: bytes) -> str:
        stdout_text = redact_text(stdout.decode("utf-8", errors="replace"))
        stderr_text = redact_text(stderr.decode("utf-8", errors="replace"))
        if stderr_text:
            return f"{stdout_text}\n[stderr]\n{stderr_text}" if stdout_text else f"[stderr]\n{stderr_text}"
        return stdout_text
