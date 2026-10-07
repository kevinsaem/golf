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


def test_landing_intro_and_seo_when_logged_out(ctx):
    """로그아웃 방문자(검색봇)에게 소개·사용법·SEO 메타가 보인다."""
    client, _, _ = ctx
    app.dependency_overrides[current_organizer] = lambda: None
    r = client.get("/")
    assert "티메이트가 뭔가요?" in r.text
    assert "이렇게 써요" in r.text
    assert 'name="description"' in r.text
    assert 'name="keywords"' in r.text
    assert "application/ld+json" in r.text  # 구조화 데이터


def test_robots_and_sitemap(ctx):
    client, _, _ = ctx
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap:" in r.text
    assert "Disallow: /r/" in r.text
    s = client.get("/sitemap.xml")
    assert s.status_code == 200
    assert "<urlset" in s.text and "/sitemap" not in s.text.split("urlset")[0]


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
    assert "drag-card" in r.text  # 카드 전체가 드래그 영역
    assert "Sortable" in r.text

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


def test_claimed_name_can_be_reselected(ctx):
    """이미 선택된 이름도 (다른 기기/쿠키 유실 시) 다시 선택해 이어갈 수 있다."""
    client, Session, _ = ctx
    pid = _create_round(client)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    gaid = next(p.id for p in rnd.participants if p.name == "가")
    db.close()

    client.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)
    # 새 손님(쿠키 없음)으로 같은 이름 재선택 → 허용(쿠키 재발급)
    fresh = TestClient(app)
    r = fresh.post(f"/r/{pid}/join", data={"participant_id": gaid}, follow_redirects=False)
    assert r.status_code == 303
    assert any("tm_" in k for k in fresh.cookies.keys())


def test_finalized_result_visible_without_cookie(ctx):
    """확정 후에는 쿠키/이름 선택 없이 링크만 열어도 전체 조편성이 보인다."""
    client, Session, _ = ctx
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)
    client.post(f"/rounds/{pid}/finalize", follow_redirects=False)

    anon = TestClient(app)  # 쿠키 없는 방문자
    r = anon.get(f"/r/{pid}")
    assert r.status_code == 200
    assert "조편성 결과" in r.text
    assert "전체 조편성" in r.text
    assert "가" in r.text and "아" in r.text  # 참가자들이 결과에 보임


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


def test_deadline_set_on_first_submission(ctx):
    """첫 제출 시 마감 카운트다운(deadline)이 설정된다."""
    from app.tasks import utcnow

    client, Session, _ = ctx
    pid = _create_round(client, names=["가", "나", "다", "라"], group_count=2)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.deadline is None
    ids = {p.name: p.id for p in rnd.participants}
    acm = rnd.auto_close_minutes
    db.close()

    c = TestClient(app)
    c.post(f"/r/{pid}/join", data={"participant_id": ids["가"]}, follow_redirects=False)
    c.post(f"/r/{pid}/rank", data={"order": f"{ids['나']},{ids['다']},{ids['라']}"}, follow_redirects=False)

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.deadline is not None
    delta = (rnd.deadline - utcnow()).total_seconds()
    assert acm * 60 - 120 < delta <= acm * 60 + 5  # 대략 설정 시간 뒤
    db.close()


def test_auto_close_finalizes_when_due(ctx, monkeypatch):
    """마감 시각이 지나면 접속 시 자동 확정되고 주최자에게 1회 메일."""
    from datetime import timedelta

    import app.tasks as t

    client, Session, _ = ctx
    calls = []
    monkeypatch.setattr(t, "send_email", lambda *a, **k: calls.append(a) or True)
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    rnd.deadline = t.utcnow() - timedelta(minutes=1)  # 이미 지난 마감
    db.commit()
    db.close()

    anon = TestClient(app)  # 아무나 접속해도 자동 확정 트리거
    r = anon.get(f"/r/{pid}")
    assert r.status_code == 200

    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.status == "finalized"
    assert rnd.result_json is not None
    db.close()
    assert len(calls) == 1  # 주최자 자동마감 메일 1회


def test_create_round_cleans_close_minutes_to_30_step(ctx):
    client, Session, _ = ctx
    r = client.post(
        "/rounds",
        data={"title": "t", "group_count": 2, "names": "가\n나\n다\n라", "auto_close_minutes": 50},
        follow_redirects=False,
    )
    pid = r.headers["location"].split("/")[2]
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.auto_close_minutes == 30  # 50 → 30분 단위로 내림
    db.close()


