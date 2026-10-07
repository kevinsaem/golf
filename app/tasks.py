"""조편성 확정 / 자동 마감 로직 (요청 핸들러와 크론 양쪽에서 공용).

- finalize_round: 응답한 데이터 기준으로 매칭 실행 → 결과 저장 → 상태 확정
- auto_close_if_due / close_due_rounds: 마감시각이 지난 라운딩을 자동 확정 + 주최자 메일
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import config
from app.models import Round
from tools.mailer import send_email
from tools.matcher import match_groups


def utcnow() -> datetime:
    """tz 정보 없는 UTC 시각(저장/비교 일관성용)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_preferences(rnd: Round) -> tuple[list[str], dict[str, list[str]]]:
    names = [p.name for p in rnd.participants]
    id_to_name = {p.id: p.name for p in rnd.participants}
    prefs: dict[str, list[str]] = {}
    for p in rnd.participants:
        order = p.preference_json or []
        prefs[p.name] = [id_to_name[i] for i in order if i in id_to_name]
    return names, prefs


def serialize_result(total: float, per_person, groups: list[list[str]], exact: bool) -> dict:
    return {
        "groups": groups,
        "total_score": total,
        "exact": exact,
        "per_person": [
            {
                "name": pr.name,
                "group_index": pr.group_index,
                "got_top_choice": pr.got_top_choice,
                "satisfied_count": pr.satisfied_count,
                "best_mate_rank": pr.best_mate_rank,
                "mates": [
                    {"name": m.name, "rank": m.rank, "mutual": m.mutual} for m in pr.mates
                ],
            }
            for pr in per_person
        ],
    }


def finalize_round(db: Session, rnd: Round) -> None:
    """응답한 데이터 기준으로 조편성을 확정한다. (미응답자는 희망 없음으로 자동 배치)"""
    names, prefs = build_preferences(rnd)
    result = match_groups(
        names, prefs, group_count=rnd.group_count, mutual_bonus=rnd.mutual_bonus
    )
    rnd.result_json = serialize_result(
        result.total_score, result.per_person, result.groups, result.exact
    )
    rnd.status = "finalized"
    db.commit()


def _notify_closed(rnd: Round, base_url: str, reason: str = "deadline") -> None:
    organizer = rnd.organizer
    if not organizer or not organizer.email:
        return
    base = base_url.rstrip("/")
    result_url = f"{base}/r/{rnd.public_id}"      # 로그인 없이 누구나 결과 열람
    manage_url = f"{base}/rounds/{rnd.public_id}/manage"  # 주최자 관리(로그인 필요)
    if reason == "all":
        headline = "참가자 전원이 희망 순위를 등록해서 바로 조편성이 확정됐어요."
        subject = f"[티메이트] '{rnd.title}' 전원 등록 완료 — 조편성이 확정됐어요"
    else:
        headline = (
            f"설정하신 시간에 도달해 자동 마감됐어요. "
            f"응답한 {rnd.submitted_count}/{rnd.total_count}명 기준으로 확정됐습니다."
        )
        subject = f"[티메이트] '{rnd.title}' 자동 마감 — 조편성이 확정됐어요"
    text = (
        f"{organizer.name or '주최자'}님,\n\n"
        f"'{rnd.title}' {headline}\n\n"
        f"▶ 결과 보기(바로 열림): {result_url}\n"
        f"▶ 관리/수정(로그인): {manage_url}\n\n⛳ 티메이트 (golf.kevinsaem.com)"
    )
    html = (
        f"<p>{organizer.name or '주최자'}님,</p>"
        f"<p><b>'{rnd.title}'</b> {headline}</p>"
        f"<p><a href=\"{result_url}\" style=\"display:inline-block;background:#16a34a;color:#fff;"
        f"padding:12px 20px;border-radius:10px;text-decoration:none;font-weight:bold\">🏁 결과 보기</a></p>"
        f"<p style=\"font-size:13px;color:#64748b\">조를 직접 조정하려면 "
        f"<a href=\"{manage_url}\">관리 화면</a>(로그인)에서 가능해요.</p>"
        f"<p style=\"color:#94a3b8;font-size:12px\">⛳ 티메이트 · golf.kevinsaem.com</p>"
    )
    send_email(organizer.email, subject, text, html)


def auto_close_if_due(db: Session, rnd: Round, base_url: str) -> bool:
    """마감시각이 지났으면 자동 확정 + 주최자 메일. 확정했으면 True."""
    if rnd.status != "collecting" or rnd.deadline is None:
        return False
    if utcnow() < rnd.deadline:
        return False
    finalize_round(db, rnd)
    _notify_closed(rnd, base_url, reason="deadline")
    return True


def close_due_rounds(db: Session, base_url: str) -> int:
    """자동 확정 대상(전원 제출 or 마감시간 경과)을 모두 확정(크론용). 처리 건수 반환."""
    rounds = (
        db.query(Round)
        .filter(Round.status == "collecting", Round.deadline.isnot(None))
        .all()
    )
    count = 0
    for rnd in rounds:
        if rnd.deadline is not None and utcnow() >= rnd.deadline:
            finalize_round(db, rnd)
            _notify_closed(rnd, base_url, reason="deadline")
            count += 1
    return count
