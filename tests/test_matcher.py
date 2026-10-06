"""매칭 엔진 단위 테스트."""

from __future__ import annotations

from itertools import combinations

import pytest

from tools.matcher import (
    MAX_EXACT_PARTITIONS,
    _count_partitions,
    _iter_partitions,
    match_groups,
    resolve_group_sizes,
)


# ---------------------------------------------------------------------------
# 조 크기 계산
# ---------------------------------------------------------------------------
def test_resolve_sizes_even_split():
    assert resolve_group_sizes(8, group_count=2) == [4, 4]


def test_resolve_sizes_uneven_split():
    # 9명 2조 -> 5, 4 로 최대한 고르게
    assert resolve_group_sizes(9, group_count=2) == [5, 4]


def test_resolve_sizes_from_group_size():
    # 조당 4명 기본, 10명 -> 4, 3, 3
    assert resolve_group_sizes(10, group_size=4) == [4, 3, 3]


def test_resolve_sizes_explicit():
    assert resolve_group_sizes(7, group_sizes=[4, 3]) == [4, 3]


def test_resolve_sizes_bad_explicit_raises():
    with pytest.raises(ValueError):
        resolve_group_sizes(8, group_sizes=[4, 3])  # 합이 7


# ---------------------------------------------------------------------------
# 경우의 수 생성 (중복 없이 전수)
# ---------------------------------------------------------------------------
def test_partition_count_8_into_2x4():
    assert _count_partitions(8, [4, 4]) == 35


def test_partition_count_12_into_3x4():
    assert _count_partitions(12, [4, 4, 4]) == 5775


def test_iter_partitions_matches_count_and_is_unique():
    people = [f"p{i}" for i in range(8)]
    parts = list(_iter_partitions(people, [4, 4]))
    assert len(parts) == 35
    # 각 조편성은 전원을 정확히 한 번씩 포함해야 한다
    for groups in parts:
        flat = [p for g in groups for p in g]
        assert sorted(flat) == sorted(people)
    # 서로 다른 조편성이어야 한다 (프로즌셋 기준 중복 없음)
    signatures = {frozenset(frozenset(g) for g in groups) for groups in parts}
    assert len(signatures) == 35


# ---------------------------------------------------------------------------
# 핵심: 서로 원하는 사람끼리 같은 조가 되는가
# ---------------------------------------------------------------------------
def test_two_cliques_are_separated():
    """A·B·C·D 끼리, E·F·G·H 끼리 서로를 1~3지망으로 꼽으면
    정확히 두 무리로 갈라져야 한다."""
    clique1 = ["A", "B", "C", "D"]
    clique2 = ["E", "F", "G", "H"]
    prefs: dict[str, list[str]] = {}
    for person in clique1:
        others = [p for p in clique1 if p != person]
        prefs[person] = others + clique2  # 같은 무리를 위로
    for person in clique2:
        others = [p for p in clique2 if p != person]
        prefs[person] = others + clique1

    result = match_groups(clique1 + clique2, prefs, group_count=2)
    assert result.exact is True
    groups_as_sets = [set(g) for g in result.groups]
    assert set(clique1) in groups_as_sets
    assert set(clique2) in groups_as_sets


def test_everyone_gets_top_choice_when_consistent():
    """일관된 희망이면 모두가 1지망과 같은 조가 될 수 있다."""
    clique1 = ["A", "B", "C", "D"]
    clique2 = ["E", "F", "G", "H"]
    prefs = {}
    for person in clique1:
        prefs[person] = [p for p in clique1 if p != person] + clique2
    for person in clique2:
        prefs[person] = [p for p in clique2 if p != person] + clique1

    result = match_groups(clique1 + clique2, prefs, group_count=2)
    assert all(pr.got_top_choice for pr in result.per_person)


