"""运行配置：加载工作区、数据库、Bash 和模型参数，供各层共享而不散落读取环境变量。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigError(f"配置错误：{name} 必须是正整数，实际值为 {value!r}") from exc
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
    database_path: Path | None = None
    bash_path: Path | None = None
    default_bash_timeout_seconds: int = 30
    max_bash_timeout_seconds: int = 120
    max_output_bytes: int = 64 * 1024
    max_read_lines: int = 2_000
    max_read_bytes: int = 64 * 1024
    max_turns: int = 20
    model_timeout_seconds: int = 60
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    api_base_url: str = "https://api.openai.com/v1"

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve(strict=False)
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"配置错误：WORKSPACE_ROOT 不是已存在的目录：{root}")
        object.__setattr__(self, "workspace_root", root)

        database = self.database_path or root / ".likai_nexus" / "tasks.db"
        database = Path(database).expanduser()
        if not database.is_absolute():
            database = root / database
        object.__setattr__(self, "database_path", database.resolve(strict=False))

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
        if self.max_read_bytes < 4:
            raise ConfigError("配置错误：MAX_READ_BYTES 至少为 4，才能保证截断游标可推进")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """先加载当前目录 .env，再用进程环境变量覆盖，并给出具体缺失项。"""

        if environ is None:
            values = _load_dotenv(Path.cwd() / ".env")
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
            database_path=database,
            bash_path=bash_path,
            default_bash_timeout_seconds=default_timeout,
            max_bash_timeout_seconds=max_timeout,
            max_output_bytes=_positive_int(
                values.get("MAX_OUTPUT_BYTES", str(64 * 1024)), "MAX_OUTPUT_BYTES"
            ),
            max_read_lines=_positive_int(
                values.get("MAX_READ_LINES", "2000"), "MAX_READ_LINES"
            ),
            max_read_bytes=_positive_int(
                values.get("MAX_READ_BYTES", str(64 * 1024)), "MAX_READ_BYTES"
            ),
            max_turns=_positive_int(values.get("MAX_TURNS", "20"), "MAX_TURNS"),
            model_timeout_seconds=_positive_int(
                values.get("MODEL_TIMEOUT_SECONDS", "60"), "MODEL_TIMEOUT_SECONDS"
            ),
            api_key=values.get("OPENAI_API_KEY"),
            model=values.get("MODEL", "gpt-4o-mini"),
            api_base_url=values.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
