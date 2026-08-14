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


def test_nurse_not_counted_in_care_headcount():
    """看護師は曜日ごとの介護配置人数に数えない（配置ルールの有無に関係なく）。

    2026-08 本番: 看護師がデイに入った木曜が「介護3名（上限2名）」と警告されていた。
    看護師の判定を配置ルール名頼りにしていたため（対象資格が未設定だと素通り）。
    """
    care = [_staff(1), _staff(2)]
    # 看護師は毎日必ず出勤させる（＝介護2名＋看護1名の日を必ず作る）
    nurse = _staff(3, qualification_codes=["nurse"], qualification_names=["看護師"],
                   min_days_per_week=7)
    settings = _settings(care_min_by_weekday="2,2,2,2,2,2,2",
                         care_max_by_weekday="2,2,2,2,2,2,2")
    shifts, warnings = _run(care + [nurse], settings)

    assert shifts is not None
    nurse_days = {i["date"] for i in shifts
                  if i["staff_id"] == 3 and i["assignment"] != "off"}
    assert len(nurse_days) >= 20, f"看護師がほとんど出勤していない: {len(nurse_days)}日"
    # 看護師が出た日も介護は2名のまま＝看護師は人数に数えられていない
    three_people_days = 0
    for d in nurse_days:
        working = {i["staff_id"] for i in shifts
                   if i["date"] == d and i["assignment"] != "off"}
        if len(working) == 3:
            three_people_days += 1
    assert three_people_days >= 20, "介護2名＋看護1名の日が作れていない"
    assert not [w for w in warnings if w["warning_type"] == "over_staffed_care"], (
        "看護師が介護人数に数えられている"
    )


def test_oncall_staff_must_work_on_duty_day():
    """オンコール担当はその当番日に出勤する（電話を持ち帰るため）。"""
    staff = [_staff(1, fixed_days_off=[5, 6]), _staff(2), _staff(3)]
    settings = _settings(oncall_must_work=[(1, "2026-09-10")])
    shifts, _ = _run(staff, settings)

    assert shifts is not None
    assert _worked(shifts, 1, "2026-09-10")


def test_oncall_must_work_degrades_to_warning_when_impossible():
    """出勤にできない日（休み希望と重なる等）は警告にとどめ、生成は続ける。"""
    # 出勤可能日を1日だけに限定した職員に、別の日の当番を割り当てる
    staff = [
        _staff(1, workable_dates=["2026-09-01"]),
        _staff(2), _staff(3),
    ]
    settings = _settings(oncall_must_work=[(1, "2026-09-10")])
    shifts, warnings = _run(staff, settings)

    assert shifts is not None, "無解にしてはいけない"
    assert not _worked(shifts, 1, "2026-09-10")
    assert any(w["warning_type"] == "oncall_staff_not_working" for w in warnings)


def test_required_weekday_forces_attendance():
    """「必ず出勤する曜日」に設定した曜日は毎週出勤する（内田さん＝水曜必須）。"""
    staff = [_staff(1, required_days=[2]), _staff(2), _staff(3)]
    settings = _settings()
    shifts, _ = _run(staff, settings)

    assert shifts is not None
    wednesdays = ["2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23", "2026-09-30"]
    for d in wednesdays:
        assert _worked(shifts, 1, d), f"{d}(水) に出勤していない"


def test_required_weekday_yields_to_day_off_request():
    """必須曜日でも、その日に休み希望があれば休みが優先される。"""
    staff = [_staff(1, required_days=[2]), _staff(2), _staff(3)]
    settings = _settings()
    shifts, _ = _run(staff, settings, [{"staff_id": 1, "date": "2026-09-09"}])

    assert shifts is not None
    assert not _worked(shifts, 1, "2026-09-09")
    assert _worked(shifts, 1, "2026-09-02")


def test_driver_not_counted_in_care_headcount():
    """ドライバー（送迎担当）は介護の配置人数に数えない。"""
    care = [_staff(1), _staff(2)]
    driver = _staff(3, job_category="driver", min_days_per_week=7, required_days=[0,1,2,3,4,5,6])
    settings = _settings(care_min_by_weekday="2,2,2,2,2,2,2",
                         care_max_by_weekday="2,2,2,2,2,2,2")
    shifts, warnings = _run(care + [driver], settings)

    assert shifts is not None
    driver_days = {i["date"] for i in shifts
                   if i["staff_id"] == 3 and i["assignment"] != "off"}
    assert len(driver_days) >= 20, f"ドライバーが出勤できていない: {len(driver_days)}日"
    assert not [w for w in warnings if w["warning_type"] == "over_staffed_care"], (
        "ドライバーが介護人数に数えられている"
    )
