import datetime
from typing import List, Optional

from solver import (
    _BREAK_SLOTS,
    _assign_break_times,
    _repair_breaks_for_onsite_staffing,
    _solve_care,
    _solve_care_with_fallback,
    _validate_onsite_staffing,
    VISIT_ASSIGNMENTS,
)


def _build_staff(
    staff_id: int,
    *,
    can_visit: bool,
    qualifications: Optional[List[int]] = None,
    available_time_slots: str = "full_day",
) -> dict:
    return {
        "id": staff_id,
        "name": f"s{staff_id}",
        "employment_type": "常勤",
        "can_visit": can_visit,
        "max_consecutive_days": 7,
        "max_days_per_week": 7,
        "min_days_per_week": 0,
        "available_days": [0, 1, 2, 3, 4, 5, 6],
        "available_time_slots": available_time_slots,
        "fixed_days_off": [],
        "staff_group": "care",
        "gender": "female",
        "has_phone_duty": False,
        "qualification_ids": qualifications or [],
        "weekend_constraint": "",
        "holiday_ng": False,
    }


def test_break_times_are_staggered_for_four_full_day_staff():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_pattern1"},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "day_pattern1"},
        {"date": dt.isoformat(), "staff_id": 3, "assignment": "day_pattern1"},
        {"date": dt.isoformat(), "staff_id": 4, "assignment": "day_pattern1"},
    ]

    out = _assign_break_times(shifts, [dt])
    starts = [i.get("break_start") for i in out if i.get("break_start")]

    assert len(starts) == 4
    assert len(set(starts)) == 4
    assert set(starts).issubset(set(_BREAK_SLOTS))


def test_dual_assignments_have_fixed_break_1230():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_p3_visit_pm"},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "visit_am_day_p4"},
    ]
    out = _assign_break_times(shifts, [dt])
    starts = sorted(i.get("break_start") for i in out)
    assert starts == ["12:30", "12:30"]


def test_fixed_break_by_staff_is_applied():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 10, "assignment": "day_pattern1"},
        {"date": dt.isoformat(), "staff_id": 11, "assignment": "day_pattern1"},
    ]
    out = _assign_break_times(shifts, [dt], fixed_break_by_staff={10: "11:00"})
    break_by_staff = {i["staff_id"]: i.get("break_start") for i in out}
    assert break_by_staff[10] == "11:00"
    assert break_by_staff[11] != "11:00"


def test_break_repair_avoids_late_afternoon_understaffing():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_pattern1", "break_start": "10:00", "counselor_desk_slots": [3]},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "day_pattern1", "break_start": "12:00"},
        {"date": dt.isoformat(), "staff_id": 3, "assignment": "day_pattern1", "break_start": "14:00"},
        {"date": dt.isoformat(), "staff_id": 4, "assignment": "day_pattern1", "break_start": "15:00"},
        {"date": dt.isoformat(), "staff_id": 5, "assignment": "day_pattern1", "break_start": "16:00"},
    ]

    repaired = _repair_breaks_for_onsite_staffing(
        shifts,
        [dt],
        min_required=4,
        nurse_pt_staff_ids=set(),
    )

    break_by_staff = {item["staff_id"]: item.get("break_start") for item in repaired}
    assert break_by_staff[5] != "16:00"
    assert not _validate_onsite_staffing(repaired, [dt], 4, set())


def test_break_repair_can_shift_another_staff_to_free_earlier_slot():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_pattern1", "break_start": "13:00", "counselor_desk_slots": [3]},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "day_pattern1", "break_start": "12:00", "counselor_desk_slots": [2]},
        {"date": dt.isoformat(), "staff_id": 3, "assignment": "day_pattern1", "break_start": "14:00"},
        {"date": dt.isoformat(), "staff_id": 4, "assignment": "day_pattern2", "break_start": "10:00"},
        {"date": dt.isoformat(), "staff_id": 5, "assignment": "day_pattern1", "break_start": "16:00", "counselor_desk_slots": [1]},
        {"date": dt.isoformat(), "staff_id": 6, "assignment": "day_pattern1", "break_start": "15:00"},
    ]

    repaired = _repair_breaks_for_onsite_staffing(
        shifts,
        [dt],
        min_required=4,
        nurse_pt_staff_ids=set(),
    )

    break_by_staff = {item["staff_id"]: item.get("break_start") for item in repaired}
    assert break_by_staff[5] == "10:00"
    assert break_by_staff[4] == "11:00"
    assert not _validate_onsite_staffing(repaired, [dt], 4, set())


