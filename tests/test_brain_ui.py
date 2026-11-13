"""产品界面契约测试：新品牌、导航结构、XSS 防护与"不暴露内部细节"。"""

from __future__ import annotations

from aieb.brain.ui import render_index

HTML = render_index("0.1.0")


def test_brand_and_navigation():
    assert "AI 外脑 Demo" in HTML
    assert "Enterprise AI Workspace" in HTML
    for label in ("工作台", "AI 对话", "知识库", "会议中心", "待办事项", "长期记忆"):
        assert label in HTML, label
    # 默认首页是工作台，不是聊天窗口
    assert 'go("dashboard")' in HTML


def test_dashboard_has_no_hardcoded_demo_numbers():
    """工作台必须读取真实状态，不能写死统计数字或虚构会议。"""
    for fake in ("128 份", "24 场", "示例会议", "演示数据"):
        assert fake not in HTML


def test_markdown_is_sanitized():
    assert "DOMPurify.sanitize" in HTML
    assert "marked.parse" in HTML
    assert "innerHTML = marked.parse" not in HTML


def test_no_credentials_in_frontend():
    assert "api_key" not in HTML.lower()
    assert "AIEB_SMTP_PASSWORD" not in HTML
    assert "?token=" not in HTML


def test_tool_names_are_translated_to_plain_language():
    for text in ("正在查询最新信息…", "正在分析会议内容…", "正在制作汇报文稿…", "正在检索知识库…"):
        assert text in HTML


def test_email_requires_confirmation():
    assert "confirm:true" in HTML
    assert "发送前需要用户确认" not in HTML  # 该提示只在服务端，前端只需带 confirm


def test_responsive_and_keyboard_friendly():
    assert "@media (max-width:820px)" in HTML
    assert ":focus-visible" in HTML
