"""记忆 CLI 集成测试：验证显式新增、查看、更新和禁用流程。"""

from __future__ import annotations

from pathlib import Path

from likai_nexus.channels.cli import main


def _configure_cli(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()


def test_memory_cli_manages_explicit_memory(monkeypatch, tmp_path: Path, capsys) -> None:
    _configure_cli(monkeypatch, tmp_path)

    assert main(["memory", "add", "--type", "project", "项目使用", "SQLite"]) == 0
    added = capsys.readouterr().out
    memory_id = added.split("：", 1)[1].split("，", 1)[0]

    assert main(["memory", "list"]) == 0
    listed = capsys.readouterr().out
    assert memory_id in listed
    assert "项目使用 SQLite" in listed

    assert main(["memory", "update", memory_id, "--content", "项目先用 SQLite 验证"]) == 0
    assert "记忆已更新" in capsys.readouterr().out
    assert main(["memory", "show", memory_id]) == 0
    assert "项目先用 SQLite 验证" in capsys.readouterr().out

    assert main(["memory", "disable", memory_id]) == 0
    assert memory_id in capsys.readouterr().out
    assert main(["memory", "list"]) == 0
    assert memory_id not in capsys.readouterr().out
