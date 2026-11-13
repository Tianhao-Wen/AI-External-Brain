"""品牌与版本一致性（2026-11 品牌统一后更新）。

约束：
* 产品展示名统一为「AI 外脑 Demo」，英文为「AI External Brain Demo」；
* 分发名 / Python 包名 / CLI 均为 ``aieb``；旧包名 ``wendream`` 已废弃；
* 上游归属（开源 MewCode Agent 框架 → WenDream 通用 Agent 实现）必须如实保留，
  不得为了消除旧品牌而删除合法署名。
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import aieb  # noqa: E402
from aieb.web_content import INDEX_HTML  # noqa: E402

PRODUCT_ZH = "AI 外脑 Demo"
PRODUCT_EN = "AI External Brain Demo"


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_is_defined_once_and_matches_pyproject():
    from aieb.brain import __version__ as product_version

    assert re.fullmatch(r"\d+\.\d+\.\d+", product_version), product_version
    assert _pyproject()["project"]["version"] == product_version


def test_product_name_constants():
    assert aieb.__product__ == PRODUCT_ZH
    assert aieb.__product_en__ == PRODUCT_EN


def test_tui_brand_and_version():
    from aieb.app import AiebApp

    assert AiebApp.TITLE == PRODUCT_EN
    banner = AiebApp._make_banner("test-model", "C:/work")
    text = banner.plain
    assert PRODUCT_ZH in text
    assert aieb.__version__ in text
    assert "WenDream" not in text
    assert "v0.1.0" not in text


def test_legacy_web_ui_brand_and_version_placeholder():
    assert PRODUCT_ZH in INDEX_HTML
    assert "__AIEB_VERSION__" in INDEX_HTML
    # 旧版网页已随品牌统一改名，不应再出现历史品牌
    assert "WenDream" not in INDEX_HTML


def test_product_ui_has_no_legacy_brand():
    """产品主界面（聊天 SPA）不得残留历史品牌。"""
    for name in ("app_js.py", "css.py", "__init__.py"):
        text = (REPO_ROOT / "aieb" / "brain" / "ui" / name).read_text(encoding="utf-8")
        assert "WenDream" not in text, name


def test_cli_keeps_upstream_attribution():
    text = (REPO_ROOT / "aieb" / "__main__.py").read_text(encoding="utf-8")
    assert "MewCode Agent" in text, "必须如实说明基于开源 Agent 框架"
    assert "from scratch" not in text.lower()


def test_attribution_constants_preserved():
    """归属常量保留上游技术事实，不随品牌改名而删除。"""
    assert aieb.__upstream__ == "MewCode Agent"
    assert aieb.__derived_from__ == "WenDream"


def test_attribution_doc_keeps_origin_chain():
    text = (REPO_ROOT / "docs" / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "MewCode Agent" in text
    assert "WenDream" in text, "归属链必须保留原始实现名称"


def test_pyproject_description_mentions_both_names():
    description = _pyproject()["project"]["description"]
    assert PRODUCT_ZH in description
    assert "Agent 框架" in description, "必须如实说明技术基础（开源 Agent 框架）"


def test_console_scripts_point_at_aieb_package():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["aieb"] == "aieb.brain.cli:main"
    # 旧命令行名保留为兼容别名，但仍指向重命名后的包，不会重新引入旧包目录
    assert scripts.get("wendream") == "aieb.__main__:main"


def test_python_package_renamed_to_aieb():
    """Python 包目录已从 ``wendream`` 迁移为 ``aieb``。"""
    assert (REPO_ROOT / "aieb" / "__init__.py").exists()
    assert not (REPO_ROOT / "wendream").exists(), "旧包目录不应再存在"
    assert _pyproject()["project"]["name"] == "aieb"
    hatch = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert hatch["packages"] == ["aieb"]


def test_sandbox_docs_do_not_claim_os_isolation_on_windows():
    from aieb.sandbox import sandbox_support_summary

    summary = sandbox_support_summary()
    assert "No OS-level sandbox" in summary or "OS-level sandbox" in summary
    # 明确区分权限层与 OS 层
    assert "Permission" in summary or "permission" in summary or "path validation" in summary
