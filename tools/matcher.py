"""골프 조편성 매칭 엔진.

각 참가자가 "같이 치고 싶은 순서"대로 나머지 참가자 전원을 줄 세우면(희망 순위),
이 모듈이 전원의 만족도가 최대가 되는 조편성을 찾아준다.

핵심 아이디어
-------------
- 희망 순위를 점수로 바꾼다. (1지망 = (상대 수)점, 꼴찌지망 = 1점)
- 한 조 안에서 "서로에게 준 점수의 합"이 그 조의 만족도다.
- 모든 조편성 경우의 수를 따져 전체 만족도가 가장 큰 조합을 고른다.
  (참가자가 적으면 전수 계산으로 '항상 최적', 많으면 휴리스틱으로 '거의 최적')
- 서로가 서로를 원하는 '양방향 짝'에는 가산점을 줘서 짝사랑보다 우선한다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import combinations
from math import comb, inf
from typing import Iterable, Iterator, Optional

# 전수 계산을 시도할 최대 경우의 수. 이보다 많으면 휴리스틱으로 전환한다.
MAX_EXACT_PARTITIONS = 300_000


@dataclass
class MateInfo:
    """결과에서 '나와 같은 조가 된 한 사람'에 대한 정보."""

    name: str
    rank: Optional[int]  # 내가 이 사람에게 매긴 희망 순위(1=1지망). 안 매겼으면 None
    mutual: bool  # 서로가 서로를 희망 목록에 넣었는지


@dataclass
class PersonResult:
    """한 참가자 입장에서 본 조편성 결과."""

    name: str
    group_index: int
    mates: list[MateInfo]
    got_top_choice: bool  # 1지망과 같은 조가 됐는가
    satisfied_count: int  # 같은 조가 된 사람 중 내 희망 목록에 있던 수
    best_mate_rank: Optional[int]  # 같은 조원 중 내가 가장 높게 매긴 순위


@dataclass
class MatchResult:
    """매칭 최종 결과."""

    groups: list[list[str]]
    total_score: float
    exact: bool  # True=전수 계산(최적 보장), False=휴리스틱(거의 최적)
    per_person: list[PersonResult] = field(default_factory=list)

    def group_of(self, name: str) -> int:
        for i, g in enumerate(self.groups):
            if name in g:
                return i
        raise ValueError(f"{name} 은(는) 어느 조에도 없습니다.")


# ---------------------------------------------------------------------------
# 조 크기 계산
# ---------------------------------------------------------------------------
def resolve_group_sizes(
    n: int,
    group_count: Optional[int] = None,
    group_size: int = 4,
    group_sizes: Optional[list[int]] = None,
) -> list[int]:
    """참가자 수(n)로부터 각 조의 인원수 목록을 정한다.

    우선순위: group_sizes(직접 지정) > group_count(조 개수) > group_size(조당 인원).
    조 개수로 나눌 때 딱 안 떨어지면 최대한 고르게 나눈다. (예: 9명 2조 -> [5, 4])
    """
    if group_sizes is not None:
        if sum(group_sizes) != n:
            raise ValueError(f"조 인원 합({sum(group_sizes)})이 참가자 수({n})와 다릅니다.")
        return sorted(group_sizes, reverse=True)

    if group_count is None:
        if group_size <= 0:
            raise ValueError("group_size 는 1 이상이어야 합니다.")
        group_count = max(1, -(-n // group_size))  # 올림 나눗셈

    if group_count <= 0 or group_count > n:
        raise ValueError(f"조 개수({group_count})가 참가자 수({n})에 비해 올바르지 않습니다.")

    base, extra = divmod(n, group_count)
    sizes = [base + 1] * extra + [base] * (group_count - extra)
    return sorted(sizes, reverse=True)


# ---------------------------------------------------------------------------
# 점수 계산
# ---------------------------------------------------------------------------
def _points_map(
    participants: list[str], preferences: dict[str, list[str]]
) -> dict[str, dict[str, int]]:
    """희망 순위를 점수표로 바꾼다. 1지망이 가장 높은 점수(=매긴 사람 수)를 받는다."""
    valid = set(participants)
    pts: dict[str, dict[str, int]] = {}
    for person in participants:
        ranking = [o for o in preferences.get(person, []) if o in valid and o != person]
        m = len(ranking)
        pts[person] = {other: (m - i) for i, other in enumerate(ranking)}
    return pts


def _group_score(
    group: list[str], pts: dict[str, dict[str, int]], mutual_bonus: float
) -> float:
    """한 조의 만족도 = 조원끼리 서로에게 준 점수의 합 (+ 양방향 짝 가산점)."""
    score = 0.0
    for p in group:
        pmap = pts.get(p, {})
        for q in group:
            if p != q:
                score += pmap.get(q, 0)
    if mutual_bonus:
        for p, q in combinations(group, 2):
            if pts.get(p, {}).get(q) and pts.get(q, {}).get(p):
                score += mutual_bonus
    return score


def _total_score(
    groups: Iterable[list[str]], pts: dict[str, dict[str, int]], mutual_bonus: float
) -> float:
    return sum(_group_score(g, pts, mutual_bonus) for g in groups)


# ---------------------------------------------------------------------------
# 조편성 경우의 수 생성
# ---------------------------------------------------------------------------
def _count_partitions(n: int, sizes: list[int]) -> int:
    """(중복 제외) 조편성 경우의 수를 미리 계산한다."""
    count = 1
    rem = n
    for s in sizes:
        count *= comb(rem - 1, s - 1)
        rem -= s
    return count


def _iter_partitions(elements: list[str], sizes: list[int]) -> Iterator[list[list[str]]]:
    """elements 를 주어진 크기(sizes)의 조들로 나누는 모든 방법을 생성한다.

    같은 크기 조의 순서로 인한 중복을 없애기 위해, 각 조는 항상
    '남은 원소 중 가장 앞 원소'를 포함하도록 고정한다.
    """
    if not sizes:
        yield []
        return
    first, *rest = sizes
    head = elements[0]
    others = elements[1:]
    for combo in combinations(others, first - 1):
        group = [head, *combo]
        chosen = set(combo)
        remaining = [e for e in others if e not in chosen]
        for tail in _iter_partitions(remaining, rest):
            yield [group, *tail]


def _solve_exact(
    participants: list[str], sizes: list[int], pts: dict[str, dict[str, int]], mutual_bonus: float
) -> tuple[list[list[str]], float]:
    best_groups: list[list[str]] = []
    best_score = -inf
    for groups in _iter_partitions(participants, sizes):
        score = _total_score(groups, pts, mutual_bonus)
        if score > best_score:
            best_score = score
            best_groups = [g[:] for g in groups]
    return best_groups, best_score


def _slice_into_groups(arr: list[str], sizes: list[int]) -> list[list[str]]:
    groups = []
    i = 0
    for s in sizes:
        groups.append(arr[i : i + s])
        i += s
    return groups


def _solve_heuristic(
    participants: list[str],
    sizes: list[int],
    pts: dict[str, dict[str, int]],
    mutual_bonus: float,
    seed: int,
    restarts: int = 12,
    iterations: int = 20_000,
) -> tuple[list[list[str]], float]:
    """참가자가 아주 많을 때 쓰는 근사 해법 (랜덤 재시작 + 지역 탐색)."""
    rng = random.Random(seed)
    best_groups: list[list[str]] = []
    best_score = -inf
    for _ in range(restarts):
        arr = participants[:]
        rng.shuffle(arr)
        groups = _slice_into_groups(arr, sizes)
        cur = _total_score(groups, pts, mutual_bonus)
        for _ in range(iterations):
            gi, gj = rng.sample(range(len(groups)), 2)
            ii = rng.randrange(len(groups[gi]))
            jj = rng.randrange(len(groups[gj]))
            groups[gi][ii], groups[gj][jj] = groups[gj][jj], groups[gi][ii]
            new = _total_score(groups, pts, mutual_bonus)
            if new >= cur:
                cur = new
            else:  # 개선 안 되면 되돌리기
                groups[gi][ii], groups[gj][jj] = groups[gj][jj], groups[gi][ii]
        if cur > best_score:
            best_score = cur
            best_groups = [g[:] for g in groups]
    return best_groups, best_score


# ---------------------------------------------------------------------------
# 결과 설명(투명성) 만들기
# ---------------------------------------------------------------------------
def _build_per_person(
    groups: list[list[str]], preferences: dict[str, list[str]]
) -> list[PersonResult]:
    results: list[PersonResult] = []
    name_to_group = {name: i for i, g in enumerate(groups) for name in g}
    for gi, group in enumerate(groups):
        for person in group:
            ranking = [o for o in preferences.get(person, []) if o != person]
            rank_of = {other: idx + 1 for idx, other in enumerate(ranking)}
            mates: list[MateInfo] = []
            for mate in group:
                if mate == person:
                    continue
                mate_ranks_me = person in preferences.get(mate, [])
                i_rank_mate = mate in rank_of
                mates.append(
                    MateInfo(
                        name=mate,
                        rank=rank_of.get(mate),
                        mutual=i_rank_mate and mate_ranks_me,
                    )
                )
            mate_ranks = [m.rank for m in mates if m.rank is not None]
            results.append(
                PersonResult(
                    name=person,
                    group_index=gi,
                    mates=sorted(mates, key=lambda m: (m.rank is None, m.rank or 0)),
                    got_top_choice=bool(ranking) and ranking[0] in name_to_group
                    and name_to_group[ranking[0]] == gi,
                    satisfied_count=len(mate_ranks),
                    best_mate_rank=min(mate_ranks) if mate_ranks else None,
                )
            )
    return results


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def match_groups(
    participants: Iterable[str],
    preferences: dict[str, list[str]],
    *,
    group_count: Optional[int] = None,
    group_size: int = 4,
    group_sizes: Optional[list[int]] = None,
    mutual_bonus: float = 1.0,
    seed: int = 0,
) -> MatchResult:
    """희망 순위로부터 최적(또는 거의 최적) 조편성을 계산한다.

    Parameters
    ----------
    participants : 참가자 이름 목록 (중복 불가)
    preferences  : {참가자: [같이 치고 싶은 순서대로 나열한 나머지 참가자]}
                   일부만 적거나 비워도 된다.
    group_count  : 조 개수 (예: 2 -> 1조·2조)
    group_size   : 조당 인원 (group_count 미지정 시 사용, 기본 4)
    group_sizes  : 조별 인원을 직접 지정 (예: [4, 3])
    mutual_bonus : 양방향 짝 가산점 (0이면 끔)
    seed         : 휴리스틱 사용 시 재현성을 위한 난수 시드

    Returns
    -------
    MatchResult
    """
    participants = list(participants)
    if len(set(participants)) != len(participants):
        raise ValueError("참가자 이름이 중복됩니다. 이름을 서로 다르게 해주세요.")
    n = len(participants)
    if n == 0:
        raise ValueError("참가자가 없습니다.")

    sizes = resolve_group_sizes(n, group_count, group_size, group_sizes)
    pts = _points_map(participants, preferences)

    total_partitions = _count_partitions(n, sizes)
    if total_partitions <= MAX_EXACT_PARTITIONS:
        groups, score = _solve_exact(participants, sizes, pts, mutual_bonus)
        exact = True
    else:
        groups, score = _solve_heuristic(participants, sizes, pts, mutual_bonus, seed)
        exact = False

    per_person = _build_per_person(groups, preferences)
    return MatchResult(groups=groups, total_score=score, exact=exact, per_person=per_person)
