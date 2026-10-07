"""티메이트 웹 서버.

주최자: 구글 로그인 → 라운딩 생성 → 공유 링크 → 자동 배정/확정
참가자: 링크 → 이름 선택 → 희망 순위 등록 → (재접속 시) 진행현황/결과 확인
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import config, tasks
from app.auth import current_organizer, router as auth_router
from app.database import get_db, init_db
from app.models import Organizer, Participant, Round
from tools.matcher import describe_grouping


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="티메이트 (TeeMate)", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["APP_BASE_URL"] = config.APP_BASE_URL
templates.env.globals["GOOGLE_SITE_VERIFICATION"] = config.GOOGLE_SITE_VERIFICATION
templates.env.globals["NAVER_SITE_VERIFICATION"] = config.NAVER_SITE_VERIFICATION


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return (
        "User-agent: *\n"
        "Disallow: /r/\n"
        "Disallow: /rounds\n"
        "Disallow: /login\n"
        "Disallow: /logout\n"
        "Disallow: /auth\n"
        f"Sitemap: {config.APP_BASE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{config.APP_BASE_URL}/</loc>"
        "<changefreq>monthly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{config.APP_BASE_URL}/guide</loc>"
        "<changefreq>monthly</changefreq><priority>0.8</priority></url>\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """브라우저 보안 헤더 추가 (HSTS, 콘텐츠 스니핑/클릭재킹 방지)."""
    response = await call_next(request)
    if config.COOKIE_SECURE:  # 운영(https)에서만 HSTS
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _cookie_name(public_id: str) -> str:
    return f"tm_{public_id}"


def _get_round_or_404(db: Session, public_id: str) -> Round:
    rnd = db.query(Round).filter_by(public_id=public_id).first()
    if rnd is None:
        raise HTTPException(status_code=404, detail="라운딩을 찾을 수 없습니다.")
    return rnd


def _require_owner(rnd: Round, organizer: Optional[Organizer]) -> None:
    if organizer is None or rnd.organizer_id != organizer.id:
        raise HTTPException(status_code=403, detail="이 라운딩의 주최자만 접근할 수 있습니다.")


def _countdown_ctx(rnd: Round) -> dict:
    """마감 카운트다운 + 미응답자 명단 (화면 실시간 표시용)."""
    deadline_iso = None
    if rnd.deadline is not None:
        deadline_iso = rnd.deadline.replace(microsecond=0).isoformat() + "Z"
    return {
        "deadline_iso": deadline_iso,
        "non_responders": [p.name for p in rnd.participants if not p.submitted],
    }


# ---------------------------------------------------------------------------
# 주최자 화면
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    if organizer is None:
        return templates.TemplateResponse(
            "landing.html",
            {"request": request, "google_enabled": config.GOOGLE_LOGIN_ENABLED,
             "error": request.query_params.get("error")},
        )
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "organizer": organizer, "rounds": organizer.rounds}
    )


@app.get("/rounds/new", response_class=HTMLResponse)
def round_new_form(
    request: Request, organizer: Optional[Organizer] = Depends(current_organizer)
):
    if organizer is None:
        return RedirectResponse("/login")
    return templates.TemplateResponse("round_new.html", {"request": request, "organizer": organizer})


def _clean_close_minutes(value: int) -> int:
    """자동 마감 시간을 30분 단위(30~360)로 정리."""
    step = config.AUTO_CLOSE_STEP_MINUTES
    value = max(step, min(360, int(value)))
    return (value // step) * step


@app.post("/rounds")
def create_round(
    request: Request,
    title: str = Form(...),
    group_count: int = Form(...),
    names: str = Form(...),
    auto_close_minutes: int = Form(config.AUTO_CLOSE_DEFAULT_MINUTES),
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    if organizer is None:
        return RedirectResponse("/login")

    name_list = [n.strip() for n in names.replace(",", "\n").splitlines() if n.strip()]
    # 중복 제거(순서 유지)
    seen: set[str] = set()
    unique_names = []
    for n in name_list:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if len(unique_names) < 2:
        raise HTTPException(status_code=400, detail="참가자는 최소 2명 이상이어야 합니다.")
    if group_count < 1 or group_count > len(unique_names):
        raise HTTPException(status_code=400, detail="조 개수가 참가자 수에 비해 올바르지 않습니다.")

    rnd = Round(
        organizer_id=organizer.id,
        title=title.strip() or "골프 라운딩",
        group_count=group_count,
        auto_close_minutes=_clean_close_minutes(auto_close_minutes),
    )
    for n in unique_names:
        rnd.participants.append(Participant(name=n))
    db.add(rnd)
    db.commit()
    db.refresh(rnd)
    return RedirectResponse(f"/rounds/{rnd.public_id}/manage", status_code=303)


@app.get("/rounds/{public_id}/manage", response_class=HTMLResponse)
def round_manage(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    rnd = _get_round_or_404(db, public_id)
    if organizer is None:  # 로그인 안 했으면 로그인 후 이 페이지로 복귀
        return RedirectResponse(f"/login?next=/rounds/{public_id}/manage")
    _require_owner(rnd, organizer)
    tasks.auto_close_if_due(db, rnd, str(request.base_url))  # 마감시각이 지났으면 자동 확정
    share_url = str(request.base_url).rstrip("/") + f"/r/{rnd.public_id}"
    return templates.TemplateResponse(
        "round_manage.html",
        {
            "request": request,
            "organizer": organizer,
            "rnd": rnd,
            "share_url": share_url,
            **_countdown_ctx(rnd),
        },
    )


@app.post("/rounds/{public_id}/participants")
async def edit_participants(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    """명단 수정: 이름 변경·삭제·추가 + 조 개수 변경. (모으는 중에만 가능)"""
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)
    if rnd.status == "finalized":
        raise HTTPException(
            status_code=400, detail="확정된 라운딩입니다. 먼저 '다시 열기'를 눌러 수정하세요."
        )

    form = await request.form()
    try:
        group_count = int(form.get("group_count", rnd.group_count))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="조 개수가 올바르지 않습니다.")
    try:
        new_close = _clean_close_minutes(int(form.get("auto_close_minutes", rnd.auto_close_minutes)))
    except (ValueError, TypeError):
        new_close = rnd.auto_close_minutes

    existing = list(rnd.participants)
    delete_ids = {p.id for p in existing if form.get(f"delete_{p.id}")}
    keep = [p for p in existing if p.id not in delete_ids]
    renamed = {p.id: str(form.get(f"name_{p.id}") or p.name).strip() for p in keep}
    new_names = [
        n.strip()
        for n in str(form.get("new_names", "")).replace(",", "\n").splitlines()
        if n.strip()
    ]

    final_names = [renamed[p.id] for p in keep] + new_names
    if any(not n for n in final_names):
        raise HTTPException(status_code=400, detail="빈 이름은 쓸 수 없습니다.")
    if len(final_names) < 2:
        raise HTTPException(status_code=400, detail="참가자는 최소 2명 이상이어야 합니다.")
    if len(set(final_names)) != len(final_names):
        raise HTTPException(status_code=400, detail="이름이 중복됩니다. 서로 다르게 해주세요.")
    if group_count < 1 or group_count > len(final_names):
        raise HTTPException(
            status_code=400, detail="조 개수가 참가자 수에 비해 올바르지 않습니다."
        )

    # 적용
    for p in existing:
        if p.id in delete_ids:
            db.delete(p)
    for p in keep:
        p.name = renamed[p.id]
    for n in new_names:
        rnd.participants.append(Participant(name=n))
    rnd.group_count = group_count
    # 마감 시간 변경 반영 (이미 카운트다운 중이면 그만큼 마감시각 이동)
    if new_close != rnd.auto_close_minutes:
        if rnd.deadline is not None:
            rnd.deadline = rnd.deadline + timedelta(minutes=(new_close - rnd.auto_close_minutes))
        rnd.auto_close_minutes = new_close

    # 삭제된 사람 참조를 남은 참가자 희망순위에서 제거
    if delete_ids:
        for p in keep:
            if p.preference_json:
                cleaned = [i for i in p.preference_json if i not in delete_ids]
                if cleaned != p.preference_json:
                    p.preference_json = cleaned
    rnd.result_json = None  # 명단이 바뀌면 이전 자동배정 결과는 무효
    db.commit()
    return RedirectResponse(f"/rounds/{public_id}/manage", status_code=303)


@app.post("/rounds/{public_id}/finalize")
def finalize_round(
    public_id: str,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)
    tasks.finalize_round(db, rnd)  # 주최자가 직접 즉시 확정 (메일 없음)
    return RedirectResponse(f"/rounds/{public_id}/manage", status_code=303)


@app.post("/rounds/{public_id}/reopen")
def reopen_round(
    public_id: str,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)
    rnd.status = "collecting"
    db.commit()
    return RedirectResponse(f"/rounds/{public_id}/manage", status_code=303)


@app.post("/rounds/{public_id}/delete")
def delete_round(
    public_id: str,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    """주최자가 자기 라운딩을 완전히 삭제(참가자·순위·결과 포함)."""
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)
    db.delete(rnd)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/rounds/{public_id}/swap")
def swap_members(
    public_id: str,
    name_a: str = Form(...),
    name_b: str = Form(...),
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    """확정 결과에서 두 사람의 조를 맞바꾸는 수동 조정."""
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)
    if not rnd.result_json:
        raise HTTPException(status_code=400, detail="먼저 자동 배정을 해주세요.")

    groups = [list(g) for g in rnd.result_json["groups"]]
    pos = {}
    for gi, g in enumerate(groups):
        for idx, nm in enumerate(g):
            pos[nm] = (gi, idx)
    if name_a not in pos or name_b not in pos:
        raise HTTPException(status_code=400, detail="참가자를 찾을 수 없습니다.")
    (ga, ia), (gb, ib) = pos[name_a], pos[name_b]
    groups[ga][ia], groups[gb][ib] = groups[gb][ib], groups[ga][ia]

    _, prefs = tasks.build_preferences(rnd)
    total, per_person = describe_grouping(groups, prefs, mutual_bonus=rnd.mutual_bonus)
    rnd.result_json = tasks.serialize_result(total, per_person, groups, rnd.result_json.get("exact", True))
    db.commit()
    return RedirectResponse(f"/rounds/{public_id}/manage", status_code=303)


# ---------------------------------------------------------------------------
# 참가자 화면 (로그인 없음, 링크 + 쿠키)
# ---------------------------------------------------------------------------
def _identify_participant(request: Request, rnd: Round) -> Optional[Participant]:
    token = request.cookies.get(_cookie_name(rnd.public_id))
    if not token:
        return None
    return next((p for p in rnd.participants if p.token == token), None)


@app.get("/r/{public_id}", response_class=HTMLResponse)
def participant_entry(
    public_id: str, request: Request, db: Session = Depends(get_db)
):
    rnd = _get_round_or_404(db, public_id)
    tasks.auto_close_if_due(db, rnd, str(request.base_url))  # 마감시각이 지났으면 자동 확정
    me = _identify_participant(request, rnd)

    # 확정된 라운딩: 결과는 누구나(이름 선택 없이도) 볼 수 있다. 본인이면 내 조 강조.
    if rnd.status == "finalized":
        return _render_status(request, rnd, me)
    # 수집 중: 본인 식별이 안 되면 이름 선택 화면
    if me is None:
        return templates.TemplateResponse(
            "participant_join.html",
            {"request": request, "rnd": rnd, "og_title": f"{rnd.title} · 티메이트 조편성"},
        )
    if me.submitted:
        return _render_status(request, rnd, me)
    return _render_rank_form(request, rnd, me)


def _render_status(request: Request, rnd: Round, me: Participant) -> HTMLResponse:
    """참가자 재접속 시: 진행현황 + 내가 낸 순위 + (확정 시) 내 조."""
    notice = None
    if request.query_params.get("closed"):
        notice = "closed"
    elif request.query_params.get("saved"):
        notice = "saved"
    id_to_name = {p.id: p.name for p in rnd.participants}
    my_ranking = (
        [id_to_name[i] for i in (me.preference_json or []) if i in id_to_name] if me else []
    )
    my_group: Optional[list[str]] = None
    my_group_index: Optional[int] = None
    if me and rnd.status == "finalized" and rnd.result_json:
        for gi, group in enumerate(rnd.result_json["groups"]):
            if me.name in group:
                my_group = group
                my_group_index = gi
                break
    return templates.TemplateResponse(
        "participant_status.html",
        {
            "request": request,
            "rnd": rnd,
            "me": me,
            "my_ranking": my_ranking,
            "my_group": my_group,
            "my_group_index": my_group_index,
            "notice": notice,
            "og_title": f"{rnd.title} · 티메이트 조편성",
            **_countdown_ctx(rnd),
        },
    )


def _render_rank_form(request: Request, rnd: Round, me: Participant) -> HTMLResponse:
    others = [p for p in rnd.participants if p.id != me.id]
    # 이미 저장된 순위가 있으면 그 순서대로, 없으면 명단 순서
    if me.preference_json:
        order = {pid: i for i, pid in enumerate(me.preference_json)}
        others.sort(key=lambda p: order.get(p.id, 999))
    return templates.TemplateResponse(
        "participant_rank.html",
        {
            "request": request,
            "rnd": rnd,
            "me": me,
            "others": others,
            "editing": me.submitted,  # 이미 제출한 적 있으면 '수정' 모드
            "og_title": f"{rnd.title} · 티메이트 조편성",
        },
    )


@app.post("/r/{public_id}/join")
def participant_join(
    public_id: str,
    participant_id: int = Form(...),
    db: Session = Depends(get_db),
):
    rnd = _get_round_or_404(db, public_id)
    me = next((p for p in rnd.participants if p.id == participant_id), None)
    if me is None:
        raise HTTPException(status_code=404, detail="참가자를 찾을 수 없습니다.")
    # 이미 선택된 이름도 다시 선택 가능(재접속·결과확인용). 쿠키를 다시 발급한다.
    me.claimed = True
    db.commit()
    resp = RedirectResponse(f"/r/{public_id}", status_code=303)
    resp.set_cookie(
        _cookie_name(public_id),
        me.token,
        max_age=60 * 60 * 24 * 60,  # 60일
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
    )
    return resp


@app.get("/r/{public_id}/edit", response_class=HTMLResponse)
def participant_edit(public_id: str, request: Request, db: Session = Depends(get_db)):
    rnd = _get_round_or_404(db, public_id)
    me = _identify_participant(request, rnd)
    if me is None:
        return RedirectResponse(f"/r/{public_id}")
    if rnd.status == "finalized":
        return RedirectResponse(f"/r/{public_id}?closed=1")
    return _render_rank_form(request, rnd, me)


@app.post("/r/{public_id}/rank")
def participant_rank_submit(
    public_id: str,
    request: Request,
    order: str = Form(...),  # 쉼표로 이어진 participant_id 들 (희망 순서)
    db: Session = Depends(get_db),
):
    rnd = _get_round_or_404(db, public_id)
    me = _identify_participant(request, rnd)
    if me is None:
        return RedirectResponse(f"/r/{public_id}")
    if rnd.status == "finalized":
        return RedirectResponse(f"/r/{public_id}?closed=1", status_code=303)

    valid_ids = {p.id for p in rnd.participants if p.id != me.id}
    ordered = []
    for chunk in order.split(","):
        chunk = chunk.strip()
        if chunk.isdigit() and int(chunk) in valid_ids and int(chunk) not in ordered:
            ordered.append(int(chunk))
    me.preference_json = ordered
    me.submitted = True
    me.submitted_at = datetime.now(timezone.utc)
    # 첫 제출이면 자동 마감 카운트다운 시작
    if rnd.deadline is None:
        rnd.deadline = tasks.utcnow() + timedelta(minutes=rnd.auto_close_minutes)
    db.commit()
    return RedirectResponse(f"/r/{public_id}?saved=1", status_code=303)
