"""/callback 端點：簽章檢查、LINE 的 Verify 按鈕、背景處理。"""
import json

from fastapi.testclient import TestClient

from app import main
from app.bot import Bot
from app.signature import make_signature
from conftest import make_event

client = TestClient(main.app)


def post(body: bytes, signature: str | None = None):
    return client.post("/callback", content=body, headers={
        "content-type": "application/json",
        "x-line-signature": signature if signature is not None else make_signature(body, "test-secret"),
    })


def test_bad_signature_is_rejected():
    assert post(b'{"events":[]}', signature="not-a-real-signature").status_code == 400


def test_verify_button_gets_200():
    # 在 LINE Developers Console 按「Verify」時，LINE 會送一個 events 為空的請求
    assert post(b'{"destination":"Uffffffffffffffffffffffffffffffff","events":[]}').status_code == 200


def test_event_is_handled_after_responding(monkeypatch, line):
    monkeypatch.setattr(main, "bot", Bot(line))
    body = json.dumps({"destination": "U" + "f" * 32, "events": [make_event(text="榜單")]},
                      ensure_ascii=False).encode("utf-8")
    r = post(body)
    assert r.status_code == 200 and r.json() == {"ok": True, "events": 1}
    assert "榜單" in line.texts()[0]


def test_healthz():
    assert client.get("/healthz").json()["ok"] is True
