"""웹 라우트 통합 테스트 (FastAPI TestClient).

구글 로그인은 current_organizer 의존성을 테스트용 주최자로 오버라이드해서 흉내 낸다.
참가자 흐름은 쿠키를 그대로 쓰는 TestClient 로 실제처럼 검증한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import current_organizer
from app.database import Base, get_db
from app.main import app
from app.models import Organizer, Round


@pytest.fixture
def ctx():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    s = TestingSession()
    org = Organizer(google_sub="test-sub", email="boss@test.com", name="테스트사장")
    s.add(org)
    s.commit()
    org_id = org.id
    s.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def override_current_organizer():
        db = TestingSession()
        try:
            yield db.get(Organizer, org_id)
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[current_organizer] = override_current_organizer
    client = TestClient(app)
    try:
        yield client, TestingSession, org_id
    finally:
        app.dependency_overrides.clear()


def _create_round(client, title="테스트 라운딩", group_count=2, names=None):
    names = names or ["가", "나", "다", "라", "마", "바", "사", "아"]
    r = client.post(
        "/rounds",
        data={"title": title, "group_count": group_count, "names": "\n".join(names)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"].split("/")[2]  # /rounds/{pid}/manage


# ---------------------------------------------------------------------------
# 주최자 흐름
# ---------------------------------------------------------------------------
def test_dashboard_shown_when_logged_in(ctx):
    client, _, _ = ctx
    r = client.get("/")
    assert r.status_code == 200
    assert "새 라운딩 만들기" in r.text


def test_create_round_and_manage_page(ctx):
    client, Session, _ = ctx
    pid = _create_round(client)
    r = client.get(f"/rounds/{pid}/manage")
    assert r.status_code == 200
    assert f"/r/{pid}" in r.text  # 공유 링크 노출
    assert "가" in r.text and "아" in r.text  # 참가자 명단 노출
    assert "0 / 8명" in r.text  # 진행현황

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.total_count == 8
    assert rnd.group_count == 2
    db.close()


def test_create_round_requires_min_participants(ctx):
    client, _, _ = ctx
    r = client.post(
        "/rounds",
        data={"title": "x", "group_count": 1, "names": "혼자"},
        follow_redirects=False,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 참가자 흐름 (링크 + 쿠키 + 재접속)
# ---------------------------------------------------------------------------
def test_participant_join_rank_and_reconnect(ctx):
    client, Session, _ = ctx
    pid = _create_round(client)

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    parts = {p.name: p.id for p in rnd.participants}
    db.close()

    # 로그인 안 한 상태: 이름 선택 화면
    r = client.get(f"/r/{pid}")
    assert "본인 이름을 선택" in r.text

    # '가' 로 참여 (쿠키 발급)
    r = client.post(
        f"/r/{pid}/join", data={"participant_id": parts["가"]}, follow_redirects=False
    )
    assert r.status_code == 303
    assert f"tm_{pid}" in r.cookies or any("tm_" in c for c in client.cookies.keys())

    # 이제 순위 입력 폼
    r = client.get(f"/r/{pid}")
    assert "희망 순위" in r.text

    # 순위 제출 (나,다,라... 순서)
    order = ",".join(str(parts[n]) for n in ["나", "다", "라", "마", "바", "사", "아"])
    r = client.post(f"/r/{pid}/rank", data={"order": order}, follow_redirects=False)
    assert r.status_code == 303

    # 재접속: 진행현황 + 내가 낸 순위가 보임
    r = client.get(f"/r/{pid}")
    assert "등록 완료" in r.text
    assert "1 / 8명" in r.text
    assert "내가 낸 희망 순위" in r.text

    # DB 확인: 제출 저장됨
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    me = next(p for p in rnd.participants if p.name == "가")
    assert me.submitted is True
    assert me.preference_json[0] == parts["나"]
    db.close()


def test_edit_mode_header_and_saved_notice(ctx):
    """수정 모드 화면(수정/취소 표시) + 저장 안내 배너."""
    client, Session, _ = ctx
    pid = _create_round(client)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    parts = {p.name: p.id for p in rnd.participants}
    db.close()
    client.post(f"/r/{pid}/join", data={"participant_id": parts["가"]}, follow_redirects=False)
    order = ",".join(str(parts[n]) for n in ["나", "다", "라", "마", "바", "사", "아"])
    r = client.post(f"/r/{pid}/rank", data={"order": order}, follow_redirects=False)
    assert "saved=1" in r.headers["location"]  # 제출 후 저장 안내로 이동

    # 수정 화면: '수정' 표시 + 취소 버튼
    r = client.get(f"/r/{pid}/edit")
    assert "수정" in r.text
    assert "취소" in r.text
    # 저장 안내 배너
    r = client.get(f"/r/{pid}?saved=1")
    assert "저장됐어요" in r.text


def test_edit_blocked_after_finalize_shows_closed(ctx):
    """확정 후 수정 시도 → 마감 안내로 유도."""
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    gaid = next(p.id for p in rnd.participants if p.name == "가")
    db.close()
    client.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)
    _fill_all_preferences(Session, pid)
    client.post(f"/rounds/{pid}/finalize", follow_redirects=False)

    r = client.get(f"/r/{pid}/edit", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "closed=1" in r.headers["location"]
    r = client.get(f"/r/{pid}?closed=1")
    assert "마감" in r.text


def test_claimed_name_cannot_be_taken_twice(ctx):
    client, Session, _ = ctx
    pid = _create_round(client)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    gaid = next(p.id for p in rnd.participants if p.name == "가")
    db.close()

    client.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)
    # 새 손님(쿠키 없음)으로 같은 이름 시도
    fresh = TestClient(app)
    r = fresh.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 자동 배정 / 확정 / 수동 조정
# ---------------------------------------------------------------------------
def _fill_all_preferences(Session, pid):
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    ids = [p.id for p in rnd.participants]
    for p in rnd.participants:
        p.preference_json = [i for i in ids if i != p.id]
        p.submitted = True
        p.claimed = True
    db.commit()
    db.close()


def test_finalize_produces_full_grouping(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)

    r = client.post(f"/rounds/{pid}/finalize", follow_redirects=False)
    assert r.status_code == 303

    r = client.get(f"/rounds/{pid}/manage")
    assert "최종 조편성" in r.text
    assert "1조" in r.text and "2조" in r.text

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.status == "finalized"
    flat = sorted(n for g in rnd.result_json["groups"] for n in g)
    assert flat == sorted(["가", "나", "다", "라", "마", "바", "사", "아"])
    assert len(rnd.result_json["groups"]) == 2
    db.close()


def test_participant_sees_their_group_after_finalize(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    gaid = next(p.id for p in rnd.participants if p.name == "가")
    db.close()
    client.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)

    _fill_all_preferences(Session, pid)
    client.post(f"/rounds/{pid}/finalize", follow_redirects=False)

    r = client.get(f"/r/{pid}")
    assert "조편성이 확정됐어요" in r.text
    assert "(나)" in r.text  # 본인 표시


def test_manual_swap_moves_members(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)
    client.post(f"/rounds/{pid}/finalize", follow_redirects=False)

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    g0, g1 = rnd.result_json["groups"]
    a, b = g0[0], g1[0]
    db.close()

    r = client.post(
        f"/rounds/{pid}/swap", data={"name_a": a, "name_b": b}, follow_redirects=False
    )
    assert r.status_code == 303

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    new_g0, new_g1 = rnd.result_json["groups"]
    assert b in new_g0 and a in new_g1  # 서로 조가 바뀜
    db.close()


def test_edit_rename_keeps_preferences(ctx):
    """이름을 바꿔도 (ID 기반) 기존 희망순위가 유지되는지."""
    client, Session, _ = ctx
    pid = _create_round(client, names=["가", "나", "다", "라"])
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    ids = {p.name: p.id for p in rnd.participants}
    # '가' 가 [나,다,라] 순위 제출
    me = next(p for p in rnd.participants if p.name == "가")
    me.preference_json = [ids["나"], ids["다"], ids["라"]]
    me.submitted = True
    db.commit()
    db.close()

    # '나' -> '나나' 로 개명
    r = client.post(
        f"/rounds/{pid}/participants",
        data={"group_count": 2, f"name_{ids['가']}": "가", f"name_{ids['나']}": "나나",
              f"name_{ids['다']}": "다", f"name_{ids['라']}": "라", "new_names": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    names = {p.name for p in rnd.participants}
    assert "나나" in names and "나" not in names
    me = next(p for p in rnd.participants if p.name == "가")
    assert me.preference_json == [ids["나"], ids["다"], ids["라"]]  # ID 그대로 → 순위 유지
    assert me.submitted is True
    db.close()


def test_edit_add_and_remove_participant(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, names=["가", "나", "다", "라"])
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    ids = {p.name: p.id for p in rnd.participants}
    # '가' 순위에 '라' 포함
    me = next(p for p in rnd.participants if p.name == "가")
    me.preference_json = [ids["나"], ids["다"], ids["라"]]
    me.submitted = True
    db.commit()
    db.close()

    # '라' 삭제 + '마' 추가
    data = {"group_count": 2, "new_names": "마"}
    for n, i in ids.items():
        data[f"name_{i}"] = n
    data[f"delete_{ids['라']}"] = "1"
    r = client.post(f"/rounds/{pid}/participants", data=data, follow_redirects=False)
    assert r.status_code == 303

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    names = {p.name for p in rnd.participants}
    assert names == {"가", "나", "다", "마"}  # 라 빠지고 마 추가
    me = next(p for p in rnd.participants if p.name == "가")
    assert ids["라"] not in me.preference_json  # 삭제된 사람 참조 정리됨
    db.close()


def test_edit_rejects_duplicate_names(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, names=["가", "나", "다", "라"])
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    ids = {p.name: p.id for p in rnd.participants}
    db.close()
    # '나' 를 '가' 로 바꿔 중복 유발
    data = {"group_count": 2, "new_names": ""}
    for n, i in ids.items():
        data[f"name_{i}"] = n
    data[f"name_{ids['나']}"] = "가"
    r = client.post(f"/rounds/{pid}/participants", data=data, follow_redirects=False)
    assert r.status_code == 400


def test_edit_blocked_when_finalized(ctx):
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)
    client.post(f"/rounds/{pid}/finalize", follow_redirects=False)
    r = client.post(
        f"/rounds/{pid}/participants",
        data={"group_count": 2, "new_names": "새사람"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_outsider_cannot_manage_round(ctx):
    client, Session, org_id = ctx
    pid = _create_round(client)
    # 다른 주최자로 오버라이드
    db = Session()
    other = Organizer(google_sub="other", email="o@test.com", name="딴사람")
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()

    def as_other():
        s = Session()
        try:
            yield s.get(Organizer, other_id)
        finally:
            s.close()

    app.dependency_overrides[current_organizer] = as_other
    r = client.get(f"/rounds/{pid}/manage")
    assert r.status_code == 403
