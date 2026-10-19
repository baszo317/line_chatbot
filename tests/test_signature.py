import base64
import hashlib
import hmac

from app.signature import make_signature, verify_signature

BODY = '{"destination":"U0000","events":[{"type":"message","message":{"text":"課程介紹"}}]}'.encode("utf-8")


def test_matches_line_formula():
    expected = base64.b64encode(hmac.new(b"secret", BODY, hashlib.sha256).digest()).decode()
    assert make_signature(BODY, "secret") == expected


def test_valid_signature_passes():
    assert verify_signature(BODY, make_signature(BODY, "secret"), "secret")


def test_changed_body_fails():
    sig = make_signature(BODY, "secret")
    assert not verify_signature(BODY.replace(b'"U0000"', b'"U0001"'), sig, "secret")


def test_reformatted_json_fails():
    # 先解析再轉回字串（例如多了空白）就對不上：一定要用原始 bytes 驗證
    sig = make_signature(BODY, "secret")
    assert not verify_signature(BODY.replace(b",", b", "), sig, "secret")


def test_wrong_secret_or_missing_header_fails():
    sig = make_signature(BODY, "secret")
    assert not verify_signature(BODY, sig, "another-secret")
    assert not verify_signature(BODY, "", "secret")
