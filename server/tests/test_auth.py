"""骨架验证：拒绝未授权（401 与审计留痕）+ dev-token / JWKS JWT 放行。

照 fd-zhesi test_cards_lifecycle 的测试形态：dev-token 模式做 API 级测试，
另附 JWKS JWT 直测（本地 RSA 密钥 + 本地 JWKS 文件服务）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")


def test_unauthorized_denied_and_audited() -> None:
    """无 token / 错 token / 非 Bearer → 401，且审计表留痕。"""
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
        r = client.get("/api/v1/me", headers=headers)
        assert r.status_code == 401
    from fd_zhengxin_api import personal

    conn = personal.connect()
    rows = conn.execute(
        "SELECT user_id, action, outcome, detail FROM audit_log WHERE outcome='denied'"
    ).fetchall()
    conn.close()
    assert len(rows) >= 3
    assert all(r["user_id"] is None for r in rows)


def test_dev_token_allowed_and_audited() -> None:
    """dev-token 放行：返回 user_id，审计留 allowed 痕。"""
    r = client.get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"user_id": "alice"}

    from fd_zhengxin_api import personal

    conn = personal.connect()
    rows = conn.execute(
        "SELECT user_id, outcome FROM audit_log WHERE outcome='allowed'"
    ).fetchall()
    conn.close()
    assert rows and rows[-1]["user_id"] == "alice"


def test_jwks_jwt_path(tmp_path, monkeypatch) -> None:
    """JWKS 路径（Logto 形态）：本地 RSA 签发 token 能过、篡改/过期被拒。"""
    import http.server
    import socketserver
    import threading

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "k1"
    jwk["alg"] = "RS256"
    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps({"keys": [jwk]}))

    # PyJWKClient 只认 http/https：本地起临时 HTTP 服务喂 JWKS
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # 静音
            pass

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("FDZX__AUTH_JWKS", f"http://127.0.0.1:{port}/{jwks_file.name}")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FDZX__AUTH_DEV_TOKENS", raising=False)

        def make(claims):
            return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

        tok = make({"sub": "carol", "exp": 4102444800})
        r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "carol"}

        bad = make({"sub": "mallory", "exp": 4102444800})[:-3] + "xyz"
        r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401

        expired = make({"sub": "carol", "exp": 1000000000})
        r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401
    finally:
        srv.shutdown()
