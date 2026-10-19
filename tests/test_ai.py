"""Claude 呼叫：用假的 HTTP 回應檢查「送出的請求格式」與「解析回應」，不需要真的 API key。

注意：新版 anthropic SDK 內部用的是 httpx2（不是 httpx），假的傳輸層要用 httpx2 建立。
"""
import json
from dataclasses import replace

import anthropic
import httpx2 as httpx
import pytest

from app import ai
from app.bot import Bot
from conftest import make_event

ANSWER = {"reply": "國二數學 A 班一週上 2 次。", "intent": "course", "needs_human": False,
          "used_titles": ["國二數學 A 班"]}


def response(payload, stop_reason="end_turn", text=None):
    return httpx.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text if text is not None else json.dumps(payload, ensure_ascii=False)}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 60,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1400},
    })


@pytest.fixture
def use_claude(monkeypatch):
    """切到 claude 模式，並把 HTTP 換成假的；回傳一個 dict 記錄收到的請求。"""
    seen: dict = {}

    def install(handler):
        def wrapped(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return handler(request)
        client = anthropic.Anthropic(api_key="test", base_url="http://mock-claude",
                                     http_client=httpx.Client(transport=httpx.MockTransport(wrapped)),
                                     max_retries=0)
        monkeypatch.setattr(ai, "settings", replace(ai.settings, ai_mode="claude", claude_effort="low"))
        monkeypatch.setattr(ai, "_client", client)
        return seen
    return install


def test_request_follows_api_format(use_claude):
    seen = use_claude(lambda r: response(ANSWER))
    history = [{"direction": "in", "content": "你好"}, {"direction": "out", "content": "您好！"}]
    result = ai.answer("國二數學一週上幾次？", history)

    body = seen["body"]
    assert seen["path"] == "/v1/messages"
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "國二數學 A 班" in body["system"][1]["text"]        # 補習班資料有放進去
    assert "今天是" in body["system"][2]["text"]                # 日期放在快取區塊之後
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][-1]["content"] == "國二數學一週上幾次？"

    assert result.reply == ANSWER["reply"] and result.intent == "course" and not result.needs_human
    assert result.usage["cache_read_input_tokens"] == 1400


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}),
    lambda r: response(None, text="這不是 JSON"),
    lambda r: response(ANSWER, stop_reason="refusal"),
    lambda r: response(ANSWER, stop_reason="max_tokens"),
    lambda r: response({**ANSWER, "reply": ""}),
])
def test_problems_return_none(use_claude, handler):
    use_claude(handler)
    assert ai.answer("國二數學一週上幾次？", []) is None


def test_markdown_symbols_are_removed(use_claude):
    use_claude(lambda r: response({**ANSWER, "reply": "## 上課時間\n**週一**、週三"}))
    assert ai.answer("國二數學什麼時候上課？", []).reply == "上課時間\n週一、週三"


def test_history_is_merged_and_starts_with_user():
    history = [{"direction": "out", "content": "歡迎！"},
               {"direction": "in", "content": "請問"}, {"direction": "in", "content": "國二數學"}]
    msgs = ai.build_messages(history, "什麼時候上課？")
    assert msgs == [{"role": "user", "content": "請問\n國二數學\n什麼時候上課？"}]


def test_bot_uses_ai_reply(use_claude, line):
    use_claude(lambda r: response({**ANSWER, "needs_human": True}))
    Bot(line).handle_event(make_event(text="國二數學一週上幾次？"))
    assert line.texts() == [ANSWER["reply"]]
    assert "找真人客服" in line.quick_replies()   # AI 判斷需要真人 → 附上按鈕
    assert line.loading == 1                      # 有顯示「輸入中」動畫


def test_bot_falls_back_to_keywords_when_ai_fails(use_claude, line):
    use_claude(lambda r: httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))
    Bot(line).handle_event(make_event(text="補習班地址在哪裡？"))
    assert "【地址與交通方式】" in line.texts()[0]
