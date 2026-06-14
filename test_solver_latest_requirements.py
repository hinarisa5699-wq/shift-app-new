import datetime
from typing import List, Optional

from solver import (
    _BREAK_SLOTS,
    _assign_break_times,
    _assign_counselor_rotation,
    _break_overlaps_slot,
    _repair_breaks_for_onsite_staffing,
    _solve_care,
    _solve_care_with_fallback,
    _validate_onsite_staffing,
    PRESENT_FULL_DAY,
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


def test_counselor_enabled_does_not_force_day5():
    dt = datetime.date(2026, 6, 1)
    staff = [_build_staff(i, can_visit=False) for i in range(1, 5)]
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
        "counselor_desk_enabled": True,
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

    assert shifts, "相談員ONでデイ必要人数が+1されるとこのケースは解けなくなる"
    assert not any(w["warning_type"] == "no_solution" for w in warnings)


def test_counselor_enabled_prefers_two_counselors_when_available():
    dt = datetime.date(2026, 6, 1)
    staff = [
        _build_staff(1, can_visit=False, qualifications=[1]),
        _build_staff(2, can_visit=False, qualifications=[1]),
        _build_staff(3, can_visit=False),
        _build_staff(4, can_visit=False),
        _build_staff(5, can_visit=False),
        _build_staff(6, can_visit=False),
        _build_staff(7, can_visit=False),
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
        "placement_rules": [
            {
                "name": "相談員 午前1名以上",
                "rule_type": "qualification_min",
                "target_qualification_ids": [1],
                "period": "am",
                "min_count": 1,
                "is_hard": True,
                "is_active": True,
                "penalty_weight": 100,
            },
            {
                "name": "相談員 午後1名以上",
                "rule_type": "qualification_min",
                "target_qualification_ids": [1],
                "period": "pm",
                "min_count": 1,
                "is_hard": True,
                "is_active": True,
                "penalty_weight": 100,
            },
        ],
        "counselor_desk_enabled": True,
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
    counselor_ids = {1, 2}
    working_counselors = {
        item["staff_id"]
        for item in shifts
        if item["staff_id"] in counselor_ids and item["assignment"] != "off"
    }
    assert working_counselors == counselor_ids
    assert "understaffed_counselor_staff" not in {w["warning_type"] for w in warnings}


def test_counselor_enabled_prefers_two_full_day_counselors_when_available():
    dt = datetime.date(2026, 2, 2)
    staff = [
        _build_staff(1, can_visit=True, qualifications=[1]),
        _build_staff(2, can_visit=True, qualifications=[1]),
        _build_staff(3, can_visit=True, qualifications=[1]),
        _build_staff(4, can_visit=True),
        _build_staff(5, can_visit=False),
        _build_staff(6, can_visit=False),
        _build_staff(7, can_visit=False),
        _build_staff(8, can_visit=False),
        _build_staff(9, can_visit=False),
    ]
    settings = {
        "min_day_service": 4,
        "max_day_service": 7,
        "min_visit_am": 1,
        "min_visit_pm": 1,
        "min_dual_assignment": 0,
        "closed_days": [],
        "visit_operating_days": [0, 1, 2, 3, 4, 5, 6],
        "am_preferred_gender": "",
        "phone_duty_enabled": False,
        "phone_duty_max_consecutive": 1,
        "min_staff_at_9": 4,
        "min_staff_at_15": 4,
        "male_am_constraint_mode": "off",
        "placement_rules": [
            {
                "name": "相談員 午前1名以上",
                "rule_type": "qualification_min",
                "target_qualification_ids": [1],
                "period": "am",
                "min_count": 1,
                "is_hard": True,
                "is_active": True,
                "penalty_weight": 100,
            },
            {
                "name": "相談員 午後1名以上",
                "rule_type": "qualification_min",
                "target_qualification_ids": [1],
                "period": "pm",
                "min_count": 1,
                "is_hard": True,
                "is_active": True,
                "penalty_weight": 100,
            },
        ],
        "counselor_desk_enabled": True,
        "counselor_desk_count": 1,
    }

    shifts, warnings = _solve_care_with_fallback(
        2026,
        2,
        [dt],
        staff,
        [],
        settings,
        allowed_patterns={},
    )

    assert shifts is not None
    full_day_counselors = {
        item["staff_id"]
        for item in shifts
        if item["staff_id"] in {1, 2, 3} and item["assignment"] in PRESENT_FULL_DAY
    }
    assert len(full_day_counselors) >= 2
    assert "understaffed_counselor_full_day" not in {w["warning_type"] for w in warnings}


def test_min_full_day_counselor_keeps_one_counselor_on_site_all_day():
    dt = datetime.date(2026, 7, 6)
    staff_ids = list(range(1, 9))
    staff_by_id = {
        1: _build_staff(1, can_visit=True, qualifications=[1]),
        2: _build_staff(2, can_visit=True, qualifications=[1]),
        3: _build_staff(3, can_visit=True),
        4: _build_staff(4, can_visit=False),
        5: _build_staff(5, can_visit=False),
        6: _build_staff(6, can_visit=False),
        7: _build_staff(7, can_visit=False),
        8: _build_staff(8, can_visit=False),
    }

    shifts, warnings = _solve_care(
        2026,
        7,
        [dt],
        staff_ids,
        staff_by_id,
        off_request_set=set(),
        min_day_service=5,
        min_visit_am=1,
        min_visit_pm=1,
        min_dual=0,
        closed_days_set=set(),
        visit_operating_days=[0, 1, 2, 3, 4, 5, 6],
        am_preferred_gender="",
        phone_duty_enabled=False,
        phone_duty_max_consecutive=1,
        min_staff_at_9=5,
        min_staff_at_11=5,
        min_staff_at_13=5,
        min_staff_at_15=5,
        male_am_constraint_mode="off",
        placement_rules=[],
        counselor_staff_ids=[1, 2],
        min_counselor_staff=2,
        min_full_day_counselor=1,
        allowed_patterns={},
        max_day_service=6,
        use_slack=False,
    )

    assert shifts is not None
    counselor_assignments = {
        item["staff_id"]: item["assignment"]
        for item in shifts
        if item["staff_id"] in {1, 2}
    }
    assert any(
        assignment in PRESENT_FULL_DAY
        for assignment in counselor_assignments.values()
    )
    assert "understaffed_counselor_full_day" not in {w["warning_type"] for w in warnings}


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


def test_counselor_rotation_covers_all_slots_without_break_overlap():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_pattern1", "break_start": "11:00"},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "day_pattern1", "break_start": "14:00"},
    ]
    care_staff = [
        _build_staff(1, can_visit=True, qualifications=[1]),
        _build_staff(2, can_visit=True, qualifications=[1]),
    ]
    settings = {
        "placement_rules": [
            {
                "name": "相談員 午前1名以上",
                "target_qualification_ids": [1],
            }
        ],
        "counselor_desk_count": 1,
    }

    out, warnings = _assign_counselor_rotation(shifts, care_staff, settings, [dt])
    assigned = [
        item for item in out
        if item.get("counselor_desk_slots")
    ]

    assert not warnings
    assert len(assigned) == 2
    covered_slots = sorted(
        slot
        for item in assigned
        for slot in item["counselor_desk_slots"]
    )
    assert covered_slots == [0, 1, 2, 3]
    for item in assigned:
        for slot in item["counselor_desk_slots"]:
            assert not _break_overlaps_slot(item["break_start"], slot)


def test_dual_assignments_can_cover_edge_counselor_slots():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_p3_visit_pm", "break_start": "12:30"},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "visit_am_day_p4", "break_start": "12:30"},
    ]
    care_staff = [
        _build_staff(1, can_visit=True, qualifications=[1]),
        _build_staff(2, can_visit=True, qualifications=[1]),
    ]
    settings = {
        "placement_rules": [
            {
                "name": "相談員 午前1名以上",
                "target_qualification_ids": [1],
            }
        ],
        "counselor_desk_count": 1,
    }
    out, warnings = _assign_counselor_rotation(shifts, care_staff, settings, [dt])
    assigned = [item for item in out if item.get("counselor_desk_slots")]
    covered_slots = sorted(
        slot
        for item in assigned
        for slot in item["counselor_desk_slots"]
    )
    assert covered_slots == [0, 3]
    assert len(warnings) == 1
    assert warnings[0]["warning_type"] == "counselor_slot_unfilled"


