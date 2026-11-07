"""
대시보드용 MCP 서버 (Gate 0: Google OAuth + ping).

- 외부 주소: https://mcp.20mn.app (Cloudflare 터널 → localhost:8100)
- 인증: FastMCP GoogleProvider(OAuth Proxy). 허용 계정 1개(MCP_ALLOWED_EMAIL).
- MCP 엔드포인트: /mcp  (claude.ai 등록 주소 = https://mcp.20mn.app/mcp)
- 로그: logs/mcp_server.log (비밀값 기록 금지)
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------------------------------------------------------- 로깅
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "mcp_server.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler(sys.stderr)])
log = logging.getLogger("mcp_server")

# ---------------------------------------------------------------- 환경변수
CLIENT_ID = os.getenv("MCP_GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MCP_GOOGLE_CLIENT_SECRET", "").strip()
ALLOWED_EMAIL = os.getenv("MCP_ALLOWED_EMAIL", "").strip().lower()
BASE_URL = os.getenv("MCP_BASE_URL", "https://mcp.20mn.app").strip().rstrip("/")

_missing = [
    name
    for name, val in (
        ("MCP_GOOGLE_CLIENT_ID", CLIENT_ID),
        ("MCP_GOOGLE_CLIENT_SECRET", CLIENT_SECRET),
        ("MCP_ALLOWED_EMAIL", ALLOWED_EMAIL),
    )
    if not val
]
if _missing:
    log.error("필수 환경변수 누락: %s (.env에 추가 필요)", ", ".join(_missing))
    sys.exit(1)

# ---------------------------------------------------------------- 인증
import fastmcp  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.auth import AuthContext  # noqa: E402
from fastmcp.server.auth.providers.google import GoogleProvider  # noqa: E402
from fastmcp.server.dependencies import get_access_token  # noqa: E402
from fastmcp.server.middleware import AuthMiddleware  # noqa: E402

KST = timezone(timedelta(hours=9))
MCP_PATH = "/mcp"

auth = GoogleProvider(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    base_url=BASE_URL,
    required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
)


def _token_email(token) -> str | None:
    """검증된 AccessToken에서 Google 이메일을 꺼낸다. 없으면 None."""
    if token is None:
        return None
    claims = token.claims or {}
    email = claims.get("email") or (claims.get("upstream_claims") or {}).get("email")
    return email.strip().lower() if isinstance(email, str) and email else None


def allowed_email_only(ctx: AuthContext) -> bool:
    """전역 AuthCheck: 허용 이메일이 아니면 모든 도구/리소스/프롬프트 거부 (fail-closed)."""
    email = _token_email(ctx.token)
    ok = email is not None and email == ALLOWED_EMAIL
    if not ok:
        log.warning("접근 거부: component=%s email=%s", getattr(ctx.component, "name", "?"), email or "(none)")
    return ok


def require_allowed_email() -> str:
    """도구 내부 공통 가드(이중 방어). 허용 이메일이 아니면 예외."""
    email = _token_email(get_access_token())
    if email is None or email != ALLOWED_EMAIL:
        raise PermissionError("not allowed")
    return email


mcp = FastMCP(
    name="investment-dashboard",
    auth=auth,
    middleware=[AuthMiddleware(auth=allowed_email_only)],
)


@mcp.tool
def ping() -> dict:
    """서버 생존/인증 확인."""
    email = require_allowed_email()
    return {
        "ok": True,
        "server_time": datetime.now(KST).isoformat(timespec="seconds"),
        "email": email,
        "version": fastmcp.__version__,
    }


if __name__ == "__main__":
    log.info(
        "MCP 서버 시작: fastmcp=%s base_url=%s callback=%s%s mcp=%s%s",
        fastmcp.__version__,
        BASE_URL,
        BASE_URL,
        auth._redirect_path,
        BASE_URL,
        MCP_PATH,
    )
    mcp.run(transport="http", host="127.0.0.1", port=8100, path=MCP_PATH, show_banner=False)
