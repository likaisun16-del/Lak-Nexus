"""Session CLI 集成测试：验证会话管理命令不依赖模型密钥且遵守删除确认。"""

from __future__ import annotations

import re

from likai_nexus.channels.cli import main


def test_session_cli_new_list_and_delete(monkeypatch, settings, capsys) -> None:
    monkeypatch.setattr("likai_nexus.channels.cli.Settings.from_env", lambda: settings)

    assert main(["session", "new", "--title", "CLI 会话"]) == 0
    created = capsys.readouterr().out
    match = re.search(r"Session ([0-9a-f]+)", created)
    assert match is not None
    session_id = match.group(1)

    assert main(["session", "list"]) == 0
    assert session_id in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda prompt: "NO")
    assert main(["session", "delete", session_id]) == 1
    assert "已取消删除" in capsys.readouterr().out

    assert main(["session", "delete", session_id, "--confirm"]) == 0
    assert session_id in capsys.readouterr().out
