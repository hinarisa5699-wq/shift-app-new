"""訪問営業日の早番(7:30-16:30)は「訪問介護午前」の1名として数える。

早番は訪問営業日は「AM訪問＋PMデイ」だが、従来はその午前をデイ午前にも訪問午前にも
数えていなかったため、訪問可の職員が同じ日に「早番」と「兼務B(visit_am_day_p4)」で
2名必要になっていた。本番8月シフトでは訪問可の人数が足りず、
「月曜=訪問介護午前 1名不足」「金曜=早番 未配置」が交互に出ていた。
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


def test_early_fills_visit_am_on_visit_days():
    """訪問可が2名しか居なくても、訪問午前と早番の両方が埋まる。

    早番が訪問午前を兼ねるので、訪問営業日に必要な訪問可の人数は
    「早番1＋PM訪問兼務1」の2名で足りる（従来は兼務Bの分も含めて3名必要だった）。
    """
    care = [_staff(1, True), _staff(2, True)] + [_staff(i, False) for i in range(3, 9)]
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
        am = assigns.count("visit_am_day_p4") + assigns.count("early")
        assert am == 1, f"{d}: 訪問午前は早番か兼務Bで合計1名のはず (={am})"
    assert checked > 0, "訪問営業日が検査されていない"