def test_fallback_reports_understaffed_at_13_when_only_half_day_cover_exists():
    dt = datetime.date(2026, 6, 1)
    staff = [
        _build_staff(1, can_visit=False),
        _build_staff(2, can_visit=False),
        _build_staff(3, can_visit=False),
        _build_staff(4, can_visit=False),
        _build_staff(5, can_visit=False, available_time_slots="am_only"),
        _build_staff(6, can_visit=False, available_time_slots="am_only"),
        _build_staff(7, can_visit=False, available_time_slots="pm_only"),
        _build_staff(8, can_visit=False, available_time_slots="pm_only"),
    ]
    settings = {
        "min_day_service": 4,
        "max_day_service": 0,
        "min_visit_am": 0,
        "min_visit_pm": 0,
        "min_dual_assignment": 0,
        "closed_days": [],
        "visit_operating_days": [0, 1, 2, 3, 4, 5, 6],
        "am_preferred_gender": "",
        "phone_duty_enabled": False,
        "phone_duty_max_consecutive": 1,
        "min_staff_at_9": 4,
        "min_staff_at_15": 4,
        "male_am_constraint_mode": "off",
        "placement_rules": [],
        "counselor_desk_enabled": False,
        "counselor_desk_count": 1,
    }

    shifts, warnings = _solve_care_with_fallback(
        2026,
        6,
        [dt],
        staff,
        [],
        settings,
        allowed_patterns={},
    )

    assert shifts is not None
    warning_types = {w["warning_type"] for w in warnings}
    assert "understaffed_at_13" in warning_types


def test_allowed_day_patterns_do_not_block_visit_assignments():
    dt = datetime.date(2026, 6, 1)
    staff_ids = [1, 2]
    staff_by_id = {
        1: _build_staff(1, can_visit=True),
        2: _build_staff(2, can_visit=False),
    }
    shifts, _warnings = _solve_care(
        2026,
        6,
        [dt],
        staff_ids,
        staff_by_id,
        off_request_set=set(),
        # min_day_service=0: 本テストの主旨は「デイ限定の許可パターンが訪問兼務を
        # 塞がないこと」の検証。min_day_service>=1 にすると 13:00 在席要件
        # (min_staff_at_13=min_day_service) 等が 2名構成では満たせず、許可パターンと
        # 無関係に infeasible になり主旨を覆い隠すため 0 とする（依頼文30で是正）。
        min_day_service=0,
        min_visit_am=0,
        min_visit_pm=1,
        closed_days_set=set(),
        visit_operating_days=[0, 1, 2, 3, 4, 5, 6],
        am_preferred_gender="",
        phone_duty_enabled=False,
        phone_duty_max_consecutive=1,
        min_staff_at_9=0,
        min_staff_at_15=0,
        male_am_constraint_mode="off",
        placement_rules=[],
        allowed_patterns={1: {"day_pattern1"}},
        max_day_service=1,
        use_slack=False,
    )

    assert shifts is not None
    s1_assignments = [i["assignment"] for i in shifts if i["staff_id"] == 1]
    assert any(a in VISIT_ASSIGNMENTS for a in s1_assignments), "デイ許可パターンだけで訪問/兼務が封じられてはいけない"


