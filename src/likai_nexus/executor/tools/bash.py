"""bash 工具：由 ToolExecutor 调用，在固定工作区执行经 CommandPolicy 和审批确认的命令。"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
from typing import Any

from ...config import Settings
from ...errors import ToolExecutionError, ValidationError
from ...orchestrator.schemas import ToolSpec
from ...safety.approval import ApprovalRequest
from ...safety.command_policy import CommandPolicy
from ...safety.redaction import (
    action_fingerprint,
    content_sha256,
    is_sensitive_key,
    redact_text,
    truncate_text,
)
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
        arguments["argv"] = self.policy.check(arguments["command"]).argv

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest:
        argv = self._normalized_argv(arguments)
        return ApprovalRequest(
            action_type="bash",
            summary=(
                f"在工作区 {self.settings.workspace_root} 执行命令："
                f"{redact_text(arguments['command'])}（超时 {arguments['timeout_seconds']} 秒）"
            ),
            fingerprint=action_fingerprint(
                {"argv": argv, "timeout_seconds": arguments["timeout_seconds"]}
            ),
            audit_summary=(
                f"bash 动作：argv={argv!r}，超时={arguments['timeout_seconds']} 秒，"
                f"command sha256={content_sha256(arguments['command'])}"
            ),
        )

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        argv = self._normalized_argv(arguments)
        bash_path = self._find_bash()
        command_path = self._find_command(argv[0])
        environment = {
            key: value for key, value in os.environ.items() if not is_sensitive_key(key)
        }
        for key in (
            "BASH_ENV",
            "ENV",
            "CDPATH",
            "GLOBIGNORE",
            "SHELLOPTS",
            "BASHOPTS",
            "PROMPT_COMMAND",
            "PS4",
        ):
            environment.pop(key, None)
        for key in tuple(environment):
            if key.startswith("BASH_FUNC_"):
                environment.pop(key, None)
        environment["CI"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if command_path:
            environment["PATH"] = os.path.dirname(command_path)
        # 作用：只把已通过策略的 argv 重新安全引用后交给 Bash，避免原始命令再次被解释。
        safe_script = shlex.join(("exec", *argv))
        try:
            process = await asyncio.create_subprocess_exec(
                str(bash_path),
                "--noprofile",
                "--norc",
                "-c",
                safe_script,
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
            stdout, stderr, reason, stream_truncated = await self._communicate(
                process, arguments["timeout_seconds"], cancel_event
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        output = self._format_output(stdout, stderr)
        output, formatted_truncated = truncate_text(output, self.settings.max_output_bytes)
        truncated = stream_truncated or formatted_truncated
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

    @staticmethod
    def _find_command(executable: str) -> str | None:
        """绑定允许命令的实际路径，并让子 Shell 只搜索该目录。"""

        command_path = shutil.which(executable)
        if command_path:
            return os.path.abspath(command_path)
        # Windows 下 pwd 是 Bash 内建命令，pytest/ruff 可能只在当前解释器的 Scripts 目录中。
        if executable == "pwd":
            return None
        if executable in {"python", "python3", "pytest", "ruff"}:
            candidate = os.path.join(os.path.dirname(sys.executable), f"{executable}.exe")
            if os.path.exists(candidate):
                return os.path.abspath(candidate)
        raise ToolExecutionError(f"Bash 执行失败：未找到允许命令 {executable}")

    def _normalized_argv(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        """重新校验并固定 argv，防止审批、执行两个阶段使用不同命令表示。"""

        decision = self.policy.check(arguments["command"])
        supplied = arguments.get("argv")
        if supplied is not None and tuple(supplied) != decision.argv:
            raise ValidationError("工具 bash 安全校验失败：审批后的 argv 与当前命令不一致")
        return decision.argv

    async def _communicate(self, process, timeout: int, cancel_event):
        communication = asyncio.create_task(self._collect_process(process))
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event else None
        wait_set = {communication}
        if cancellation:
            wait_set.add(cancellation)
        try:
            done, _ = await asyncio.wait(
                wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                await self._terminate(process)
                stdout, stderr, truncated = await self._finish_collection(communication)
                return stdout, stderr, "timeout", truncated
            if cancellation and cancellation in done and cancellation.result():
                await self._terminate(process)
                stdout, stderr, truncated = await self._finish_collection(communication)
                return stdout, stderr, "cancelled", truncated
            stdout, stderr, truncated = await communication
            return stdout, stderr, "", truncated
        finally:
            if cancellation and not cancellation.done():
                cancellation.cancel()
            if not communication.done():
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)

    async def _collect_process(self, process):
        """并行排空 stdout/stderr，且只保留配置上限内的字节。"""

        stdout = bytearray()
        stderr = bytearray()
        state = {"remaining": self.settings.max_output_bytes, "truncated": False}
        tasks = [
            asyncio.create_task(self._drain_stream(process.stdout, stdout, state)),
            asyncio.create_task(self._drain_stream(process.stderr, stderr, state)),
            asyncio.create_task(process.wait()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return bytes(stdout), bytes(stderr), bool(state["truncated"])

    async def _drain_stream(self, stream, target: bytearray, state: dict[str, Any]) -> None:
        """持续读取一个管道，即使达到上限也继续消费，避免子进程被管道反压卡住。"""

        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            remaining = state["remaining"]
            if remaining > 0:
                target.extend(chunk[:remaining])
                state["remaining"] = remaining - min(len(chunk), remaining)
            if len(chunk) > remaining:
                state["truncated"] = True

    async def _finish_collection(self, communication):
        """进程终止后限时回收剩余输出，避免异常子进程让工具永久等待。"""

        try:
            return await asyncio.wait_for(communication, timeout=5)
        except TimeoutError:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            return b"", b"", True

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
