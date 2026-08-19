"""bash 工具：由 ToolExecutor 调用，按审查模式执行经策略或确认的 Shell 命令。"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
from typing import Any

from ...errors import ToolExecutionError, ValidationError
from ...safety.approval import ApprovalRequest
from ...safety.redaction import (
    action_fingerprint,
    content_sha256,
    is_sensitive_key,
    redact_text,
    sanitize_terminal_text,
    truncate_text,
)
from ..base import Tool, ToolOutput
from ..context import ToolExecutionContext
from ..contracts import ToolDisplayField, ToolDisplayProjection, ToolSpec
from .common import require_arguments, require_string


class BashTool(Tool):
    """受控 Bash 工具，命令策略在进程启动前执行，输出和环境变量均做安全处理。"""

    name = "bash"
    spec = ToolSpec(
        name=name,
        description=(
            "按当前审查模式在 Git Bash 中执行受控命令或原始 Shell 脚本；"
            "需要保存脚本时默认使用工作区内的 script/ 目录。"
        ),
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

    def __init__(self, context: ToolExecutionContext) -> None:
        self.context = context
        self.settings = context.settings
        self.policy = context.commands
        self.review_mode = context.review_mode

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

    def approval_request(self, arguments: dict[str, Any]) -> ApprovalRequest | None:
        if not self.context.shell_requires_approval:
            return None
        fingerprint_input = {
            "command_sha256": content_sha256(arguments["command"]),
            "timeout_seconds": arguments["timeout_seconds"],
            "mode": self.context.review_mode.value,
        }
        fingerprint = action_fingerprint(fingerprint_input)
        return ApprovalRequest(
            action_type="bash",
            summary=(
                f"在工作区 {self.settings.workspace_root} 执行命令："
                f"{redact_text(arguments['command'])}（模式 {self.context.review_mode.value}，"
                f"超时 {arguments['timeout_seconds']} 秒）"
            ),
            fingerprint=fingerprint,
            audit_summary=(
                f"bash 动作：模式={self.context.review_mode.value}，"
                f"超时={arguments['timeout_seconds']} 秒，"
                f"command sha256={content_sha256(arguments['command'])}，"
                f"审批指纹={fingerprint}"
            ),
        )

    async def execute(
        self, arguments: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> ToolOutput:
        strict_mode = self.context.shell_uses_restricted_argv
        if strict_mode:
            argv = self._normalized_argv(arguments)
        else:
            self.policy.check(arguments["command"])
            argv = ()
        bash_path = self._find_bash()
        environment = self._safe_environment()
        environment["CI"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if strict_mode:
            command_path = self._find_command(argv[0])
            if command_path:
                environment["PATH"] = os.path.dirname(command_path)
            # 作用：严格模式只把策略后的 argv 交给 Bash，避免原始命令再次被解释。
            safe_script = shlex.join(("exec", *argv))
        else:
            # 宽松/完全访问模式明确保留原始脚本语义，审批或任务级确认是唯一权限门槛。
            safe_script = arguments["command"]
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
        display_metadata = {
            "stdout": self._safe_output_text(stdout),
            "stderr": self._safe_output_text(stderr),
            "exit_code": process.returncode,
            "truncated": truncated,
        }
        if reason == "timeout":
            message = self._bounded_message(
                f"Bash 执行超时：超过 {arguments['timeout_seconds']} 秒\n",
                output,
                truncated,
            )
            return ToolOutput(
                message,
                is_error=True,
                metadata=metadata,
                display_metadata=display_metadata,
            )
        if reason == "cancelled":
            return ToolOutput(
                "Bash 已取消：收到任务取消信号",
                is_error=True,
                metadata=metadata,
                display_metadata=display_metadata,
            )
        if process.returncode != 0:
            message = self._bounded_message(
                f"Bash 执行失败：退出码 {process.returncode}\n",
                output,
                truncated,
            )
            return ToolOutput(
                message,
                is_error=True,
                metadata=metadata,
                display_metadata=display_metadata,
            )
        message = self._bounded_message(
            "Bash 执行成功：退出码 0\n",
            output,
            truncated,
        )
        return ToolOutput(message, metadata=metadata, display_metadata=display_metadata)

    def display_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        command = values.get("command")
        if not isinstance(command, str):
            return "bash 指令不可用：command 不是字符串"
        lines = [command]
        timeout = values.get("timeout_seconds")
        if timeout is not None and timeout != self.settings.default_bash_timeout_seconds:
            lines.append(f"超时：{timeout} 秒")
        return "\n".join(lines)

    def display_result(self, output: ToolOutput | None) -> ToolDisplayProjection:
        if output is None:
            return ToolDisplayProjection((ToolDisplayField("退出码", None),))
        display = output.display_metadata
        stdout = display.get("stdout", "")
        stderr = display.get("stderr", "")
        fields = [ToolDisplayField("退出码", display.get("exit_code"))]
        if stdout:
            fields.append(ToolDisplayField("stdout", stdout))
        if stderr:
            fields.append(ToolDisplayField("stderr", stderr))
        if not stdout and not stderr:
            fields.append(ToolDisplayField("输出", "无输出"))
        if display.get("truncated"):
            fields.append(ToolDisplayField("输出状态", "已截断"))
        return ToolDisplayProjection(tuple(fields))

    def audit_arguments(self, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        projection = {
            "mode": self.context.review_mode.value,
            "command_sha256": content_sha256(values["command"])
            if isinstance(values.get("command"), str)
            else None,
            "timeout_seconds": values.get("timeout_seconds"),
        }
        return f"bash 参数摘要：指纹={action_fingerprint(projection)}，投影={projection}"

    def model_metadata(self, output: ToolOutput) -> dict[str, Any]:
        return {
            key: output.metadata[key]
            for key in ("exit_code", "truncated", "timed_out", "cancelled")
            if key in output.metadata
        }

    def audit_summary(self, output: ToolOutput) -> str:
        metadata = output.metadata
        return (
            f"bash {output.effective_status().label}：模式={self.context.review_mode.value}，"
            f"退出码={metadata.get('exit_code')}，超时={metadata.get('timed_out', False)}，"
            f"取消={metadata.get('cancelled', False)}，输出截断={metadata.get('truncated', False)}"
        )

    def _find_bash(self):
        bash_path = self.settings.bash_path
        if bash_path is None:
            bash_path = self.discover_bash_path()
            discovered = shutil.which("bash")
            if bash_path is None and self._is_wsl_path(discovered):
                raise ToolExecutionError(
                    "Bash 配置失败：PATH 中的 bash.exe 是 WSL 入口，不是 Git Bash；"
                    "请在 .env 设置 Git Bash 的 BASH_PATH"
                )
        if not bash_path:
            raise ToolExecutionError(
                "Bash 配置失败：未找到 Git Bash；请在 .env 设置 BASH_PATH"
            )
        path = os.fspath(bash_path)
        if not os.path.exists(path):
            raise ToolExecutionError(f"Bash 执行失败：配置的 BASH_PATH 不存在：{path}")
        if os.name == "nt" and self._is_wsl_path(path):
            raise ToolExecutionError(
                f"Bash 配置失败：BASH_PATH 指向 WSL 入口 {path}，请改为 Git Bash"
            )
        self._probe_runtime(path)
        return path

    def validate_runtime(self) -> None:
        """在 CLI 组装运行时阶段验证 Bash 身份和最小 Git 能力。"""

        self._find_bash()

    @classmethod
    def discover_bash_path(cls) -> str | None:
        """只自动发现 Git Bash，避免 Windows PATH 中的 WSL bash.exe 被误选。"""

        if os.name != "nt":
            return shutil.which("bash")
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
            os.path.join(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "Git",
                "bin",
                "bash.exe",
            ),
        ]
        discovered = shutil.which("bash")
        if discovered and not cls._is_wsl_path(discovered):
            candidates.append(discovered)
        for candidate in candidates:
            if os.path.isfile(candidate) and not cls._is_wsl_path(candidate):
                return os.path.abspath(candidate)
        return None

    @staticmethod
    def _is_wsl_path(path: object) -> bool:
        if not path:
            return False
        normalized = os.path.normcase(os.fspath(path)).replace("/", "\\")
        return "\\windowsapps\\" in normalized or normalized.endswith("\\system32\\bash.exe")

    def _probe_runtime(self, bash_path: str) -> None:
        """验证运行时能启动受控 Bash 并提供 Git，失败时报告具体路径和原因。"""

        try:
            result = subprocess.run(
                [bash_path, "--noprofile", "--norc", "-c", "command -v pwd && command -v git"],
                cwd=self.settings.workspace_root,
                env=self._safe_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolExecutionError(
                f"Bash 运行时探测失败：路径 {bash_path}，原因：{type(exc).__name__}"
            ) from exc
        if result.returncode != 0:
            raise ToolExecutionError(
                f"Bash 运行时不兼容：路径 {bash_path} 缺少 pwd/git，退出码 {result.returncode}"
            )

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        """移除密钥、启动脚本、Shell 函数和会改变命令语义的环境变量。"""

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
        return environment

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
            names = ("python.exe",) if executable == "python3" else (f"{executable}.exe",)
            for name in names:
                candidate = os.path.join(os.path.dirname(sys.executable), name)
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
            self._close_process_transport(process)

    @staticmethod
    def _close_process_transport(process) -> None:
        """在 Windows Proactor 事件循环关闭前释放已结束进程的管道传输。"""

        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()

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
        stdout_text = BashTool._safe_output_text(stdout)
        stderr_text = BashTool._safe_output_text(stderr)
        if stderr_text:
            return f"{stdout_text}\n[stderr]\n{stderr_text}" if stdout_text else f"[stderr]\n{stderr_text}"
        return stdout_text

    @staticmethod
    def _safe_output_text(value: bytes) -> str:
        """先移除终端控制字符，再执行凭据脱敏，供模型和界面复用安全文本。"""

        return redact_text(sanitize_terminal_text(value.decode("utf-8", errors="replace")))

    def _bounded_message(self, prefix: str, body: str, truncated: bool) -> str:
        """在一次预算内保留状态前缀、正文和截断标记，避免二次截断丢失提示。"""

        marker = "\n[输出已截断]" if truncated else ""
        if marker and len(marker.encode("utf-8")) > self.settings.max_output_bytes:
            marker = "[截断]"
        if marker and len(marker.encode("utf-8")) > self.settings.max_output_bytes:
            marker = "!"
        prefix, _ = truncate_text(
            prefix,
            max(0, self.settings.max_output_bytes - len(marker.encode("utf-8"))),
        )
        remaining = self.settings.max_output_bytes - len((prefix + marker).encode("utf-8"))
        body, _ = truncate_text(body, max(0, remaining))
        return prefix + body + marker
