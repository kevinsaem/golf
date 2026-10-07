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

# --- 이메일(SMTP) : 전원 등록 완료 시 주최자 알림용 ---
# 설정이 없으면 메일 발송은 조용히 건너뛴다(앱은 정상 동작).
SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER: str = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM: str = os.environ.get("MAIL_FROM", "") or SMTP_USER
MAIL_ENABLED: bool = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

# 서비스 기본 주소 (크론 자동마감 메일의 링크 생성용 — 요청 컨텍스트가 없을 때 사용)
APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "https://golf.kevinsaem.com")

# 검색엔진 소유확인 코드 (구글 서치콘솔 / 네이버 서치어드바이저). 발급받으면 .env에 넣기.
GOOGLE_SITE_VERIFICATION: str = os.environ.get("GOOGLE_SITE_VERIFICATION", "")
NAVER_SITE_VERIFICATION: str = os.environ.get("NAVER_SITE_VERIFICATION", "")

# 자동 마감 기본값(분) 및 허용 단위(30분)
AUTO_CLOSE_DEFAULT_MINUTES: int = 60
AUTO_CLOSE_STEP_MINUTES: int = 30

# 구글 로그인 설정이 갖춰졌는지 (열쇠 2개 모두 있는지)
GOOGLE_LOGIN_ENABLED: bool = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
