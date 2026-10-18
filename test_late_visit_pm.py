"""遅番＋午後訪問（ユーザー依頼 2026-10:「遅番の訪問可能な人は遅番でも訪問いれる」）。

訪問可の職員が訪問営業日に遅番(9:30-18:30)のとき、午前デイ→13:30-17:30 午後訪問→
戻って18:30まで、を組めるようにした。出力は assignment="late" のまま visit_slot="pm"。
ほかで訪問を埋められる日は使わない。
"""
import datetime

from export import recompute_warnings_from_shifts
from solver import generate_shift

YEAR, MONTH = 2026, 8
VISIT_DAYS = [0, 4]  # 月・金


def _staff(staff_id, can_visit):
    return {
        "id": staff_id, "name": f"s{staff_id}", "employment_type": "常勤",
        "can_visit": can_visit, "max_consecutive_days": 5, "max_days_per_week": 5,
        "min_days_per_week": 0, "available_days": [0, 1, 2, 3, 4, 5, 6],
        "available_time_slots": "full_day", "fixed_days_off": [], "staff_group": "care",
        "gender": "female", "has_phone_duty": False,
        "qualification_ids": [], "qualification_names": [],
        "qualification_codes": ["care_worker"],
        "weekend_constraint": "", "holiday_ng": False,
    }


def _settings():
    return {
        "min_day_service": 1, "min_visit_am": 0, "min_visit_pm": 1,
        "closed_days": [], "visit_operating_days": VISIT_DAYS,
        "min_cooking_staff": 0, "min_cooking_overlap": 0, "placement_rules": [],
        "cooking_combo_rules": [], "min_staff_at_9": 1, "min_staff_at_15": 1,
        "max_day_service": 5, "min_early_staff": 1, "min_late_staff": 1,
    }


def _is_visit_day(iso):
    return datetime.date.fromisoformat(iso).weekday() in VISIT_DAYS


def test_late_staff_who_can_visit_takes_pm_visit():
    """訪問できるのが遅番の人だけなら、遅番のまま午後訪問に出る。"""
    care = [_staff(1, True)] + [_staff(i, False) for i in range(2, 9)]
    # s1 は遅番しか入れない（訪問コードは単独の午後訪問だけ＝兼務も不可）
    allowed = {1: {"late", "visit_pm"}}
    shifts, warnings = generate_shift(YEAR, MONTH, care, [], [], _settings(),
                                      allowed_patterns=allowed)

    tagged = [s for s in shifts if s.get("visit_slot") == "pm"]
    assert tagged, "遅番＋午後訪問が一度も使われていない"
    for s in tagged:
        assert s["staff_id"] == 1 and s["assignment"] == "late", s
        assert _is_visit_day(s["date"]), f"訪問営業日以外に訪問: {s}"

    # 遅番＋午後訪問の日は訪問午後の不足が出ない
    tagged_dates = {s["date"] for s in tagged}
    short = [w for w in warnings
             if w["warning_type"] == "understaffed_visit_pm" and w["date"] in tagged_dates]
    assert short == [], short

    # 手直し後の警告再計算でも訪問午後に数える
    re_warn = recompute_warnings_from_shifts(
        shifts, care, _settings() | {"min_visit_am": 0}, YEAR, MONTH)
    re_short = [w for w in re_warn
                if w["warning_type"] == "understaffed_visit_pm" and w["date"] in tagged_dates]
    assert re_short == [], re_short


def test_late_staff_who_cannot_visit_never_gets_visit():
    """訪問不可の人は遅番でも訪問に出ない。"""
    care = [_staff(i, False) for i in range(1, 9)]
    shifts, _ = generate_shift(YEAR, MONTH, care, [], [], _settings())
    assert not [s for s in shifts if s.get("visit_slot")]


def test_not_used_when_visit_can_be_filled_otherwise():
    """訪問可の職員が十分いれば、遅番の人を午後訪問に出さない。"""
    care = [_staff(i, True) for i in range(1, 9)]
    shifts, warnings = generate_shift(YEAR, MONTH, care, [], [], _settings())
    assert not [w for w in warnings if w["warning_type"] == "understaffed_visit_pm"]
    assert not [s for s in shifts if s.get("visit_slot") == "pm"]


# ---------------------------------------------------------------------------
# 終日訪問（ユーザー依頼 2026-10:「午前午後で訪問分けられない時使う」）
# ---------------------------------------------------------------------------
def _settings_am_pm():
    return _settings() | {"min_visit_am": 1, "min_visit_pm": 1}


def test_full_day_visit_when_only_one_visitor():
    """訪問できる人が1人だけなら、その人が終日訪問で午前・午後とも入る。"""
    care = [_staff(1, True)] + [_staff(i, False) for i in range(2, 9)]
    shifts, warnings = generate_shift(YEAR, MONTH, care, [], [], _settings_am_pm())

    full = [s for s in shifts if s["assignment"] == "visit_full"]
    assert full, "終日訪問が一度も使われていない"
    for s in full:
        assert s["staff_id"] == 1 and _is_visit_day(s["date"]), s
        assert s.get("break_start") == "12:30", s
    full_dates = {s["date"] for s in full}
    short = [w for w in warnings
             if w["warning_type"] in ("understaffed_visit_am", "understaffed_visit_pm")
             and w["date"] in full_dates]
    assert short == [], short
    re_warn = recompute_warnings_from_shifts(shifts, care, _settings_am_pm(), YEAR, MONTH)
    assert not [w for w in re_warn
                if w["warning_type"] in ("understaffed_visit_am", "understaffed_visit_pm")
                and w["date"] in full_dates]


def test_full_day_visit_not_used_when_split_possible():
    """午前・午後を別の人に分けられるなら終日訪問は使わない。"""
    care = [_staff(i, True) for i in range(1, 9)]
    shifts, warnings = generate_shift(YEAR, MONTH, care, [], [], _settings_am_pm())
    assert not [s for s in shifts if s["assignment"] == "visit_full"]
    assert not [w for w in warnings
                if w["warning_type"] in ("understaffed_visit_am", "understaffed_visit_pm")]


def test_full_day_visit_never_for_non_visitor():
    care = [_staff(i, False) for i in range(1, 9)]
    shifts, _ = generate_shift(YEAR, MONTH, care, [], [], _settings_am_pm())
    assert not [s for s in shifts if s["assignment"] == "visit_full"]
