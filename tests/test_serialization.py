"""针对各 provider 序列化构建器的单元测试。

会话层与具体 provider 无关；序列化逻辑位于 aieb.serialization。
这些测试用于锁定各种线上传输格式（wire format），更关键的是锁定
Extended Thinking 的往返（round-trip）契约：带 tool-use 的这一轮必须把它
带签名的 thinking block 一并回传给 API（否则 Anthropic 会返回 400）。
"""
from __future__ import annotations

from aieb.conversation import (
    ConversationManager,
    DocumentBlock,
    ImageBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from aieb.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_messages,
    build_openai_input,
)


def test_anthropic_preserves_signed_thinking_at_head():
    conv = ConversationManager()
    conv.add_assistant_message(
        "answer",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
        thinking_blocks=[ThinkingBlock(thinking="let me think", signature="sig-1")],
    )
    msgs = build_anthropic_messages(conv.get_messages())
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["signature"] == "sig-1"
    assert content[-1]["type"] == "tool_use"


def test_anthropic_tool_results_become_user_blocks():
    conv = ConversationManager()
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="tu-1", content="out", is_error=False)])
    msgs = build_anthropic_messages(conv.get_messages())
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["type"] == "tool_result"
    assert msgs[0]["content"][0]["tool_use_id"] == "tu-1"


def test_anthropic_merges_system_reminder_into_prev_user():
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_system_reminder("note")
    msgs = build_anthropic_messages(conv.get_messages())
    assert len(msgs) == 1
    assert "hello" in msgs[0]["content"]
    assert "system-reminder" in msgs[0]["content"]


def test_openai_input_tool_use_as_function_call():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
    )
    msgs = build_openai_input(conv.get_messages())
    assert len(msgs) == 2  # 文本消息 + function_call
    assert msgs[0]["role"] == "assistant"
    assert msgs[1]["type"] == "function_call"
    assert msgs[1]["name"] == "Bash"


def test_openai_input_tool_results_as_function_call_output():
    conv = ConversationManager()
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="tu-1", content="out")])
    msgs = build_openai_input(conv.get_messages())
    assert msgs[0]["type"] == "function_call_output"
    assert msgs[0]["output"] == "out"


def test_chat_completion_uses_tool_calls_with_reasoning_content():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
        thinking_blocks=[ThinkingBlock(thinking="let me think", signature="sig")],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "Bash"
    # thinking blocks 作为 reasoning_content 回传（DeepSeek/小米等 provider 要求）
    assert msgs[0]["reasoning_content"] == "let me think"


def test_chat_completion_no_reasoning_when_no_thinking():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    assert "reasoning_content" not in msgs[0]


def test_chat_completion_supports_multimodal_user_images():
    conv = ConversationManager()
    conv.add_user_message(
        "看图",
        images=[ImageBlock(media_type="image/png", data="abc123")],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    assert msgs[0]["role"] == "user"
    assert isinstance(msgs[0]["content"], list)
    assert msgs[0]["content"][0]["type"] == "text"
    assert msgs[0]["content"][1]["type"] == "image_url"
    assert msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_completion_turns_documents_into_text_blocks():
    """openai-compat 不支持 {"type": "file"}（DeepSeek 直接 400），
    文档必须转成带页码的文本块，且原文不能丢。"""
    import base64

    import pymupdf

    doc = pymupdf.open()
    for i in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 100), f"CLAUSE-{i} 11.9% requires CEO approval", fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()

    conv = ConversationManager()
    conv.add_user_message(
        "谁审批？",
        documents=[DocumentBlock(
            media_type="application/pdf",
            data=base64.b64encode(pdf_bytes).decode(),
            name="制度.pdf",
        )],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    blocks = msgs[0]["content"]
    # 关键断言：绝不出现 file 块（DeepSeek 会以 400 拒绝）
    assert all(b["type"] != "file" for b in blocks)
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "制度.pdf" in text
    assert "【第1页】" in text and "【第2页】" in text
    assert "CLAUSE-1" in text and "CLAUSE-2" in text
    assert "谁审批？" in text


def test_chat_completion_documents_degrade_when_unparsable():
    conv = ConversationManager()
    conv.add_user_message(
        "看看",
        documents=[DocumentBlock(media_type="application/pdf", data="not-a-real-pdf", name="坏文件.pdf")],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    blocks = msgs[0]["content"]
    assert all(b["type"] != "file" for b in blocks)
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "坏文件.pdf" in text


def test_openai_input_includes_reasoning_items():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
        thinking_blocks=[ThinkingBlock(thinking="reasoning here", signature="rs-001")],
    )
    msgs = build_openai_input(conv.get_messages())
    # 第一个 item 应该是 reasoning
    assert msgs[0]["type"] == "reasoning"
    assert msgs[0]["id"] == "rs-001"
    assert msgs[0]["summary"][0]["text"] == "reasoning here"


def test_build_messages_dispatch_by_protocol():
    conv = ConversationManager()
    conv.add_user_message("hi")
    msgs = conv.get_messages()
    assert build_messages(msgs, "anthropic") == build_anthropic_messages(msgs)
    assert build_messages(msgs, "openai") == build_openai_input(msgs)
    assert build_messages(msgs, "openai-compat") == build_chat_completion_messages(msgs)
