"""工作区路径安全：供文件工具共享，解析路径并按审查模式控制越界和敏感文件访问。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import PathAccessError, ToolExecutionError, ValidationError
from .review_mode import ReviewMode, parse_review_mode

_SENSITIVE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_DIRECTORIES = {".aws", ".gnupg", ".ssh", "private"}
_SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """安全解析后的绝对路径及其工作区相对展示名。"""

    path: Path
    relative_path: str
    exists: bool
    sensitive: bool = False


class WorkspaceAccessPolicy:
    """共享工作区资源策略，供文件工具和 Bash 的显式路径参数复用。"""

    @staticmethod
    def is_sensitive_path(path: object) -> bool:
        """判断路径任一组件是否指向环境文件、凭据、私钥或敏感目录。"""

        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str):
            return False
        for part in path.replace("\\", "/").split("/"):
            name = part.lower()
            if not name or name == ".":
                continue
            if (
                name.startswith(".env")
                or name in _SENSITIVE_NAMES
                or name in _SENSITIVE_DIRECTORIES
                or name.endswith(_SENSITIVE_SUFFIXES)
            ):
                return True
        return False

    @classmethod
    def rg_exclude_globs(cls) -> tuple[str, ...]:
        """从同一敏感资源规则生成 ripgrep 排除模式，避免两套名单漂移。"""

        patterns = ["!.env*", "!**/.env*"]
        patterns.extend(f"!**/{name}" for name in sorted(_SENSITIVE_NAMES))
        patterns.extend(f"!**/{directory}/**" for directory in sorted(_SENSITIVE_DIRECTORIES))
        patterns.extend(f"!**/*{suffix}" for suffix in _SENSITIVE_SUFFIXES)
        return tuple(patterns)


class WorkspacePathResolver:
    """所有文件工具共享的工作区路径解析器。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        review_mode: ReviewMode = ReviewMode.STRICT,
        allow_external: bool = False,
        allow_sensitive: bool = False,
        enforce_symlink_safety: bool = True,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not root.exists() or not root.is_dir():
            raise PathAccessError(f"工作区初始化失败：根目录不存在或不是目录：{root}")
        self.root = root
        self.review_mode = parse_review_mode(review_mode)
        self.allow_external = allow_external
        self.allow_sensitive = allow_sensitive
        self.enforce_symlink_safety = enforce_symlink_safety

    def resolve(
        self,
        raw_path: object,
        *,
        require_exists: bool = False,
        file_only: bool = False,
        reject_symlink: bool = False,
    ) -> ResolvedPath:
        """解析并验证路径，所有失败信息都包含输入路径和失败原因。"""

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValidationError("路径校验失败：path 必须是非空字符串")
        input_path = Path(raw_path).expanduser()
        lexical_path = input_path if input_path.is_absolute() else self.root / input_path
        try:
            resolved = lexical_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathAccessError(
                f"路径校验失败：无法解析 {raw_path!r}，原因：{type(exc).__name__}"
            ) from exc

        if not self.allow_external:
            try:
                common = os.path.commonpath(
                    (os.path.normcase(str(self.root)), os.path.normcase(str(resolved)))
                )
            except ValueError as exc:
                raise PathAccessError(
                    f"路径访问被拒绝：{raw_path!r} 与工作区不在同一文件系统"
                ) from exc
            if common != os.path.normcase(str(self.root)):
                raise PathAccessError(
                    f"路径访问被拒绝：{raw_path!r} 解析后位于工作区外：{resolved}"
                )
        if reject_symlink and self.enforce_symlink_safety and self._contains_link(lexical_path):
            raise PathAccessError(f"路径访问被拒绝：不允许通过符号链接或目录连接访问：{raw_path!r}")
        exists = resolved.exists()
        relative = self._relative(resolved)
        sensitive = self._is_sensitive_path(relative)
        if sensitive and not self.allow_sensitive:
            raise PathAccessError(
                f"路径访问被拒绝：目标 {relative} 可能包含密钥或凭据，默认禁止工具访问"
            )
        if require_exists and not exists:
            raise ToolExecutionError(f"文件操作失败：目标不存在：{relative}")
        if file_only and exists and not resolved.is_file():
            raise ToolExecutionError(f"文件操作失败：目标不是普通文件：{relative}")
        return ResolvedPath(resolved, relative, exists, sensitive)

    @staticmethod
    def _is_sensitive_path(path: object) -> bool:
        """默认拒绝环境文件、私钥和凭据文件，避免工具把密钥送入模型上下文。"""

        return WorkspaceAccessPolicy.is_sensitive_path(path)

    def _contains_link(self, path: Path) -> bool:
        """检查输入路径到工作区根之间的符号链接和 Windows 目录连接。"""

        current = path
        while current != self.root:
            if self._is_link(current):
                return True
            parent = current.parent
            if parent == current:
                break
            current = parent
        return False

    @staticmethod
    def _is_link(path: Path) -> bool:
        """兼容检查普通符号链接和 Python 3.12 的目录连接。"""

        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())

    def _relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            if not self.allow_external:
                raise PathAccessError(f"路径访问被拒绝：无法生成工作区相对路径：{path}")
            return path.as_posix()
        return relative or "."
