"""应用数据目录管理：创建 data/script 目录并安全迁移旧 SQLite 数据库。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from ..errors import ConfigError


class AppDataManager:
    """集中管理项目根目录 data、工作区 script 和旧数据库迁移。"""

    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        database_path: Path,
        use_default_database: bool,
    ) -> None:
        self.project_root = project_root
        self.workspace_root = workspace_root
        self.database_path = database_path
        self.use_default_database = use_default_database

    @property
    def data_root(self) -> Path:
        return self.project_root / "data"

    @property
    def script_root(self) -> Path:
        return self.workspace_root / "script"

    def prepare(self) -> tuple[str, ...]:
        """创建运行目录并迁移默认数据库，返回需要提示用户的迁移说明。"""

        self._ensure_directory(self.data_root, "应用数据目录")
        self._ensure_directory(self.script_root, "默认脚本目录")
        if not self.use_default_database:
            return ()
        return self._migrate_legacy_databases()

    def _migrate_legacy_databases(self) -> tuple[str, ...]:
        target = self.database_path
        legacy_paths = tuple(
            path
            for path in (
                self.workspace_root / "data" / "likai_nexus.db",
                self.workspace_root / ".likai_nexus" / "tasks.db",
            )
            if path.resolve(strict=False) != target.resolve(strict=False)
        )
        legacy_paths = tuple(path for path in legacy_paths if self._has_database_files(path))
        if target.exists():
            if not legacy_paths:
                return ()
            backup = self._backup_legacy_databases(legacy_paths)
            return (
                (
                    f"检测到旧数据库，已保留项目根 data/likai_nexus.db 为当前库，"
                    f"旧库已备份到 {backup}"
                ),
            )
        if not legacy_paths:
            return ()

        for path in legacy_paths:
            self._validate_database(path)
        authority = legacy_paths[0]
        backup = self._new_backup_path()
        temporary_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        moved: list[tuple[Path, Path]] = []
        try:
            self._copy_database(authority, temporary_target)
            self._validate_database(temporary_target)
            backup.mkdir(parents=False, exist_ok=False)
            for source in legacy_paths:
                for item in self._database_files(source):
                    destination = backup / item.name
                    shutil.move(str(item), str(destination))
                    moved.append((destination, item))
            os.replace(temporary_target, target)
        except Exception as exc:
            temporary_target.unlink(missing_ok=True)
            for destination, original in reversed(moved):
                try:
                    shutil.move(str(destination), str(original))
                except OSError:
                    # 原始文件仍应尽量保留；最终错误会阻止程序继续启动。
                    continue
            if backup.exists():
                try:
                    backup.rmdir()
                except OSError:
                    pass
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(
                f"数据库迁移失败：从 {authority} 迁移到 {target}，"
                f"原因：{type(exc).__name__}"
            ) from exc

        if len(legacy_paths) == 1:
            return (f"旧数据库已迁移到项目根 data/likai_nexus.db，旧文件备份到 {backup}",)
        return (
            (
                f"检测到多个旧数据库，已选择 {authority} 作为权威库并迁移到项目根 data/；"
                f"所有旧文件已备份到 {backup}"
            ),
        )

    def _backup_legacy_databases(self, paths: tuple[Path, ...]) -> Path:
        backup = self._new_backup_path()
        moved: list[tuple[Path, Path]] = []
        try:
            backup.mkdir(parents=False, exist_ok=False)
            for source in paths:
                for item in self._database_files(source):
                    destination = backup / item.name
                    shutil.move(str(item), str(destination))
                    moved.append((destination, item))
        except (OSError, shutil.Error) as exc:
            for destination, original in reversed(moved):
                try:
                    shutil.move(str(destination), str(original))
                except OSError:
                    continue
            try:
                backup.rmdir()
            except OSError:
                pass
            raise ConfigError(
                f"旧数据库备份失败：目标目录 {backup}，原因：{type(exc).__name__}"
            ) from exc
        return backup

    def _new_backup_path(self) -> Path:
        return self.data_root / f"legacy-backup-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _database_files(path: Path) -> tuple[Path, ...]:
        return tuple(
            item
            for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
            if item.exists()
        )

    @classmethod
    def _has_database_files(cls, path: Path) -> bool:
        sidecars = tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm"))
        if not path.exists() and any(item.exists() for item in sidecars):
            raise ConfigError(f"数据库迁移失败：发现没有主库的 SQLite sidecar：{path}")
        return path.exists()

    @staticmethod
    def _validate_database(path: Path) -> None:
        try:
            with closing(sqlite3.connect(path)) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise ConfigError(
                f"数据库校验失败：文件 {path} 不是可打开的 SQLite 数据库，原因：{type(exc).__name__}"
            ) from exc
        if not result or result[0] != "ok":
            raise ConfigError(f"数据库校验失败：文件 {path} 的完整性检查未通过")

    @staticmethod
    def _copy_database(source: Path, destination: Path) -> None:
        try:
            with (
                closing(sqlite3.connect(source)) as source_connection,
                closing(sqlite3.connect(destination)) as destination_connection,
            ):
                source_connection.backup(destination_connection)
                destination_connection.commit()
        except (OSError, sqlite3.DatabaseError) as exc:
            destination.unlink(missing_ok=True)
            raise ConfigError(
                f"数据库复制失败：源文件 {source}，目标文件 {destination}，"
                f"原因：{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _ensure_directory(path: Path, label: str) -> None:
        if AppDataManager._is_link(path):
            raise ConfigError(f"{label}初始化失败：目标 {path} 是符号链接，拒绝跟随访问")
        if path.exists() and not path.is_dir():
            raise ConfigError(f"{label}初始化失败：目标 {path} 已存在但不是目录")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"{label}初始化失败：无法创建目标 {path}，原因：{type(exc).__name__}"
            ) from exc
        if AppDataManager._is_link(path) or not path.is_dir():
            raise ConfigError(f"{label}初始化失败：目标 {path} 创建后状态不安全")

    @staticmethod
    def _is_link(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
