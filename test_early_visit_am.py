"""早番(7:30-16:30)は訪問に自動で出ない（ユーザー依頼 2026-08 のルール変更）。

以前は「訪問営業日の早番＝午前訪問」として数えていたが、実態と合わないため
早番は終日デイ扱いにし、訪問は訪問の枠（visit_am / 兼務B）で割り当てる。
"""
import datetime

from solver import generate_shift

YEAR, MONTH = 2026, 8
VISIT_DAYS = [0, 4]  # 月・金（本番と同じ）


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
        "min_day_service": 1, "min_visit_am": 1, "min_visit_pm": 1,
        "closed_days": [], "visit_operating_days": VISIT_DAYS,
        "min_cooking_staff": 0, "min_cooking_overlap": 0, "placement_rules": [],
        "cooking_combo_rules": [], "min_staff_at_9": 1, "min_staff_at_15": 1,
        "max_day_service": 5, "min_early_staff": 1, "min_late_staff": 1,
    }


def test_early_is_not_counted_as_am_visit():
    """訪問午前は訪問の枠で割り当てる。早番は訪問に数えない。"""
    care = [_staff(i, True) for i in range(1, 4)] + [_staff(i, False) for i in range(4, 10)]
    shifts, warnings = generate_shift(YEAR, MONTH, care, [], [], _settings())

    unmet = [
        w for w in warnings
        if w["warning_type"] in ("understaffed_visit_am", "early_unassigned")
    ]
    assert unmet == [], unmet

    by_date = {}
    for s in shifts:
        by_date.setdefault(s["date"], []).append(s["assignment"])
    checked = 0
    for d, assigns in by_date.items():
        if datetime.date.fromisoformat(d).weekday() not in VISIT_DAYS:
            continue
        checked += 1
        am = assigns.count("visit_am") + assigns.count("visit_am_day_p4")
        assert am == 1, f"{d}: 訪問午前は訪問の枠でちょうど1名のはず (={am})"
    assert checked > 0, "訪問営業日が検査されていない"