def test_counselor_swap_does_not_assign_1230_break_to_full_day_staff():
    dt = datetime.date(2026, 6, 1)
    shifts = [
        {"date": dt.isoformat(), "staff_id": 1, "assignment": "day_p3_visit_pm", "break_start": "12:30"},
        {"date": dt.isoformat(), "staff_id": 2, "assignment": "visit_am_day_p4", "break_start": "12:30"},
        {"date": dt.isoformat(), "staff_id": 3, "assignment": "day_pattern1", "break_start": "10:00"},
        {"date": dt.isoformat(), "staff_id": 4, "assignment": "day_pattern1", "break_start": "11:00"},
    ]
    care_staff = [
        _build_staff(1, can_visit=True, qualifications=[1]),
        _build_staff(2, can_visit=True, qualifications=[1]),
        _build_staff(3, can_visit=True),
        _build_staff(4, can_visit=True),
    ]
    settings = {
        "placement_rules": [
            {
                "name": "相談員 午前1名以上",
                "target_qualification_ids": [1],
            }
        ],
        "counselor_desk_count": 1,
    }

    out, _warnings = _assign_counselor_rotation(shifts, care_staff, settings, [dt])
    full_day_breaks = {
        item["staff_id"]: item.get("break_start")
        for item in out
        if item["assignment"] == "day_pattern1"
    }
    assert set(full_day_breaks.values()).issubset(set(_BREAK_SLOTS))


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
        min_day_service=1,
        min_visit_am=0,
        min_visit_pm=1,
        min_dual=0,
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