def test_fallback_relaxes_hard_placement_rules_before_no_solution():
    dt = datetime.date(2026, 6, 1)
    staff = [
        _build_staff(1, can_visit=False, qualifications=[1]),
        _build_staff(2, can_visit=False),
    ]
    settings = {
        "min_day_service": 1,
        "max_day_service": 0,
        "min_visit_am": 0,
        "min_visit_pm": 0,
        "min_dual_assignment": 0,
        "closed_days": [],
        "visit_operating_days": [0, 1, 2, 3, 4, 5, 6],
        "am_preferred_gender": "",
        "phone_duty_enabled": False,
        "phone_duty_max_consecutive": 1,
        "min_staff_at_9": 0,
        "min_staff_at_15": 0,
        "male_am_constraint_mode": "off",
        "placement_rules": [
            {
                "name": "看護師/PT 9-16時 1名以上",
                "rule_type": "qualification_min",
                "target_qualification_ids": [1],
                "period": "all",
                    "min_count": 3,
                "is_hard": True,
                "is_active": True,
                "penalty_weight": 100,
            }
        ],
        "counselor_desk_enabled": False,
        "counselor_desk_count": 1,
    }

    shifts, warnings = _solve_care_with_fallback(
        2026,
        6,
        [dt],
        staff,
        [],
        settings,
        allowed_patterns={},
    )

    assert shifts is not None, "hard配置ルールが不可能でも no_solution 全落ちを避ける"
    warning_types = {w["warning_type"] for w in warnings}
    assert "placement_rules_relaxed" in warning_types
    assert "no_solution" not in warning_types


def _solve_no_day_service_day(num_staff: int, **overrides):
    """デイ利用者がいない曜日（no_service_day_indices={0}）を1日だけ解く。"""
    dt = datetime.date(2026, 6, 1)
    staff_ids = list(range(1, num_staff + 1))
    staff_by_id = {sid: _build_staff(sid, can_visit=False) for sid in staff_ids}
    kwargs = dict(
        off_request_set=set(),
        min_day_service=5,
        min_visit_am=0,
        min_visit_pm=0,
        closed_days_set=set(),
        visit_operating_days=[],       # 訪問非営業日＝早番/デイは終日デイ扱い
        am_preferred_gender="",
        phone_duty_enabled=False,
        male_am_constraint_mode="off",
        placement_rules=[],
        min_staff_at_9=5,
        min_staff_at_15=5,
        max_day_service=5,
        no_service_day_indices={0},
        require_early_late=False,
        use_slack=False,
    )
    kwargs.update(overrides)
    return _solve_care(2026, 6, [dt], staff_ids, staff_by_id, **kwargs)


def test_no_day_service_day_staffs_exactly_two():
    """デイ以外の曜日は介護がちょうど2名（余剰人員は入れない）。"""
    shifts, _ = _solve_no_day_service_day(6)
    assert shifts is not None
    working = {i["staff_id"] for i in shifts if i["assignment"] != "off"}
    assert len(working) == 2, f"デイ以外の曜日は介護2名のはず: {sorted(working)}"


def test_no_day_service_day_target_capped_by_available_staff():
    """在籍が原則人数に満たなくても無解にしない（いる人数で解く）。"""
    shifts, _ = _solve_no_day_service_day(1)
    assert shifts is not None
    working = {i["staff_id"] for i in shifts if i["assignment"] != "off"}
    assert len(working) == 1


def test_no_day_service_min_staff_is_configurable():
    """no_service_min_staff で人数を変更できる（1名に絞る）。"""
    shifts, _ = _solve_no_day_service_day(6, no_service_min_staff=1)
    assert shifts is not None
    working = {i["staff_id"] for i in shifts if i["assignment"] != "off"}
    assert len(working) == 1


def test_no_day_service_day_over_staff_warns_with_slack():
    """必須配置(早番/遅番＋訪問)が2名に収まらない日は超過を警告して生成継続。"""
    dt = datetime.date(2026, 6, 1)
    staff_ids = [1, 2, 3, 4]
    staff_by_id = {sid: _build_staff(sid, can_visit=True) for sid in staff_ids}
    shifts, warnings = _solve_care(
        2026, 6, [dt], staff_ids, staff_by_id,
        off_request_set=set(),
        min_day_service=1,
        min_visit_am=1,
        min_visit_pm=1,
        closed_days_set=set(),
        visit_operating_days=[0, 1, 2, 3, 4, 5, 6],   # 訪問営業日かつデイ非営業日
        am_preferred_gender="",
        phone_duty_enabled=False,
        male_am_constraint_mode="off",
        placement_rules=[],
        min_staff_at_9=1,
        min_staff_at_15=1,
        max_day_service=4,
        no_service_day_indices={0},
        require_early_late=True,
        use_slack=True,
    )
    assert shifts is not None
    assert any(w["warning_type"] == "over_staffed_no_day_service" for w in warnings)
