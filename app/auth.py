"""주최자 구글 로그인 (OAuth2 / OpenID Connect).

참가자는 로그인하지 않는다. 오직 주최자만 이 흐름을 쓴다.
"""

from __future__ import annotations

from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import Organizer

oauth = OAuth()
if config.GOOGLE_LOGIN_ENABLED:
    oauth.register(
        name="google",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

router = APIRouter()


def current_organizer(
    request: Request, db: Session = Depends(get_db)
) -> Optional[Organizer]:
    """로그인한 주최자를 돌려준다. 로그인 안 했으면 None."""
    oid = request.session.get("organizer_id")
    if not oid:
        return None
    return db.get(Organizer, oid)


def _safe_next(next_url: str) -> str:
    """로그인 후 돌아갈 내부 경로만 허용(오픈 리다이렉트 방지)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/login")
async def login(request: Request, next: str = "/"):
    if not config.GOOGLE_LOGIN_ENABLED:
        return RedirectResponse("/?error=google_login_disabled")
    request.session["next"] = _safe_next(next)
    return await oauth.google.authorize_redirect(request, config.OAUTH_REDIRECT_URI)


@router.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse("/?error=login_failed")

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.userinfo(token=token)

    sub = userinfo.get("sub")
    if not sub:
        return RedirectResponse("/?error=login_failed")

    organizer = db.query(Organizer).filter_by(google_sub=sub).first()
    if organizer is None:
        organizer = Organizer(
            google_sub=sub,
            email=userinfo.get("email", ""),
            name=userinfo.get("name", ""),
            picture=userinfo.get("picture", ""),
        )
        db.add(organizer)
        db.commit()
        db.refresh(organizer)
    else:
        # 프로필이 바뀌었을 수 있으니 갱신
        organizer.email = userinfo.get("email", organizer.email)
        organizer.name = userinfo.get("name", organizer.name)
        organizer.picture = userinfo.get("picture", organizer.picture)
        db.commit()

    request.session["organizer_id"] = organizer.id
    dest = _safe_next(request.session.pop("next", "/"))
    return RedirectResponse(dest)


@router.get("/logout")
async def logout(request: Request):
    request.session.pop("organizer_id", None)
    return RedirectResponse("/")
