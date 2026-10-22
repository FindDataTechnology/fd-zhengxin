"""个人层访问控制（骨架）：必须携带用户授权，未授权拒绝并留痕。

照 fd-zhesi auth.py 的双令牌形态（都算「用户授权」）：
- JWKS JWT（RS256/ES384）：设 FDZX__AUTH_JWKS（如 Logto 的 JWKS 端点）
  + FDZX__AUTH_AUDIENCE 时启用，sub 即 user_id。
- 开发态固定令牌：FDZX__AUTH_DEV_TOKENS="token1=user_a,token2=user_b"
  （本地联调与测试用；生产不设此变量即只剩 JWT 一条路）。

环境变量统一走 config.py（FDZX__ 前缀），读取即时。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from . import config, personal


class Unauthorized(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=401, detail=detail)


def _dev_tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in config.auth_dev_tokens().split(","):
        if "=" in pair:
            token, user = pair.split("=", 1)
            out[token.strip()] = user.strip()
    return out


def _verify_jwt(token: str) -> str:
    import jwt  # pyjwt

    jwks_url = config.auth_jwks()
    audience = config.auth_audience()
    if not jwks_url:
        raise Unauthorized("JWT 校验未配置（FDZX__AUTH_JWKS）")
    from jwt import PyJWKClient

    jwks = PyJWKClient(jwks_url)
    signing = jwks.get_signing_key_from_jwt(token)
    # 照 fd-zhesi 实测口径：Logto 1.42 签发 ES384，RS256 保留兼容自签场景；
    # 未配置 audience 时显式关 aud 校验（pyjwt 2.10+ 的 InvalidAudienceError 坑）。
    claims = jwt.decode(
        token,
        signing.key,
        algorithms=["RS256", "ES384"],
        audience=audience or None,
        options={
            "require": ["exp", "sub"],
            **({} if audience else {"verify_aud": False}),
        },
    )
    return str(claims["sub"])


def authenticate(request: Request) -> str:
    """从 Authorization: Bearer 解出 user_id；失败即 401 并写审计留痕。"""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""

    user_id: str | None = None
    deny_reason = "missing bearer token"
    if token:
        dev = _dev_tokens()
        if token in dev:
            user_id = dev[token]
        else:
            try:
                user_id = _verify_jwt(token)
            except Exception as exc:  # noqa: BLE001 —— 任何校验失败都归一为 401
                deny_reason = f"invalid token: {type(exc).__name__}"

    conn = personal.connect()
    try:
        personal.migrate(conn)
        if user_id:
            personal.audit(conn, user_id, "auth", request.url.path, "allowed")
            return user_id
        personal.audit(conn, None, "auth", request.url.path, "denied", detail=deny_reason)
        raise Unauthorized(deny_reason)
    finally:
        conn.close()


def require_user(user_id: str = Depends(authenticate)) -> str:
    return user_id
