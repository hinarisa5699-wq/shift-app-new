"""新人の同行は「勤務時間が実際に重なっていれば」成立する（同じ記号・同じ食事帯でなくてよい）。

ユーザー依頼（2026-08）:
  「新人の出勤時間とベテランの出勤時間が現実かぶっていれば、まったく同じでなくてもいい」

・新人 ④6:00-13:00 × ベテラン ⑤9:00-15:00（食事帯は違うが4時間重なる）→ 同行成立
・新人 ①6:00-8:00 × ベテラン ②8:00-13:00（接するだけで重なり0）→ 同行不成立
"""
import calendar
import datetime

from solver import _cook_overlap_map, _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]

TYPES = [
    {"code": "cooking_1", "label": "① 6:00-8:00", "start_time": "06:00", "end_time": "08:00"},
    {"code": "cooking_2", "label": "② 8:00-13:00", "start_time": "08:00", "end_time": "13:00"},
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_5", "label": "⑤ 9:00-15:00", "start_time": "09:00", "end_time": "15:00"},
]

STAFF = [
    {"id": 1, "name": "ベテラン", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "cooking_experience": "veteran"},
    {"id": 2, "name": "新人", "employment_type": "パート", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "cooking_experience": "new",
     "min_days_per_week": 3},
]
# ベテランは⑤9-15のみ、新人は④6-13のみ（食事帯は異なるが9:00-13:00が重なる）
ALLOWED = {1: {"cooking_5"}, 2: {"cooking_4"}}


def _run(staff, combos, allowed):
    settings = {
        "min_cooking_staff": 1,
        "cooking_types": TYPES,
        "cooking_combo_rules": [
            {"id": i + 1, "name": f"組{i + 1}", "allowed_patterns": c, "is_active": True}
            for i, c in enumerate(combos)
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "", "breakfast_off_end": "", "closed_dates": "",
    }
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    return _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, staff, [], settings,
        allowed_patterns=allowed, locked_assignments={},
    )


def test_overlap_map_ignores_touching_ranges():
    """接するだけ（6-8 と 8-13）は重なりとみなさない。"""
    ranges = {
        "cooking_1": (6 * 60, 8 * 60),
        "cooking_2": (8 * 60, 13 * 60),
        "cooking_4": (6 * 60, 13 * 60),
        "cooking_5": (9 * 60, 15 * 60),
    }
    m = _cook_overlap_map(list(ranges), ranges)
    assert "cooking_2" not in m["cooking_1"], "6-8 と 8-13 は重なっていない"
    assert "cooking_5" in m["cooking_4"], "6-13 と 9-15 は重なっている"
    assert "cooking_4" in m["cooking_5"]


def test_new_staff_pairs_with_overlapping_veteran():
    """食事帯が違っても、勤務時間が重なるベテランと組めば新人が出勤できる。"""
    shifts, _warnings = _run(STAFF, [["cooking_4", "cooking_5"]], ALLOWED)

    by_day = {}
    for s in shifts:
        by_day.setdefault(s["date"], []).append((s["staff_id"], s["assignment"]))
    new_days = [d for d, items in by_day.items() if any(sid == 2 for sid, _ in items)]
    assert new_days, "新人が1日も出勤できていない"
    for d in new_days:
        assigns = dict(by_day[d])
        assert assigns[2] == "cooking_4"
        assert assigns.get(1) == "cooking_5", f"{d}: 重なるベテランが居ない: {by_day[d]}"


def test_new_staff_not_alone_when_hours_do_not_overlap():
    """重ならない時間帯（新人①6-8 × ベテラン②8-13）では新人を入れない。"""
    staff = [dict(s) for s in STAFF]
    allowed = {1: {"cooking_2"}, 2: {"cooking_1"}}
    shifts, _warnings = _run(staff, [["cooking_1", "cooking_2"]], allowed)

    assert not [s for s in shifts if s["staff_id"] == 2], (
        "勤務時間が重ならないのに新人が配置された"
    )


def test_one_hour_overlap_is_not_enough():
    """1時間しか重ならない組み合わせは同行とみなさない。

    ユーザー指摘（2026-08）:「大平さんは新人なのに26日は佐藤さんと1時間しか
    重ならないので佐藤さんが指導できない」（②8:00-13:00 × ③12:00-19:00）。
    """
    ranges = {
        "cooking_2": (8 * 60, 13 * 60),
        "cooking_3": (12 * 60, 19 * 60),
        "cooking_4": (6 * 60, 13 * 60),
        "cooking_5": (9 * 60, 15 * 60),
    }
    m = _cook_overlap_map(list(ranges), ranges)
    assert "cooking_3" not in m["cooking_2"], "1時間の重なりは同行にしない"
    assert "cooking_5" in m["cooking_2"], "4時間重なるので同行に使える"
    assert "cooking_2" in m["cooking_2"], "同じ記号は常に同行"


def test_new_staff_not_scheduled_with_only_one_hour_overlap():
    """新人と1時間しか重ならないベテランしか居ない日は、新人を入れない。"""
    types = [
        {"code": "cooking_2", "label": "② 8:00-13:00", "start_time": "08:00", "end_time": "13:00"},
        {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    ]
    staff = [dict(s) for s in STAFF]
    settings_types = TYPES
    try:
        globals()['TYPES'] = types
        shifts, _ = _run(staff, [["cooking_2", "cooking_3"]],
                         {1: {"cooking_3"}, 2: {"cooking_2"}})
    finally:
        globals()['TYPES'] = settings_types

    assert not [s for s in shifts if s["staff_id"] == 2], (
        "重なりが1時間しかないのに新人が配置された"
    )
