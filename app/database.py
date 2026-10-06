"""데이터베이스 연결. 개발은 SQLite 파일 하나, 배포는 PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """요청 한 건 동안 쓰는 DB 세션. 끝나면 닫는다."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """테이블이 없으면 만든다. (앱 시작 시 호출)"""
    from app import models  # noqa: F401  (모델 등록용)

    Base.metadata.create_all(bind=engine)
