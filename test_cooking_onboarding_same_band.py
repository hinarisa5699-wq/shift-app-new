"""新人の同行相手は「同じ食事帯」の別記号でもよい（⑦6:00-12:00 と ④6-13）。

宇佐美さん(新人)は ④6-13 が持ちシフトだが、土日の編成は「⑦6-12 ＋ ③12-19」で
④ を含まないため、組み合わせの完全一致制約で ④ が0人に固定され、土日に一度も
出勤できなかった。⑦ と ④ は同じ食事帯(朝1/昼1/夜0)なので、新人の同行が成立する
日に限り、編成に無い同帯の記号を1名だけ足せるようにする。
"""
import calendar
import datetime

from solver import _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]

TYPES = [
    {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_7", "label": "⑦ 6:00-12:00", "start_time": "06:00", "end_time": "12:00"},
]
COMBOS = [["cooking_7", "cooking_3"]]  # ④を含まない編成だけ

STAFF = [
    {"id": 1, "name": "夜ベテラン", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "cooking_experience": "veteran"},
    {"id": 2, "name": "朝ベテラン", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "cooking_experience": "veteran"},
    {"id": 3, "name": "新人", "employment_type": "パート", "available_days": ALL_DAYS,
     "max_days_per_week": 7, "max_consecutive_days": 31, "cooking_experience": "new",
     "min_days_per_week": 3},
]
ALLOWED = {1: {"cooking_3"}, 2: {"cooking_7"}, 3: {"cooking_4"}}


def _run(staff):
    settings = {
        "min_cooking_staff": 1,
        "cooking_types": TYPES,
        "cooking_combo_rules": [
            {"id": 1, "name": "組1", "allowed_patterns": COMBOS[0], "is_active": True}
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "", "breakfast_off_end": "", "closed_dates": "",
    }
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    return _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, staff, [], settings,
        allowed_patterns=ALLOWED, locked_assignments={},
    )


def test_new_staff_can_join_with_same_band_symbol():
    """④しか持たない新人が、⑦のベテランと組んで出勤できる。"""
    shifts, _warnings = _run(STAFF)

    new_days = [s["date"] for s in shifts if s["staff_id"] == 3]
    assert new_days, "新人が1日も出勤できていない"

    by_day = {}
    for s in shifts:
        by_day.setdefault(s["date"], []).append((s["staff_id"], s["assignment"]))
    for d in new_days:
        assigns = dict(by_day[d])
        assert assigns[3] == "cooking_4", (d, by_day[d])
        # 同じ食事帯(朝昼)のベテランが必ず一緒に居る
        assert assigns.get(2) == "cooking_7", f"{d}: 同帯のベテランが居ない: {by_day[d]}"


def test_exact_match_kept_when_no_new_staff():
    """新人が居なければ編成＝パターンの完全一致のまま（④は出てこない）。"""
    veterans = [s for s in STAFF if s["cooking_experience"] == "veteran"]
    shifts, _warnings = _run(veterans)

    assert not [s for s in shifts if s["assignment"] == "cooking_4"], (
        "新人が居ないのに編成外の記号が入った"
    )