def test_result_is_globally_optimal_vs_bruteforce():
    """엔진 결과가 '정말로' 모든 경우의 수 중 최고 점수인지 직접 대조한다."""
    people = ["A", "B", "C", "D", "E", "F", "G", "H"]
    # 적당히 섞인 선호도
    prefs = {
        "A": ["B", "C", "D", "E", "F", "G", "H"],
        "B": ["A", "C", "E", "D", "F", "G", "H"],
        "C": ["A", "B", "D", "E", "F", "G", "H"],
        "D": ["C", "A", "B", "H", "E", "F", "G"],
        "E": ["F", "G", "H", "A", "B", "C", "D"],
        "F": ["E", "G", "H", "B", "A", "C", "D"],
        "G": ["E", "F", "H", "A", "B", "C", "D"],
        "H": ["E", "F", "G", "D", "A", "B", "C"],
    }
    from tools.matcher import _group_score, _points_map, _total_score

    pts = _points_map(people, prefs)
    mutual_bonus = 1.0
    brute_best = max(
        _total_score(groups, pts, mutual_bonus)
        for groups in _iter_partitions(people, [4, 4])
    )
    result = match_groups(people, prefs, group_count=2, mutual_bonus=mutual_bonus)
    assert result.total_score == brute_best


def test_mutual_bonus_prefers_reciprocated_pairs():
    """양방향 짝 가산점이 '짝사랑'보다 '서로 좋아함'을 우선하는지."""
    people = ["A", "B", "C", "D"]
    # A<->B 서로, C<->D 서로. 가산점 있으면 (A,B)(C,D) 로 나뉘어야 자연스러움.
    prefs = {
        "A": ["B", "C", "D"],
        "B": ["A", "C", "D"],
        "C": ["D", "A", "B"],
        "D": ["C", "A", "B"],
    }
    result = match_groups(people, prefs, group_count=2, mutual_bonus=5.0)
    groups_as_sets = [set(g) for g in result.groups]
    assert {"A", "B"} in groups_as_sets
    assert {"C", "D"} in groups_as_sets


# ---------------------------------------------------------------------------
# 투명성(설명) 출력
# ---------------------------------------------------------------------------
def test_per_person_breakdown_fields():
    people = ["A", "B", "C", "D"]
    prefs = {
        "A": ["B", "C", "D"],
        "B": ["A", "C", "D"],
        "C": ["D", "A", "B"],
        "D": ["C", "A", "B"],
    }
    result = match_groups(people, prefs, group_count=2, mutual_bonus=5.0)
    by_name = {pr.name: pr for pr in result.per_person}
    a = by_name["A"]
    assert a.group_index == result.group_of("A")
    assert len(a.mates) == 1  # 2명 조라 조원 1명
    mate = a.mates[0]
    assert mate.name == "B"
    assert mate.rank == 1  # A 는 B 를 1지망으로
    assert mate.mutual is True
    assert a.got_top_choice is True
    assert a.best_mate_rank == 1


# ---------------------------------------------------------------------------
# 엣지 케이스
# ---------------------------------------------------------------------------
def test_empty_preferences_still_returns_valid_grouping():
    people = ["A", "B", "C", "D", "E", "F", "G", "H"]
    result = match_groups(people, {}, group_count=2)
    flat = sorted(p for g in result.groups for p in g)
    assert flat == sorted(people)
    assert len(result.groups) == 2


def test_duplicate_names_raise():
    with pytest.raises(ValueError):
        match_groups(["A", "A", "B", "C"], {}, group_count=2)


def test_uneven_groups_handled():
    people = ["A", "B", "C", "D", "E"]
    result = match_groups(people, {}, group_count=2)
    sizes = sorted(len(g) for g in result.groups)
    assert sizes == [2, 3]


def test_large_group_uses_heuristic_and_is_valid():
    """20명(전수 계산 불가)이면 휴리스틱으로 전환하되 유효한 조편성을 낸다."""
    people = [f"P{i:02d}" for i in range(20)]
    assert _count_partitions(20, [4, 4, 4, 4, 4]) > MAX_EXACT_PARTITIONS
    # 간단한 선호: 번호가 가까운 사람끼리 선호
    prefs = {
        p: sorted((q for q in people if q != p), key=lambda q: abs(int(q[1:]) - int(p[1:])))
        for p in people
    }
    result = match_groups(people, prefs, group_count=5, seed=42)
    assert result.exact is False
    flat = sorted(f for g in result.groups for f in g)
    assert flat == sorted(people)
    assert all(len(g) == 4 for g in result.groups)
