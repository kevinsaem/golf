"""티메이트 웹 서버.

주최자: 구글 로그인 → 라운딩 생성 → 공유 링크 → 자동 배정/확정
참가자: 링크 → 이름 선택 → 희망 순위 등록 → (재접속 시) 진행현황/결과 확인
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.auth import current_organizer, router as auth_router
from app.database import get_db, init_db
from app.models import Organizer, Participant, Round
from tools.matcher import describe_grouping, match_groups


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


def _build_preferences(rnd: Round) -> tuple[list[str], dict[str, list[str]]]:
    """DB 참가자/희망순위 -> 매처가 쓰는 (이름목록, 선호도dict)."""
    names = [p.name for p in rnd.participants]
    id_to_name = {p.id: p.name for p in rnd.participants}
    prefs: dict[str, list[str]] = {}
    for p in rnd.participants:
        order = p.preference_json or []
        prefs[p.name] = [id_to_name[i] for i in order if i in id_to_name]
    return names, prefs


def _serialize_result(total: float, per_person, groups: list[list[str]], exact: bool) -> dict:
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


@app.post("/rounds")
def create_round(
    request: Request,
    title: str = Form(...),
    group_count: int = Form(...),
    names: str = Form(...),
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
    _require_owner(rnd, organizer)
    share_url = str(request.base_url).rstrip("/") + f"/r/{rnd.public_id}"
    return templates.TemplateResponse(
        "round_manage.html",
        {"request": request, "organizer": organizer, "rnd": rnd, "share_url": share_url},
    )


@app.post("/rounds/{public_id}/finalize")
def finalize_round(
    public_id: str,
    db: Session = Depends(get_db),
    organizer: Optional[Organizer] = Depends(current_organizer),
):
    rnd = _get_round_or_404(db, public_id)
    _require_owner(rnd, organizer)

    names, prefs = _build_preferences(rnd)
    result = match_groups(names, prefs, group_count=rnd.group_count, mutual_bonus=rnd.mutual_bonus)
    rnd.result_json = _serialize_result(
        result.total_score, result.per_person, result.groups, result.exact
    )
    rnd.status = "finalized"
    db.commit()
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

    _, prefs = _build_preferences(rnd)
    total, per_person = describe_grouping(groups, prefs, mutual_bonus=rnd.mutual_bonus)
    rnd.result_json = _serialize_result(total, per_person, groups, rnd.result_json.get("exact", True))
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
    me = _identify_participant(request, rnd)

    if me is None:
        return templates.TemplateResponse(
            "participant_join.html", {"request": request, "rnd": rnd}
        )
    # 확정됐거나 이미 제출했으면 현황/결과, 아니면 순위 입력 폼
    if rnd.status == "finalized" or me.submitted:
        return _render_status(request, rnd, me)
    return _render_rank_form(request, rnd, me)


def _render_status(request: Request, rnd: Round, me: Participant) -> HTMLResponse:
    """참가자 재접속 시: 진행현황 + 내가 낸 순위 + (확정 시) 내 조."""
    id_to_name = {p.id: p.name for p in rnd.participants}
    my_ranking = [id_to_name[i] for i in (me.preference_json or []) if i in id_to_name]
    my_group: Optional[list[str]] = None
    my_group_index: Optional[int] = None
    if rnd.status == "finalized" and rnd.result_json:
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
        },
    )


def _render_rank_form(request: Request, rnd: Round, me: Participant) -> HTMLResponse:
    others = [p for p in rnd.participants if p.id != me.id]
    # 이미 저장된 순위가 있으면 그 순서대로, 없으면 명단 순서
    if me.preference_json:
        order = {pid: i for i, pid in enumerate(me.preference_json)}
        others.sort(key=lambda p: order.get(p.id, 999))
    return templates.TemplateResponse(
        "participant_rank.html", {"request": request, "rnd": rnd, "me": me, "others": others}
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
    if me.claimed:
        raise HTTPException(status_code=409, detail="이미 선택된 이름입니다.")
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
        return RedirectResponse(f"/r/{public_id}")
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
        return RedirectResponse(f"/r/{public_id}")

    valid_ids = {p.id for p in rnd.participants if p.id != me.id}
    ordered = []
    for chunk in order.split(","):
        chunk = chunk.strip()
        if chunk.isdigit() and int(chunk) in valid_ids and int(chunk) not in ordered:
            ordered.append(int(chunk))
    me.preference_json = ordered
    me.submitted = True
    me.submitted_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(f"/r/{public_id}", status_code=303)
