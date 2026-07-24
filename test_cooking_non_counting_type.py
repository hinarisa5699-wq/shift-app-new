"""事務など「調理として数えない」シフト種類は調理の充足人数に入れない。

本番の ⑤9:00-15:00「事務」（江口さん）は調理シフト表には載るが調理はしない。
従来は時刻だけからカバレッジを作っていたため、事務1人しか居ない日でも
昼[8-12)・夜[12-19) が「足りている」と判定されていた。
"""
import calendar
import datetime

from solver import _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
# 事務(⑤)しか出勤できない日
JIMU_ONLY_DAY = "2026-08-04"

TYPES = [
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    {"code": "cooking_5", "label": "⑤ 9:00-15:00 事務", "start_time": "09:00", "end_time": "15:00"},
]
COMBOS = [["cooking_4", "cooking_3"], ["cooking_5"]]
STAFF = [
    {"id": 1, "name": "朝", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
    {"id": 2, "name": "夜", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
    {"id": 3, "name": "事務", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
]
ALLOWED = {1: {"cooking_4"}, 2: {"cooking_3"}, 3: {"cooking_5"}}


def _run(counts_as_cooking):
    types = [dict(t) for t in TYPES]
    for t in types:
        if t["code"] == "cooking_5":
            t["counts_as_cooking"] = counts_as_cooking
    settings = {
        "min_cooking_staff": 1,
        "cooking_types": types,
        "cooking_combo_rules": [
            {"id": i, "name": f"組{i}", "allowed_patterns": c, "is_active": True}
            for i, c in enumerate(COMBOS, 1)
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "", "breakfast_off_end": "", "closed_dates": "",
    }
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    day_off = [{"staff_id": sid, "date": JIMU_ONLY_DAY} for sid in (1, 2)]
    return _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, STAFF, day_off, settings,
        allowed_patterns=ALLOWED, locked_assignments={},
    )


def _lunch_dinner_warnings(warnings):
    return sorted(
        w["warning_type"] for w in warnings
        if w["date"] == JIMU_ONLY_DAY
        and w["warning_type"].startswith("understaffed_cook_interval_")
    )


def test_non_counting_type_does_not_satisfy_coverage():
    _shifts, warnings = _run(counts_as_cooking=False)
    assert _lunch_dinner_warnings(warnings) == [
        "understaffed_cook_interval_0",
        "understaffed_cook_interval_1",
        "understaffed_cook_interval_2",
    ], warnings


def test_counting_type_still_satisfies_coverage():
    """既定（数える）では従来どおり昼・夜を賄う扱い。"""
    _shifts, warnings = _run(counts_as_cooking=True)
    assert _lunch_dinner_warnings(warnings) == [
        "understaffed_cook_interval_0",  # 朝[6-8)は9:00開始では賄えない（従来どおり）
    ], warnings