def test_delete_round(ctx):
    """주최자는 자기 라운딩을 삭제할 수 있고, 참가자까지 함께 지워진다."""
    from app.models import Participant

    client, Session, _ = ctx
    pid = _create_round(client)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    rid = rnd.id
    db.close()

    r = client.post(f"/rounds/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    db = Session()
    assert db.query(Round).filter_by(public_id=pid).first() is None
    assert db.query(Participant).filter_by(round_id=rid).count() == 0  # 참가자도 삭제
    db.close()


def test_outsider_cannot_delete_round(ctx):
    client, Session, org_id = ctx
    pid = _create_round(client)
    db = Session()
    other = Organizer(google_sub="other-del", email="o2@test.com", name="딴사람2")
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
    r = client.post(f"/rounds/{pid}/delete", follow_redirects=False)
    assert r.status_code == 403
    db = Session()
    assert db.query(Round).filter_by(public_id=pid).first() is not None  # 안 지워짐
    db.close()


def test_all_submitted_finalizes_immediately(ctx, monkeypatch):
    """전원이 순위를 내면 마감시간을 안 기다리고 즉시 확정된다."""
    import app.tasks as t

    client, Session, _ = ctx
    monkeypatch.setattr(t, "send_email", lambda *a, **k: True)
    pid = _create_round(client, names=["가", "나"], group_count=1)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    ids = {p.name: p.id for p in rnd.participants}
    db.close()

    c1 = TestClient(app)
    c1.post(f"/r/{pid}/join", data={"participant_id": ids["가"]}, follow_redirects=False)
    c1.post(f"/r/{pid}/rank", data={"order": str(ids["나"])}, follow_redirects=False)
    db = Session()
    assert db.query(Round).filter_by(public_id=pid).first().status == "collecting"  # 1/2
    db.close()

    c2 = TestClient(app)
    c2.post(f"/r/{pid}/join", data={"participant_id": ids["나"]}, follow_redirects=False)
    c2.post(f"/r/{pid}/rank", data={"order": str(ids["가"])}, follow_redirects=False)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.status == "finalized"  # 2/2 → 즉시 확정
    assert rnd.result_json is not None
    db.close()


def test_existing_all_submitted_round_finalizes_on_view(ctx, monkeypatch):
    """이미 8/8인 기존 라운딩(마감시각 없음)을 열람하면 자동 확정된다."""
    import app.tasks as t

    client, Session, _ = ctx
    monkeypatch.setattr(t, "send_email", lambda *a, **k: True)
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)  # 전원 제출, deadline 없음(기존 라운딩처럼)
    db = Session()
    rnd = db.query(Round).filter_by(public_id=pid).first()
    assert rnd.status == "collecting" and rnd.deadline is None
    db.close()

    anon = TestClient(app)
    r = anon.get(f"/r/{pid}")  # 열람만 해도
    assert r.status_code == 200
    db = Session()
    assert db.query(Round).filter_by(public_id=pid).first().status == "finalized"
    db.close()


def test_sweep_finalizes_all_submitted_without_deadline(ctx, monkeypatch):
    """크론 sweep이 마감시각 없는 전원완료 라운딩도 확정한다."""
    import app.tasks as t

    client, Session, _ = ctx
    monkeypatch.setattr(t, "send_email", lambda *a, **k: True)
    pid = _create_round(client, group_count=2)
    _fill_all_preferences(Session, pid)
    db = Session()
    n = t.close_due_rounds(db, "http://t")
    db.close()
    assert n == 1
    db = Session()
    assert db.query(Round).filter_by(public_id=pid).first().status == "finalized"
    db.close()


def test_manage_redirects_to_login_when_not_logged_in(ctx):
    """비로그인으로 관리 링크를 열면 403이 아니라 로그인(복귀주소 포함)으로 보낸다."""
    client, Session, _ = ctx
    pid = _create_round(client)
    app.dependency_overrides[current_organizer] = lambda: None
    r = client.get(f"/rounds/{pid}/manage", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers["location"]
    assert f"next=/rounds/{pid}/manage" in r.headers["location"]


def test_notify_email_links_to_public_result(ctx, monkeypatch):
    """확정 알림 메일이 로그인 없이 열리는 결과 페이지(/r/..)를 가리킨다."""
    import app.tasks as t

    client, Session, _ = ctx
    captured = {}
    monkeypatch.setattr(
        t, "send_email",
        lambda to, subj, text, html=None: captured.update(text=text, html=html) or True,
    )
    pid = _create_round(client, names=["가", "나"], group_count=1)
    db = Session()
    ids = {p.name: p.id for p in db.query(Round).filter_by(public_id=pid).first().participants}
    db.close()
    c1 = TestClient(app)
    c1.post(f"/r/{pid}/join", data={"participant_id": ids["가"]}, follow_redirects=False)
    c1.post(f"/r/{pid}/rank", data={"order": str(ids["나"])}, follow_redirects=False)
    c2 = TestClient(app)
    c2.post(f"/r/{pid}/join", data={"participant_id": ids["나"]}, follow_redirects=False)
    c2.post(f"/r/{pid}/rank", data={"order": str(ids["가"])}, follow_redirects=False)
    assert f"/r/{pid}" in captured.get("text", "")
    assert f"/r/{pid}" in captured.get("html", "")


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
