"""환경변수 로딩. 비밀값은 .env 에만 있고 깃허브에는 올라가지 않는다."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI: str = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback"
)
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./teemate.db")

# 운영(https)에서는 .env 에 COOKIE_SECURE=true 를 넣어 쿠키에 Secure 플래그를 건다.
# 로컬 개발/테스트(http)에서는 false 라야 쿠키가 정상 설정된다.
COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# 구글 로그인 설정이 갖춰졌는지 (열쇠 2개 모두 있는지)
GOOGLE_LOGIN_ENABLED: bool = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
