"""ユーザー依頼 2026-08:「13:30-17:30 のシフトはそのままで 12:30-16:30 を看護・介護へ追加」。

デイ④(day_pattern4 / 13:30-17:30) を残したまま、1時間前倒しの午後半日枠
デイ⑦(day_pattern5 / care_7 / 12:30-16:30) を介護・看護の両方に追加した。
看護も介護も staff_group="care" なので、ケアスタッフ用パターンを1件足せば
両方の「許可シフトパターン」欄に出る。
"""
import datetime
import importlib
from typing import List, Optional

from solver import (
    AM_ONLY_FORBIDDEN,
    ASSIGNMENT_TIME_RANGES,
    CARE_ASSIGNMENTS,
    DAY_PM_ASSIGNMENTS,
    PM_ONLY_FORBIDDEN,
    PRESENT_AT_13,
    PRESENT_AT_15,
    _solve_care_with_fallback,
)


def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "test.db"))
    import config as config_module
    import app as app_module
    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    return app_module, app_module.create_app()


def _build_staff(
    staff_id: int,
    *,
    available_time_slots: str = "full_day",
    qualifications: Optional[List[int]] = None,
) -> dict:
    return {
        "id": staff_id,
        "name": f"s{staff_id}",
        "employment_type": "常勤",
        "can_visit": False,
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


_SETTINGS = {
    "min_day_service": 0,
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
    "placement_rules": [],
    "counselor_desk_enabled": False,
    "counselor_desk_count": 1,
}


def test_master_has_both_pm_half_day_patterns(tmp_path, monkeypatch):
    """④13:30-17:30 はそのまま、⑦12:30-16:30 が増えていること。"""
    _app_module, flask_app = _load_app(tmp_path, monkeypatch)
    from models import ShiftPattern

    with flask_app.app_context():
        old = ShiftPattern.query.filter_by(code="care_4").first()
        assert old is not None
        assert (old.start_time, old.end_time) == ("13:30", "17:30")

        new = ShiftPattern.query.filter_by(code="care_7").first()
        assert new is not None, "12:30-16:30 の種類がマスタに無い"
        assert (new.start_time, new.end_time) == ("12:30", "16:30")
        assert new.staff_group == "care"       # 看護・介護 共通の枠
        assert new.covers_pm and not new.covers_am


def test_care_7_is_saved_as_day_pattern5(tmp_path, monkeypatch):
    """許可シフトパターンとして保存でき、solver コードへ変換されること。"""
    app_module, flask_app = _load_app(tmp_path, monkeypatch)

    with flask_app.app_context():
        assert app_module.normalize_allowed_pattern_codes(
            ["care_4", "care_7"], "care"
        ) == ["day_pattern4", "day_pattern5"]


def test_day_pattern5_time_range_and_sets():
    assert "day_pattern5" in CARE_ASSIGNMENTS
    assert ASSIGNMENT_TIME_RANGES["day_pattern5"] == (12 * 60 + 30, 16 * 60 + 30)
    # 午後枠なので午後在籍にカウントし、午前のみ勤務では取れない
    assert "day_pattern5" in DAY_PM_ASSIGNMENTS
    assert "day_pattern5" in AM_ONLY_FORBIDDEN
    assert "day_pattern5" not in PM_ONLY_FORBIDDEN
    # 12:30 開始なので 13時にも 15時にも在席している（④は13:30開始なので13時は不在）
    assert "day_pattern5" in PRESENT_AT_13
    assert "day_pattern4" not in PRESENT_AT_13
    assert "day_pattern5" in PRESENT_AT_15


#   週の勤務日数下限は「7日そろった週」でないと効かない（不完全週は按分で0になる）ので、
#   出勤を強制したいテストは1週間ぶんの日付を渡す。
_WEEK = [datetime.date(2026, 6, 1) + datetime.timedelta(days=i) for i in range(7)]


def test_pm_only_staff_can_get_1230_1630():
    """午後のみ勤務で⑦だけ許可した職員に 12:30-16:30 が入ること。"""
    staff = [_build_staff(1, available_time_slots="pm_only")]
    staff[0]["min_days_per_week"] = 1

    shifts, _warnings = _solve_care_with_fallback(
        2026, 6, _WEEK, staff, [], dict(_SETTINGS),
        allowed_patterns={1: ["day_pattern5"]},
    )

    assert shifts is not None
    worked = [s["assignment"] for s in shifts if s["assignment"] != "off"]
    assert worked == ["day_pattern5"], worked


def test_checked_full_day_staff_gets_1230_1630():
    """⑦をチェックした職員は、終日勤務でも自動作成で 12:30-16:30 に入れる。"""
    staff = [_build_staff(1)]
    staff[0]["min_days_per_week"] = 1

    shifts, _warnings = _solve_care_with_fallback(
        2026, 6, _WEEK, staff, [], dict(_SETTINGS),
        allowed_patterns={1: ["day_pattern5"]},
    )

    assert shifts is not None
    worked = [s["assignment"] for s in shifts if s["assignment"] != "off"]
    assert worked == ["day_pattern5"], worked


def test_unchecked_staff_never_gets_1230_1630():
    """チェックなし（＝全パターン許可）の職員には⑦を付けない。"""
    staff = [_build_staff(i) for i in (1, 2, 3, 4)]
    for s in staff:
        s["min_days_per_week"] = 1
    # 午後のみ勤務でも、チェックが無ければ⑦ではなく④になる
    staff.append(_build_staff(5, available_time_slots="pm_only"))
    staff[-1]["min_days_per_week"] = 1

    shifts, _warnings = _solve_care_with_fallback(
        2026, 6, _WEEK, staff, [], dict(_SETTINGS), allowed_patterns={},
    )

    assert shifts is not None
    worked = [s["assignment"] for s in shifts if s["assignment"] != "off"]
    assert worked, "出勤日が1日も無いとテストにならない"
    assert "day_pattern5" not in worked, worked


def test_full_day_staff_never_gets_half_day_automatically():
    """終日勤務できる職員に半日⑦を自動では付けない（④と同じ扱い）。"""
    staff = [_build_staff(i) for i in (1, 2, 3, 4)]
    for s in staff:
        s["min_days_per_week"] = 1

    shifts, _warnings = _solve_care_with_fallback(
        2026, 6, _WEEK, staff, [], dict(_SETTINGS), allowed_patterns={},
    )

    assert shifts is not None
    worked = [s["assignment"] for s in shifts if s["assignment"] != "off"]
    assert worked, "出勤日が1日も無いとテストにならない"
    assert "day_pattern5" not in worked, worked
    assert "day_pattern4" not in worked, worked
