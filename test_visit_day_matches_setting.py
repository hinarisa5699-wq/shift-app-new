"""訪問営業日の設定どおりに訪問が入るかのテスト。

ユーザー依頼 2026-09:
  「訪問介護兼務可能は設定にある曜日訪問設定に＝じゃないと困る」
  「訪問介護の曜日 火曜日金曜日 に 訪問介護可能の人が1人もいない（不具合）」

不具合の中身:
  訪問営業日が「デイ利用者がいない曜日」と重なると、その日はデイ午前/午後の
  上限（曜日ごとの介護配置人数＝既定2名）がハード制約になっていたため、
  早番＋遅番でデイ午後の枠が埋まり、兼務訪問（visit_am_day_p4＝AM訪問＋PMデイ）を
  1人も置けなかった。結果、その曜日は毎週「訪問介護午前: 1名不足」となり
  訪問が一度も入らなかった。

  → デイ上限をスラック段階では超過可（＝必須配置を優先し警告にとどめる）にした。
     頭数の上限（care_headcount_by_day）は元からスラックで、そちらと揃えた形。
"""
import calendar
import datetime

from solver import generate_shift

YEAR, MONTH = 2026, 10
VISIT_WEEKDAYS = [1, 4]        # 火・金
NO_DAY_SERVICE = [1, 3, 5]     # 火・木・土（訪問日の火と重なるのが要点）


def _staff(sid, name, can_visit, job_category="caregiver"):
    return {
        "id": sid,
        "name": name,
        "employment_type": "常勤",
        "job_category": job_category,
        "department": "介護",
        "staff_group": "care",
        "can_visit": can_visit,
        "gender": "female",
        "max_consecutive_days": 5,
        "max_days_per_week": 5,
        "min_days_per_week": 0,
        "available_days": [0, 1, 2, 3, 4, 5, 6],
        "available_time_slots": "full_day",
        "fixed_days_off": [],
        "required_days": [],
        "holiday_ng": False,
        "public_holiday_target": 0,
    }


def _settings():
    return {
        "min_day_service": 3,
        "max_day_service": 5,
        "min_visit_am": 1,
        "min_visit_pm": 0,
        "min_early_staff": 1,
        "min_late_staff": 1,
        "min_bath_mid": 0,
        "min_bath_out": 0,
        "closed_days": [],
        "visit_operating_days": VISIT_WEEKDAYS,
        "day_service_operating_days": [0, 2, 4, 6],
        "no_day_service_days": NO_DAY_SERVICE,
        "no_day_service_min_staff": 2,
        "phone_duty_enabled": False,
        "auto_public_holidays": False,
    }


def _care_staff():
    return [
        _staff(1, "訪問可A", True),
        _staff(2, "訪問可B", True),
        _staff(3, "訪問不可C", False),
        _staff(4, "訪問不可D", False),
        _staff(5, "訪問不可E", False),
        _staff(6, "訪問不可F", False),
    ]


def _visit_days_of(shifts):
    """訪問（兼務を含む）が割り当たった日付の集合。"""
    visit = {"visit_am", "visit_pm", "visit_am_day_p4", "day_p3_visit_pm"}
    return {s["date"] for s in shifts if s.get("assignment") in visit}


def _run():
    shifts, warnings = generate_shift(
        YEAR, MONTH, _care_staff(), [], [], _settings())
    assert shifts is not None, "シフトを生成できませんでした"
    return shifts, warnings


def test_visit_is_assigned_on_every_visit_weekday():
    """設定した訪問営業日には必ず訪問が入る（デイ利用者なしの曜日と重なっても）。"""
    shifts, _warnings = _run()
    got = _visit_days_of(shifts)

    missing = []
    for day in range(1, calendar.monthrange(YEAR, MONTH)[1] + 1):
        dt = datetime.date(YEAR, MONTH, day)
        if dt.weekday() in VISIT_WEEKDAYS and dt.isoformat() not in got:
            missing.append(dt.isoformat())
    assert not missing, f"訪問営業日なのに訪問が入っていない日: {missing}"


def test_visit_never_lands_outside_the_setting():
    """訪問営業日に設定していない曜日には訪問を入れない（設定＝実際）。"""
    shifts, _warnings = _run()
    outside = sorted(
        d for d in _visit_days_of(shifts)
        if datetime.date.fromisoformat(d).weekday() not in VISIT_WEEKDAYS
    )
    assert not outside, f"訪問営業日ではない日に訪問が入っている: {outside}"


def test_only_visit_capable_staff_take_visits():
    """訪問の枠に入るのは訪問介護兼務可の職員だけ。"""
    shifts, _warnings = _run()
    visit = {"visit_am", "visit_pm", "visit_am_day_p4", "day_p3_visit_pm"}
    capable_ids = {s["id"] for s in _care_staff() if s["can_visit"]}
    bad = [
        s for s in shifts
        if s.get("assignment") in visit and s["staff_id"] not in capable_ids
    ]
    assert not bad, f"訪問可でない職員が訪問に入っている: {bad}"


def test_exceeding_the_day_service_cap_is_reported():
    """上限を超えて必須配置を通した日は、黙って超えずに警告で知らせる。"""
    _shifts, warnings = _run()
    kinds = {w.get("warning_type") for w in warnings}
    # 火曜は「デイ利用者なし＝2名」に早番・遅番・訪問の3名を入れるため超過する
    assert "over_staffed_day_service" in kinds or "over_staffed_care" in kinds, (
        f"上限超過の警告が出ていません: {sorted(kinds)}"
    )
