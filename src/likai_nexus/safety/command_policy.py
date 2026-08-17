"""Bash 命令策略：为 BashTool 解析并限制本地检查命令，拒绝组合、网络和破坏性语法。"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..errors import CommandDeniedError, NexusError, ValidationError
from .paths import WorkspaceAccessPolicy, WorkspacePathResolver

# 作用：阻断会让原始字符串重新进入 Shell 解释器的语法；Bash 工具随后只执行 argv。
_META = (
    ";",
    "&&",
    "||",
    "|",
    ">",
    "<",
    "`",
    "\n",
    "\r",
    "$",
    "{",
    "}",
    "*",
    "?",
    "[",
    "]",
    "~",
)
_NETWORK_COMMANDS = {"curl", "wget", "nc", "ncat", "ssh", "scp", "sftp", "telnet", "ftp"}
_DANGEROUS_GIT = {"clone", "fetch", "pull", "push", "remote", "submodule"}
_SAFE_OPTION = {"-a", "-l", "-la", "-al", "-n", "-i", "-L", "--files", "--short"}
_RG_PROTECTED_ARGS = (
    "--no-follow",
    "--glob",
    "!.env*",
    "--glob",
    "!**/.env*",
    "--glob",
    "!**/.netrc",
    "--glob",
    "!**/.npmrc",
    "--glob",
    "!**/.pypirc",
    "--glob",
    "!**/credentials",
    "--glob",
    "!**/credentials.json",
    "--glob",
    "!**/secrets",
    "--glob",
    "!**/secrets.json",
    "--glob",
    "!**/id_rsa",
    "--glob",
    "!**/id_ed25519",
    "--glob",
    "!**/*.pem",
    "--glob",
    "!**/*.p12",
    "--glob",
    "!**/*.pfx",
    "--glob",
    "!**/*.key",
    "--glob",
    "!**/.aws/**",
    "--glob",
    "!**/.gnupg/**",
    "--glob",
    "!**/.ssh/**",
    "--glob",
    "!**/private/**",
)


@dataclass(frozen=True, slots=True)
class CommandDecision:
    """命令策略评估结果，便于测试和向调用方说明拒绝原因。"""

    allowed: bool
    executable: str
    reason: str
    argv: tuple[str, ...] = ()


class CommandPolicy:
    """严格模式命令策略，不负责启动进程。"""

    def __init__(self, resolver: WorkspacePathResolver | None = None) -> None:
        self.resolver = resolver

    def evaluate(self, command: object) -> CommandDecision:
        if not isinstance(command, str) or not command.strip():
            return CommandDecision(False, "", "命令校验失败：command 必须是非空字符串")
        if any(marker in command for marker in _META):
            return CommandDecision(False, "", "命令被拒绝：不允许 Shell 组合、管道、重定向或多行语法")
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            return CommandDecision(False, "", f"命令解析失败：引号不完整或语法无效：{exc}")
        if not tokens:
            return CommandDecision(False, "", "命令校验失败：解析后没有可执行内容")
        executable_token = tokens[0]
        if "/" in executable_token or "\\" in executable_token or re.match(
            r"^[A-Za-z]:", executable_token
        ):
            return CommandDecision(False, executable_token, "命令被拒绝：不允许通过绝对路径指定可执行文件")
        executable = PurePosixPath(executable_token).name.lower().removesuffix(".exe")
        if executable in _NETWORK_COMMANDS:
            return CommandDecision(False, executable, "命令被拒绝：网络和远程连接命令不在允许范围")
        validators = {
            "pwd": self._pwd,
            "ls": self._ls,
            "rg": self._rg,
            "git": self._git,
            "pytest": self._pytest,
            "ruff": self._ruff,
            "python": self._python,
            "python3": self._python,
        }
        validator = validators.get(executable)
        if validator is None:
            return CommandDecision(False, executable, "命令被拒绝：可执行文件不在最小允许列表")
        try:
            validator(tokens[1:])
        except (CommandDeniedError, ValidationError) as exc:
            return CommandDecision(False, executable, str(exc))
        argv = tuple(tokens)
        if executable == "rg":
            argv += _RG_PROTECTED_ARGS
        return CommandDecision(
            True,
            executable,
            "命令通过最小允许列表，仍需要人工审批",
            argv,
        )

    def check(self, command: object) -> CommandDecision:
        """校验命令，失败时抛出带具体命令原因的拒绝异常。"""

        decision = self.evaluate(command)
        if not decision.allowed:
            raise CommandDeniedError(decision.reason)
        return decision

    @staticmethod
    def _pwd(args: list[str]) -> None:
        if args:
            raise CommandDeniedError("命令被拒绝：pwd 不接受额外参数")

    def _ls(self, args: list[str]) -> None:
        for token in args:
            if token.startswith("-") and token not in _SAFE_OPTION:
                raise CommandDeniedError(f"命令被拒绝：ls 选项不允许：{token}")
            if not token.startswith("-"):
                self._safe_path_token(token)

    def _rg(self, args: list[str]) -> None:
        if not args:
            raise CommandDeniedError("命令被拒绝：rg 必须指定搜索模式或 --files")
        expect_glob = False
        pattern_seen = False
        for token in args:
            if expect_glob:
                self._safe_path_token(token)
                expect_glob = False
                continue
            if token in {"-n", "-i", "-l", "--files"}:
                continue
            if token in {"-g", "--glob"}:
                expect_glob = True
                continue
            if token.startswith("-"):
                raise CommandDeniedError(f"命令被拒绝：rg 选项不允许：{token}")
            if not pattern_seen and "--files" not in args:
                pattern_seen = True
                continue
            self._safe_path_token(token)
        if expect_glob:
            raise CommandDeniedError("命令被拒绝：rg 的 -g/--glob 缺少模式")

    def _git(self, args: list[str]) -> None:
        if not args or args[0] in _DANGEROUS_GIT:
            subcommand = args[0] if args else ""
            raise CommandDeniedError(f"命令被拒绝：git 子命令不允许：{subcommand or '缺失'}")
        if args[0] == "status":
            if args[1:] not in ([], ["--short"]):
                raise CommandDeniedError("命令被拒绝：git status 只允许无参数或 --short")
            return
        if args[0] == "diff":
            allowed = {"--stat", "--name-only", "--check"}
            if len(args) == 1 or any(token not in allowed for token in args[1:]):
                raise CommandDeniedError("命令被拒绝：git diff 只允许只读展示选项")
            return
        raise CommandDeniedError(f"命令被拒绝：git 子命令不允许：{args[0]}")

    def _pytest(self, args: list[str]) -> None:
        allowed_prefixes = ("--maxfail=", "-k=")
        for token in args:
            if token in {"-q", "-x", "-s"} or token.startswith(allowed_prefixes):
                continue
            if token.startswith("-"):
                raise CommandDeniedError(f"命令被拒绝：pytest 选项不允许：{token}")
            self._safe_path_token(token)

    def _ruff(self, args: list[str]) -> None:
        if not args or args[0] != "check":
            raise CommandDeniedError("命令被拒绝：ruff 只允许 ruff check")
        for token in args[1:]:
            if token.startswith("-"):
                raise CommandDeniedError(f"命令被拒绝：ruff 选项不允许：{token}")
            self._safe_path_token(token)

    def _python(self, args: list[str]) -> None:
        if len(args) < 3 or args[:2] != ["-m", "compileall"]:
            raise CommandDeniedError("命令被拒绝：python 只允许 python -m compileall <工作区路径>")
        for token in args[2:]:
            self._safe_path_token(token)

    def _safe_path_token(self, token: str) -> None:
        if token in {".", "./"}:
            return
        if token.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", token):
            raise CommandDeniedError(f"命令被拒绝：参数不能访问绝对路径：{token}")
        if any(part == ".." for part in token.replace("\\", "/").split("/")):
            raise CommandDeniedError(f"命令被拒绝：参数不能包含工作区逃逸路径：{token}")
        if "\x00" in token:
            raise ValidationError("命令校验失败：参数包含 NUL 字符")
        if WorkspaceAccessPolicy.is_sensitive_path(token):
            raise CommandDeniedError(f"命令被拒绝：参数不能访问敏感资源：{token}")
        if self.resolver is not None:
            try:
                self.resolver.resolve(token, reject_symlink=True)
            except NexusError as exc:
                raise CommandDeniedError(
                    f"命令被拒绝：显式路径无法安全解析：{token}，原因：{exc}"
                ) from exc
