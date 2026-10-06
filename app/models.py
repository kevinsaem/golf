"""데이터 구조 정의.

Organizer(주최자) ──< Round(라운딩) ──< Participant(참가자)
- 주최자는 구글 로그인으로 식별, 자기 라운딩들을 계속 소유한다.
- 참가자는 로그인 없이 라운딩별 토큰(쿠키)으로 식별한다.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token(nbytes: int = 9) -> str:
    return secrets.token_urlsafe(nbytes)


class Organizer(Base):
    """주최자 = 구글 로그인 사용자."""

    __tablename__ = "organizers"

    id: Mapped[int] = mapped_column(primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    picture: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    rounds: Mapped[list["Round"]] = relationship(
        back_populates="organizer",
        cascade="all, delete-orphan",
        order_by="Round.created_at.desc()",
    )


class Round(Base):
    """라운딩 한 건."""

    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, default=lambda: _token(8)
    )
    organizer_id: Mapped[int] = mapped_column(ForeignKey("organizers.id"))
    title: Mapped[str] = mapped_column(String(255))
    group_count: Mapped[int] = mapped_column(Integer)
    group_size: Mapped[int] = mapped_column(Integer, default=4)
    mutual_bonus: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="collecting")  # collecting / finalized
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    organizer: Mapped["Organizer"] = relationship(back_populates="rounds")
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="Participant.id",
    )

    # --- 편의 속성 ---
    @property
    def total_count(self) -> int:
        return len(self.participants)

    @property
    def submitted_count(self) -> int:
        return sum(1 for p in self.participants if p.submitted)

    @property
    def all_submitted(self) -> bool:
        return self.total_count > 0 and self.submitted_count == self.total_count


class Participant(Base):
    """라운딩 참가자. 이름은 주최자가 미리 넣고, 본인이 '내 이름'을 선택(claim)한다."""

    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"))
    name: Mapped[str] = mapped_column(String(100))
    token: Mapped[str] = mapped_column(String(32), index=True, default=lambda: _token(12))
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 희망 순위: 같은 조가 되고 싶은 순서대로 나열한 '다른 참가자 id' 목록
    preference_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    round: Mapped["Round"] = relationship(back_populates="participants")
