"""
test_req42_43.py — 依頼文42（連続同種シフト回避）/ 依頼文43（早番・遅番・オンコール均等化）

pytest 不要。bundle 済み venv でも実行可:
    venv\\Scripts\\python.exe test_req42_43.py
"""
import datetime

from solver import generate_shift, assign_oncall


def _staff(staff_id, can_visit=True):
    return {
        "id": staff_id, "name": f"s{staff_id}", "employment_type": "常勤",
        "can_visit": can_visit, "max_consecutive_days": 7, "max_days_per_week": 7,
        "min_days_per_week": 0, "available_days": [0, 1, 2, 3, 4, 5, 6],
        "available_time_slots": "full_day", "fixed_days_off": [], "staff_group": "care",
        "gender": "female", "has_phone_duty": False,
        "qualification_ids": [], "qualification_names": [], "qualification_codes": ["care_worker"],
        "weekend_constraint": "", "holiday_ng": False,
    }


def _base_settings(**over):
    s = {
        "min_day_service": 3, "min_visit_am": 1, "min_visit_pm": 1,
        "closed_days": [], "visit_operating_days": [0, 1, 3, 4],
        "min_cooking_staff": 0, "min_cooking_overlap": 0, "placement_rules": [],
        "cooking_combo_rules": [], "min_staff_at_9": 3, "min_staff_at_15": 3,
        "max_day_service": 0, "min_early_staff": 1, "min_late_staff": 1,
    }
    s.update(over)
    return s


def _by_staff_dates(shifts):
    """{staff_id: {date: assignment}} を返す。"""
    out = {}
    for sh in shifts:
        out.setdefault(sh["staff_id"], {})[sh["date"]] = sh["assignment"]
    return out


def _consec_same(shifts, code):
    """連日（カレンダー連日かつ両日とも code）の件数を機械カウント。"""
    bs = _by_staff_dates(shifts)
    n = 0
    for sid, dmap in bs.items():
        days = sorted(dmap)
        for i in range(len(days) - 1):
            d0 = datetime.date.fromisoformat(days[i])
            d1 = datetime.date.fromisoformat(days[i + 1])
            if (d1 - d0).days == 1 and dmap[days[i]] == code and dmap[days[i + 1]] == code:
                n += 1
    return n


def test_req42_early_consecutive_hard_zero():
    """依頼文42: early_consecutive_mode='hard' で早番→早番の連日が0件。"""
    care = [_staff(i) for i in range(1, 9)]
    shifts, _ = generate_shift(
        2026, 7, care, [], [], _base_settings(early_consecutive_mode="hard"),
    )
    assert shifts is not None, "生成に失敗"
    n = _consec_same(shifts, "early")
    assert n == 0, f"hard なのに早番連日が {n} 件残った"
    print("OK test_req42_early_consecutive_hard_zero")


def test_req42_late_consecutive_hard_zero():
    """依頼文42: late_consecutive_mode='hard' で遅番→遅番の連日が0件（既存・回帰）。"""
    care = [_staff(i) for i in range(1, 9)]
    shifts, _ = generate_shift(
        2026, 7, care, [], [], _base_settings(late_consecutive_mode="hard"),
    )
    assert shifts is not None
    n = _consec_same(shifts, "late")
    assert n == 0, f"hard なのに遅番連日が {n} 件残った"
    print("OK test_req42_late_consecutive_hard_zero")


def test_req43_early_fairness_hard_cap():
    """依頼文43: early_fairness_mode='hard', 上限1 で早番回数の spread<=1。"""
    care = [_staff(i) for i in range(1, 9)]
    shifts, _ = generate_shift(
        2026, 7, care, [], [],
        _base_settings(early_fairness_mode="hard", early_fairness_max=1),
    )
    assert shifts is not None, "生成に失敗（hard上限が厳しすぎる可能性）"
    bs = _by_staff_dates(shifts)
    counts = []
    for sid, dmap in bs.items():
        counts.append(sum(1 for a in dmap.values() if a == "early"))
    spread = max(counts) - min(counts) if counts else 0
    assert spread <= 1, f"early_fairness hard 上限1 なのに spread={spread}"
    print(f"OK test_req43_early_fairness_hard_cap (spread={spread})")


def test_req43_oncall_fairness_modes():
    """依頼文43: assign_oncall の off/soft/hard と spread 上限・スポット除外。"""
    dates = [datetime.date(2026, 7, 1) + datetime.timedelta(days=i) for i in range(20)]
    elig = [{"id": i, "name": f"s{i}", "unavailable": set()} for i in range(1, 6)]
    for mode in ("off", "soft", "hard"):
        asn, _ = assign_oncall(elig, dates, max_consecutive=1, fairness_mode=mode, fairness_max=1)
        from collections import Counter
        c = Counter(a["staff_id"] for a in asn)
        counts = [c.get(i, 0) for i in range(1, 6)]
        spread = max(counts) - min(counts)
        assert len(asn) == len(dates), f"{mode}: 未割当が発生 {len(asn)}/{len(dates)}"
        if mode in ("soft", "hard"):
            assert spread <= 1, f"{mode}: spread={spread}"
    # スポット職員(1日のみ可)を入れても主要5名は均等・全日割当
    spot = {"id": 6, "name": "spot", "unavailable": {d.isoformat() for d in dates[1:]}}
    asn, _ = assign_oncall(elig + [spot], dates, max_consecutive=1, fairness_mode="hard", fairness_max=1)
    assert len(asn) == len(dates), "スポット混在で未割当が発生"
    print("OK test_req43_oncall_fairness_modes")


if __name__ == "__main__":
    test_req42_early_consecutive_hard_zero()
    test_req42_late_consecutive_hard_zero()
    test_req43_early_fairness_hard_cap()
    test_req43_oncall_fairness_modes()
    print("ALL PASSED")
