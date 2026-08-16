import datetime
from typing import List, Optional

from solver import (
    DAY_PM_ASSIGNMENTS,
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
        # 13時在籍は min_day_service と同数を要求する。終日勤務できるのは4名
        # （残りは午前のみ/午後のみ）なので、5名要求で必ず不足になる。
        "min_day_service": 5,
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


def _base_settings(**overrides):
    st = {
        "min_day_service": 2,
        "max_day_service": 3,
        "min_visit_am": 0,
        "min_visit_pm": 0,
        "closed_days": [],
        "visit_operating_days": [],
        "no_day_service_days": [],
        "am_preferred_gender": "",
        "phone_duty_enabled": False,
        "phone_duty_max_consecutive": 1,
        "min_staff_at_9": 1,
        "min_staff_at_15": 1,
        "male_am_constraint_mode": "off",
        "placement_rules": [],
    }
    st.update(overrides)
    return st


def _run(settings, num_staff=6, can_visit=False, month_dates=None):
    dates = month_dates or [datetime.date(2026, 6, 1)]   # 2026-06-01 は月曜
    staff = [_build_staff(i, can_visit=can_visit) for i in range(1, num_staff + 1)]
    return _solve_care_with_fallback(
        2026, 6, dates, staff, [], settings, allowed_patterns={}
    )


def _working_ids(shifts):
    return {i["staff_id"] for i in shifts if i["assignment"] != "off"}


def test_day_service_max_is_taken_literally():
    """デイ最大3名の設定に内部で早番/遅番・休憩バッファを上乗せしない。"""
    shifts, _ = _run(_base_settings())
    assert shifts is not None
    pm = [i for i in shifts if i["assignment"] in DAY_PM_ASSIGNMENTS]
    assert len(pm) <= 3, f"デイ午後が上限3名を超えた: {len(pm)}名"


def test_care_headcount_by_weekday_caps_staff():
    """曜日ごとの介護配置人数（最大）でその日の頭数を抑えられる。"""
    shifts, _ = _run(_base_settings(care_max_by_weekday="2,,,,,,"))
    assert shifts is not None
    assert len(_working_ids(shifts)) <= 2


def test_care_headcount_by_weekday_min_fills_up():
    """曜日ごとの介護配置人数（最低）でその日の頭数を確保できる。"""
    shifts, _ = _run(_base_settings(max_day_service=5,
                                    care_min_by_weekday="4,,,,,,",
                                    care_max_by_weekday="4,,,,,,"))
    assert shifts is not None
    assert len(_working_ids(shifts)) == 4


def test_no_day_service_weekday_defaults_to_two_staff():
    """曜日設定が空なら、デイ非営業曜日は従来どおり原則2名。"""
    shifts, _ = _run(_base_settings(no_day_service_days=[0]))
    assert shifts is not None
    assert len(_working_ids(shifts)) == 2


def test_care_headcount_over_limit_warns_but_generates():
    """必須配置（早番・遅番・訪問）が上限に収まらない日は警告のみで生成継続。"""
    shifts, warnings = _run(
        _base_settings(
            no_day_service_days=[0],
            visit_operating_days=[0],
            min_visit_am=1,
            min_visit_pm=1,
            care_max_by_weekday="2,,,,,,",
        ),
        can_visit=True,
    )
    assert shifts is not None
    # 上限に収まらない分は「人数超過」か「訪問が埋まらない」の警告として出る。
    #   早番は訪問に数えないルールに変えたため（2026-08）、上限を守って
    #   訪問が不足する形になることもある。どちらでも生成は続く。
    assert any(w["warning_type"] in (
        "over_staffed_care", "understaffed_visit_am", "understaffed_visit_pm",
    ) for w in warnings), [w["warning_type"] for w in warnings]


def test_backup_staff_used_only_when_needed():
    """応援職員（backup_only）は、他の職員で足りるときは入れない。

    ユーザー依頼 2026-08:「ヘルパーステーションヘルプは人数が本当に
    足りないときに入れます。公休を多くしているのに応援を入れるのはおかしい」。
    """
    from solver import generate_shift

    def _st(i, backup=False, maxw=5):
        return {
            "id": i, "name": ("応援" if backup else "介護") + str(i),
            "employment_type": "常勤", "can_visit": True,
            "max_consecutive_days": 5, "max_days_per_week": maxw,
            "min_days_per_week": 0, "available_days": [0, 1, 2, 3, 4, 5, 6],
            "available_time_slots": "full_day", "fixed_days_off": [],
            "staff_group": "care", "gender": "female", "has_phone_duty": False,
            "qualification_ids": [], "qualification_names": [],
            "qualification_codes": ["care_worker"],
            "weekend_constraint": "", "holiday_ng": False,
            "backup_only": backup,
        }

    settings = {
        "min_day_service": 2, "min_visit_am": 0, "min_visit_pm": 0,
        "closed_days": [6], "visit_operating_days": [],
        "min_cooking_staff": 0, "min_cooking_overlap": 0, "placement_rules": [],
        "cooking_combo_rules": [], "min_staff_at_9": 2, "min_staff_at_15": 2,
        "max_day_service": 4, "min_early_staff": 1, "min_late_staff": 1,
    }
    # 通常の職員だけで足りる人数を用意し、応援を1名混ぜる
    care = [_st(i) for i in range(1, 8)] + [_st(99, backup=True)]
    shifts, _w = generate_shift(2026, 9, care, [], [], settings)
    backup_days = [s for s in shifts if s["staff_id"] == 99 and s["assignment"] != "off"]
    assert not backup_days, f"応援が {len(backup_days)} 日入っている"

    # 逆に人が足りないときは応援が入る
    few = [_st(i) for i in range(1, 3)] + [_st(99, backup=True)]
    shifts2, _w2 = generate_shift(2026, 9, few, [], [], settings)
    backup_days2 = [s for s in shifts2 if s["staff_id"] == 99 and s["assignment"] != "off"]
    assert backup_days2, "人手が足りないのに応援が入っていない"
