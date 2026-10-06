"""마감 시각이 지난 라운딩을 자동 확정(+주최자 메일)하는 크론용 스크립트.

cron 예: * * * * * cd /opt/golf && PYTHONPATH=/opt/golf .venv/bin/python tools/close_due.py
"""

from __future__ import annotations

from app import config
from app.database import SessionLocal, init_db
from app.tasks import close_due_rounds


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        n = close_due_rounds(db, config.APP_BASE_URL)
        if n:
            print(f"[close_due] 자동 마감 처리: {n}건")
    finally:
        db.close()


if __name__ == "__main__":
    main()
