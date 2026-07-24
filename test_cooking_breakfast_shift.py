"""朝食あり日に 9:00-16:00 ではなく 7:00-15:00 を使う（朝[6-8)不足を出さない）。

依頼: 「7:00-15:00 を追加して職員にも✓しているのに、朝食アリの期間なのに
9-16 単独で入って 6-8 エラーになる」。
9-16 は朝食なし用の時間帯なので、朝食あり日は 7:00-15:00 に置き換わること、
朝食なし期間では従来どおり 9-16 が使われることを確認する。
"""
import calendar
import datetime

from solver import _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8

COOKING_TYPES = [
    {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_5", "label": "⑤ 9:00-15:00", "start_time": "09:00", "end_time": "15:00"},
    {"code": "cooking_6", "label": "⑥ 9:00-16:00", "start_time": "09:00", "end_time": "16:00"},
    {"code": "cooking_7", "label": "⑦ 6:00-12:00", "start_time": "06:00", "end_time": "12:00"},
    {"code": "cooking_8", "label": "⑧ 13:00-19:00", "start_time": "13:00", "end_time": "19:00"},
    {"code": "cooking_9", "label": "⑨ 7:00-15:00", "start_time": "07:00", "end_time": "15:00"},
]

# 朝を賄えない編成（⑥単独・⑥+⑤）を含む組み合わせルール
COMBOS = [
    ["cooking_3", "cooking_7"],
    ["cooking_4", "cooking_8"],
    ["cooking_6"],
    ["cooking_6", "cooking_5"],
]

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
STAFF = [
    {"id": 1, "name": "A", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 5, "public_holiday_count": 9},
    {"id": 2, "name": "B", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 5, "public_holiday_count": 9},
    {"id": 3, "name": "C", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 6, "public_holiday_count": 8},
    {"id": 4, "name": "D", "employment_type": "パート", "available_days": ALL_DAYS,
     "max_days_per_week": 3, "max_consecutive_days": 3, "public_holiday_count": 20},
]
ALLOWED = {
    1: {"cooking_4", "cooking_6", "cooking_9"},
    2: {"cooking_3", "cooking_6", "cooking_9"},
    3: {"cooking_7", "cooking_8"},
    4: {"cooking_5"},
}

# 1人しか出勤できない日（＝1人編成しか組めない日）
THIN_DAY = "2026-08-04"
# 全員休みの日を1日入れてスラック付きフェーズ（不足を警告に降格するフェーズ）で解かせる。
# 実データで 6-8 不足警告が出ていたのはこのフェーズ。
EMPTY_DAY = "2026-08-09"


def _base_settings():
    return {
        "min_cooking_staff": 1,
        "cooking_types": COOKING_TYPES,
        "cooking_combo_rules": [
            {"id": i, "name": f"組{i}", "allowed_patterns": c, "is_active": True}
            for i, c in enumerate(COMBOS, 1)
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "",
        "breakfast_off_end": "",
        "closed_dates": "",
    }


def _run(settings):
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    day_off = [{"staff_id": s["id"], "date": THIN_DAY} for s in STAFF if s["id"] != 2]
    day_off += [{"staff_id": s["id"], "date": EMPTY_DAY} for s in STAFF]
    shifts, warnings = _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, STAFF, day_off, settings,
        allowed_patterns=ALLOWED, locked_assignments={},
    )
    return shifts, warnings


def test_breakfast_day_uses_7_15_instead_of_9_16():
    shifts, warnings = _run(_base_settings())

    thin = [s["assignment"] for s in shifts if s["date"] == THIN_DAY]
    assert thin == ["cooking_9"], f"朝食あり日は7:00-15:00で組むこと: {thin}"

    # 9:00-16:00 は朝食あり期間では使わない
    assert not [s for s in shifts if s["assignment"] == "cooking_6"]

    # 全員休みの日以外に朝[6-8)不足の警告を出さない
    morning_short = [
        w for w in warnings
        if w["warning_type"] == "understaffed_cook_interval_0" and w["date"] != EMPTY_DAY
    ]
    assert morning_short == [], morning_short


def test_no_breakfast_period_still_uses_9_16():
    settings = _base_settings()
    settings["breakfast_off_start"] = "2026-08-01"
    settings["breakfast_off_end"] = "2026-08-31"

    shifts, _ = _run(settings)

    thin = [s["assignment"] for s in shifts if s["date"] == THIN_DAY]
    assert thin == ["cooking_6"], f"朝食なし期間は9:00-16:00のまま: {thin}"
    assert not [s for s in shifts if s["assignment"] == "cooking_9"]
