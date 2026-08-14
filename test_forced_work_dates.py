"""出勤可能日の「通常に加えて出勤する（追加・振替）」モードの検証。

ユーザー依頼（2026-08）:
  「出勤可能日は、ここに入れたらここしか出勤できないようにするか（現状）、
    シフト作成した内容に追加でここに出勤する、を選べるようにしてほしい。
    いつもは10日出るけどこの日は休んで11日に出勤したい、といった振替の要望があるため」

・追加モードの日は必ず出勤する（勤務可能曜日・固定休・休み希望より優先）
・休み希望とセットで「10日休み → 11日出勤」の振替ができる
"""
import calendar
import datetime

from solver import _solve_care_with_fallback

YEAR, MONTH = 2026, 9
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def _staff(staff_id, **over):
    s = {
        "id": staff_id,
        "name": f"s{staff_id}",
        "employment_type": "常勤",
        "can_visit": False,
        "max_consecutive_days": 31,
        "max_days_per_week": 7,
        "min_days_per_week": 0,
        "available_days": list(ALL_DAYS),
        "available_time_slots": "full_day",
        "fixed_days_off": [],
        "staff_group": "care",
        "gender": "female",
        "has_phone_duty": False,
        "qualification_ids": [],
        "weekend_constraint": "",
        "holiday_ng": False,
    }
    s.update(over)
    return s


def _settings(**over):
    st = {
        "min_day_service": 1,
        "max_day_service": 3,
        "min_visit_am": 0,
        "min_visit_pm": 0,
        "closed_days": [],
        "visit_operating_days": [],
        "no_day_service_days": [],
        "am_preferred_gender": "",
        "phone_duty_enabled": False,
        "min_staff_at_9": 1,
        "min_staff_at_15": 1,
        "male_am_constraint_mode": "off",
        "placement_rules": [],
    }
    st.update(over)
    return st


def _run(staff, settings, dayoffs=()):
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    return _solve_care_with_fallback(
        YEAR, MONTH, all_dates, staff, list(dayoffs), settings, allowed_patterns={}
    )


def _worked(shifts, staff_id, iso):
    return any(
        i["staff_id"] == staff_id and i["date"] == iso and i["assignment"] != "off"
        for i in shifts
    )


def test_forced_work_date_overrides_fixed_day_off():
    """勤務不可曜日（固定休）でも、追加出勤日に登録した日は出勤する。"""
    # 9/12 は土曜。土日固定休の職員でも追加出勤日なら出勤する
    staff = [_staff(1, fixed_days_off=[5, 6]), _staff(2), _staff(3)]
    settings = _settings(forced_work_dates=[(1, "2026-09-12")])
    shifts, _ = _run(staff, settings)

    assert shifts is not None
    assert _worked(shifts, 1, "2026-09-12"), "追加出勤日に出勤していない"
    assert not _worked(shifts, 1, "2026-09-05"), "他の土曜まで出勤している"


def test_forced_work_date_enables_swap_with_day_off_request():
    """10日を休み希望、11日を追加出勤にすると振替になる。"""
    staff = [_staff(1), _staff(2), _staff(3)]
    settings = _settings(forced_work_dates=[(1, "2026-09-11")])
    dayoffs = [{"staff_id": 1, "date": "2026-09-10"}]
    shifts, _ = _run(staff, settings, dayoffs)

    assert shifts is not None
    assert not _worked(shifts, 1, "2026-09-10"), "休み希望日に出勤している"
    assert _worked(shifts, 1, "2026-09-11"), "振替の出勤日に出勤していない"


def test_forced_work_date_on_same_day_as_off_request_prefers_work():
    """同じ日に休み希望と追加出勤が重なったら出勤を優先する（明示指定のため）。"""
    staff = [_staff(1), _staff(2), _staff(3)]
    settings = _settings(forced_work_dates=[(1, "2026-09-10")])
    dayoffs = [{"staff_id": 1, "date": "2026-09-10"}]
    shifts, _ = _run(staff, settings, dayoffs)

    assert shifts is not None
    assert _worked(shifts, 1, "2026-09-10")


def test_forced_work_date_ignored_on_closed_day():
    """休業曜日は全員休みのまま（追加出勤日でも出勤させない＝無解にしない）。"""
    staff = [_staff(1), _staff(2), _staff(3)]
    # 9/6 は日曜。日曜休業に設定した状態で日曜を追加出勤日にしても無解にならない
    settings = _settings(closed_days=[6], forced_work_dates=[(1, "2026-09-06")])
    shifts, _ = _run(staff, settings)

    assert shifts is not None
    assert not _worked(shifts, 1, "2026-09-06")
