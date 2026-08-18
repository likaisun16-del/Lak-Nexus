"""最小 CLI 集成测试：验证入口参数和缺失配置时的明确退出码。"""

from __future__ import annotations

from pathlib import Path

from likai_nexus.channels.cli import build_parser, main


def test_cli_parser_accepts_unquoted_task_words() -> None:
    args = build_parser().parse_args(["读取", "README.md"])
    assert args.request == ["读取", "README.md"]


def test_cli_reports_missing_workspace(monkeypatch, tmp_path: Path) -> None:
    # 测试必须避开项目根目录 .env，确保验证的确是缺少配置的入口错误。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert main(["执行", "任务"]) == 2
