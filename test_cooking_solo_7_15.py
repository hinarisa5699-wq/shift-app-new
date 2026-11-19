"""調理が足りない日の1人編成は 7:00-15:00 で入る（2026-11-19 依頼）。

依頼: 「朝は6時半じゃなくて7時から」「誰もいなかったら7-15で一人」。
調理シフト種類に 7:00-15:00 があれば、朝食の有無に関わらず1人編成はそれを使い、
6:30-14:30・9:00-16:00 の1人編成は使わないこと。
"""
import test_cooking_breakfast_shift as base

SOLO = {"code": "cooking_23", "label": "(22) 7:00-15:00", "start_time": "07:00", "end_time": "15:00"}


def _settings():
    s = base._base_settings()
    s["cooking_types"] = base.COOKING_TYPES + [SOLO]
    return s


def _run(settings):
    allowed = {k: set(v) | {"cooking_23"} for k, v in base.ALLOWED.items()}
    orig = base.ALLOWED
    base.ALLOWED = allowed
    try:
        return base._run(settings)
    finally:
        base.ALLOWED = orig


def test_thin_day_uses_7_15_solo():
    shifts, warnings = _run(_settings())
    thin = [s["assignment"] for s in shifts if s["date"] == base.THIN_DAY]
    assert thin == ["cooking_23"], f"1人の日は7:00-15:00で組むこと: {thin}"
    unassigned = [w for w in warnings if w["warning_type"] == "cook_combo_unassigned"
                  and w["date"] == base.THIN_DAY]
    assert unassigned == [], unassigned


def test_7_15_solo_also_in_breakfast_off_period():
    s = _settings()
    s["breakfast_off_start"] = "2026-08-01"
    s["breakfast_off_end"] = "2026-08-31"
    shifts, _ = _run(s)
    thin = [x["assignment"] for x in shifts if x["date"] == base.THIN_DAY]
    assert thin == ["cooking_23"], f"朝食なし期間も1人は7:00-15:00: {thin}"


def test_7_15_solo_not_used_on_normal_days():
    shifts, _ = _run(_settings())
    solo_days = {s["date"] for s in shifts if s["assignment"] == "cooking_23"}
    assert solo_days <= {base.THIN_DAY}, f"通常の日まで1人体制になっている: {sorted(solo_days)}"
    assert not [s for s in shifts if s["assignment"] in ("cooking_9", "cooking_6")
                and s["date"] == base.THIN_DAY]
