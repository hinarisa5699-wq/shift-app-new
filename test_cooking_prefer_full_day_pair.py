"""6:00-19:00 を2人で賄う編成を優先し、1人勤務(6:30-14:30)は最後の手段にする。

実データ（本番8月）では 6:30-14:30 が2人で入り、夜19時まで誰も居ない日が
8日出ていた。原因は「⑥9-16 を 6:30-14:30 に置き換えた1人編成」が通常編成と
同じ0ペナルティで置かれ、朝夜2人の編成より安くなっていたこと。
土日は介護が居ないため、夜17:30の配膳下膳（③12-19）を必ず立てる。
"""
import calendar
import datetime

from solver import _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]

TYPES = [
    {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_6", "label": "⑥ 9:00-16:00", "start_time": "09:00", "end_time": "16:00"},
    {"code": "cooking_9", "label": "(9) 6:30-14:30", "start_time": "06:30", "end_time": "14:30"},
]
# 本番と同様、⑥単独の「朝も夜も賄えない編成」が登録されている状態
COMBOS = [["cooking_4", "cooking_3"], ["cooking_6"]]

STAFF = [
    {"id": 1, "name": "朝", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
    {"id": 2, "name": "夜", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
]
ALLOWED = {
    1: {"cooking_4", "cooking_6", "cooking_9"},
    2: {"cooking_3", "cooking_6", "cooking_9"},
}
EMPTY_DAY = "2026-08-09"  # 全員休み＝スラック付きフェーズで解かせるため


def _run():
    settings = {
        "min_cooking_staff": 1,
        "cooking_types": TYPES,
        "cooking_combo_rules": [
            {"id": i, "name": f"組{i}", "allowed_patterns": c, "is_active": True}
            for i, c in enumerate(COMBOS, 1)
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "", "breakfast_off_end": "", "closed_dates": "",
    }
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    day_off = [{"staff_id": s["id"], "date": EMPTY_DAY} for s in STAFF]
    return _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, STAFF, day_off, settings,
        allowed_patterns=ALLOWED, locked_assignments={},
    )


def test_two_person_six_to_nineteen_is_preferred():
    """2人揃う日は 6:00-19:00 を賄う ④+③ にする（1人勤務へ逃げない）。"""
    shifts, _warnings = _run()

    by_day = {}
    for s in shifts:
        by_day.setdefault(s["date"], []).append(s["assignment"])

    single = {d: a for d, a in by_day.items() if "cooking_9" in a or a == ["cooking_6"]}
    assert single == {}, f"2人揃う日に1人勤務へ逃げている: {single}"
    for d, assigns in by_day.items():
        assert sorted(assigns) == ["cooking_3", "cooking_4"], (d, assigns)


def test_saturday_and_sunday_have_dinner_shift():
    """土日は夜17:30に居る記号(③12-19)を必ず立てる。"""
    shifts, warnings = _run()

    by_day = {}
    for s in shifts:
        by_day.setdefault(s["date"], []).append(s["assignment"])

    for d, assigns in by_day.items():
        if datetime.date.fromisoformat(d).weekday() not in (5, 6):
            continue
        assert "cooking_3" in assigns, f"{d}(土日)に夜担当が居ない: {assigns}"
    # 全員休みの日だけは担い手が居ないので警告になる（それ以外では出ないこと）
    unmet = [
        w["date"] for w in warnings
        if w["warning_type"] == "cook_sunday_dinner_unassigned"
    ]
    assert unmet == [EMPTY_DAY], unmet
