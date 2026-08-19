"""运行配置：加载工作区、数据库、Bash 和模型参数，供各层共享而不散落读取环境变量。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# read 正文必须为最终模型状态信封预留空间；Registry 用同一常量计算实际正文预算。
READ_STATUS_RESERVE_BYTES = 128
MIN_READ_BYTES = 4
MIN_OUTPUT_BYTES = READ_STATUS_RESERVE_BYTES + MIN_READ_BYTES
DEFAULT_OUTPUT_BYTES = 64 * 1024
DEFAULT_READ_BYTES = DEFAULT_OUTPUT_BYTES - READ_STATUS_RESERVE_BYTES


def _positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigError(f"配置错误：{name} 必须是正整数，请检查配置值格式") from exc
    if result <= 0:
        raise ConfigError(f"配置错误：{name} 必须大于 0，实际值为 {result}")
    return result


def _load_dotenv(path: Path) -> dict[str, str]:
    """读取当前工作目录的 .env，并在格式错误时报告具体行号。"""

    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"配置错误：无法读取 .env 文件 {path}，原因：{type(exc).__name__}"
        ) from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(
                f"配置错误：.env 第 {line_number} 行格式无效，必须使用 KEY=VALUE"
            )
        key, value = (part.strip() for part in line.split("=", 1))
        if not _ENV_KEY.fullmatch(key):
            raise ConfigError(f"配置错误：.env 第 {line_number} 行变量名无效：{key!r}")
        values[key] = _parse_dotenv_value(value, path, line_number)
    return values


def _parse_dotenv_value(value: str, path: Path, line_number: int) -> str:
    """解析未转义的单引号、双引号和行尾注释，保持实现轻量。"""

    if not value or value[0] not in {"'", '"'}:
        return value.split(" #", 1)[0].rstrip()
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise ConfigError(f"配置错误：.env 第 {line_number} 行引号未闭合：{path}")
    return value[1:-1]


@dataclass(frozen=True, slots=True)
class Settings:
    """所有运行组件使用的不可变配置。"""

    workspace_root: Path
    project_root: Path | None = None
    database_path: Path | None = None
    bash_path: Path | None = None
    default_bash_timeout_seconds: int = 30
    max_bash_timeout_seconds: int = 120
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES
    max_read_lines: int = 2_000
    max_read_bytes: int = DEFAULT_READ_BYTES
    max_turns: int = 20
    model_timeout_seconds: int = 60
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    api_base_url: str = "https://api.openai.com/v1"
    _use_default_database: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve(strict=False)
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"配置错误：WORKSPACE_ROOT 不是已存在的目录：{root}")
        object.__setattr__(self, "workspace_root", root)

        project = Path(self.project_root or Path.cwd()).expanduser().resolve(strict=False)
        if not project.exists() or not project.is_dir():
            raise ConfigError(f"配置错误：项目根目录不是已存在的目录：{project}")
        object.__setattr__(self, "project_root", project)

        database_explicit = self.database_path is not None
        database = self.database_path or project / "data" / "likai_nexus.db"
        database = Path(database).expanduser()
        if not database.is_absolute():
            database = project / database
        database = database.resolve(strict=False)
        object.__setattr__(self, "database_path", database)
        canonical_database = (project / "data" / "likai_nexus.db").resolve(strict=False)
        use_default_database = not database_explicit or database == canonical_database
        object.__setattr__(self, "_use_default_database", use_default_database)

        if database_explicit and self._is_within(database, root):
            raise ConfigError(
                "配置错误：显式 DATABASE_PATH 不能位于 WORKSPACE_ROOT 内部，"
                f"当前路径为 {database}；请将应用数据放到项目根目录 data/"
            )

        if self.default_bash_timeout_seconds > self.max_bash_timeout_seconds:
            raise ConfigError(
                "配置错误：DEFAULT_BASH_TIMEOUT_SECONDS 不能大于 MAX_BASH_TIMEOUT_SECONDS"
            )
        for name in (
            "default_bash_timeout_seconds",
            "max_bash_timeout_seconds",
            "max_output_bytes",
            "max_read_lines",
            "max_read_bytes",
            "max_turns",
            "model_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"配置错误：{name} 必须大于 0")
        if self.max_output_bytes < MIN_OUTPUT_BYTES:
            raise ConfigError(
                f"配置错误：MAX_OUTPUT_BYTES 至少为 {MIN_OUTPUT_BYTES}，"
                "才能同时容纳 read 正文和状态信封"
            )
        if self.max_read_bytes < MIN_READ_BYTES:
            raise ConfigError(
                f"配置错误：MAX_READ_BYTES 至少为 {MIN_READ_BYTES}，才能保证截断游标可推进"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """先加载当前目录 .env，再用进程环境变量覆盖，并给出具体缺失项。"""

        project_root = Path.cwd().resolve(strict=False)
        if environ is None:
            values = _load_dotenv(project_root / ".env")
            values.update(os.environ)
        else:
            values = dict(environ)
        workspace_value = values.get("WORKSPACE_ROOT")
        if not workspace_value:
            raise ConfigError("配置错误：缺少 WORKSPACE_ROOT，请指定允许智能体访问的工作区目录")

        database_value = values.get("DATABASE_PATH")
        database = Path(database_value) if database_value else None
        bash_value = values.get("BASH_PATH")
        bash_path = Path(bash_value).expanduser() if bash_value else None
        default_timeout = _positive_int(
            values.get("DEFAULT_BASH_TIMEOUT_SECONDS", "30"),
            "DEFAULT_BASH_TIMEOUT_SECONDS",
        )
        max_timeout = _positive_int(
            values.get("MAX_BASH_TIMEOUT_SECONDS", "120"),
            "MAX_BASH_TIMEOUT_SECONDS",
        )
        return cls(
            workspace_root=Path(workspace_value),
            project_root=project_root,
            database_path=database,
            bash_path=bash_path,
            default_bash_timeout_seconds=default_timeout,
            max_bash_timeout_seconds=max_timeout,
            max_output_bytes=_positive_int(
                values.get("MAX_OUTPUT_BYTES", str(DEFAULT_OUTPUT_BYTES)), "MAX_OUTPUT_BYTES"
            ),
            max_read_lines=_positive_int(
                values.get("MAX_READ_LINES", "2000"), "MAX_READ_LINES"
            ),
            max_read_bytes=_positive_int(
                values.get("MAX_READ_BYTES", str(DEFAULT_READ_BYTES)), "MAX_READ_BYTES"
            ),
            max_turns=_positive_int(values.get("MAX_TURNS", "20"), "MAX_TURNS"),
            model_timeout_seconds=_positive_int(
                values.get("MODEL_TIMEOUT_SECONDS", "60"), "MODEL_TIMEOUT_SECONDS"
            ),
            api_key=values.get("OPENAI_API_KEY"),
            model=values.get("MODEL", "gpt-4o-mini"),
            api_base_url=values.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    @property
    def data_root(self) -> Path:
        """项目根目录下保存数据库和本地偏好的应用数据目录。"""

        return self.project_root / "data"

    @property
    def use_default_database(self) -> bool:
        """表示是否应执行默认数据库的旧路径迁移。"""

        return self._use_default_database

    @property
    def preference_path(self) -> Path:
        """本地审查模式偏好文件路径。"""

        return self.data_root / "preferences.json"

    @property
    def script_root(self) -> Path:
        """工作区内供模型保存脚本的默认目录。"""

        return self.workspace_root / "script"

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            common = os.path.commonpath(
                (os.path.normcase(str(path)), os.path.normcase(str(root)))
            )
            return common == os.path.normcase(str(root))
        except ValueError:
            return False
