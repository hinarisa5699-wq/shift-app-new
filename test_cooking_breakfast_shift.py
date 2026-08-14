"""朝食あり日に 9:00-16:00 ではなく 6:30-14:30 を使う（朝[6-8)不足を出さない）。

依頼: 「朝食あり日の1人勤務を追加して職員にも✓しているのに、朝食アリの期間なのに
9-16 単独で入って 6-8 エラーになる」。
9-16 は朝食なし用の時間帯なので、朝食あり日は 6:30-14:30 に置き換わること、
朝食なし期間では従来どおり 9-16 が使われることを確認する。
"""
import calendar
import datetime

from solver import _solve_cooking_with_fallback

YEAR, MONTH = 2026, 8

COOKING_TYPES = [
    {"code": "cooking_3", "label": "③ 12:00-19:00", "start_time": "12:00", "end_time": "19:00"},
    {"code": "cooking_4", "label": "④ 6:00-13:00", "start_time": "06:00", "end_time": "13:00"},
    {"code": "cooking_5", "label": "⑤ 9:00-15:00", "start_time": "09:00", "end_time": "15:00"},
    {"code": "cooking_6", "label": "⑥ 9:00-16:00", "start_time": "09:00", "end_time": "16:00"},
    {"code": "cooking_7", "label": "⑦ 6:00-12:00", "start_time": "06:00", "end_time": "12:00"},
    {"code": "cooking_8", "label": "⑧ 13:00-19:00", "start_time": "13:00", "end_time": "19:00"},
    {"code": "cooking_9", "label": "⑨ 6:30-14:30", "start_time": "06:30", "end_time": "14:30"},
]

# 朝を賄えない編成（⑥単独・⑥+⑤）を含む組み合わせルール
COMBOS = [
    ["cooking_3", "cooking_7"],
    ["cooking_4", "cooking_8"],
    ["cooking_6"],
    ["cooking_6", "cooking_5"],
]

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
STAFF = [
    {"id": 1, "name": "A", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 5, "public_holiday_count": 9},
    {"id": 2, "name": "B", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 5, "public_holiday_count": 9},
    {"id": 3, "name": "C", "employment_type": "常勤", "available_days": ALL_DAYS,
     "max_days_per_week": 5, "max_consecutive_days": 6, "public_holiday_count": 8},
    {"id": 4, "name": "D", "employment_type": "パート", "available_days": ALL_DAYS,
     "max_days_per_week": 3, "max_consecutive_days": 3, "public_holiday_count": 20},
]
ALLOWED = {
    1: {"cooking_4", "cooking_6", "cooking_9"},
    2: {"cooking_3", "cooking_6", "cooking_9"},
    3: {"cooking_7", "cooking_8"},
    4: {"cooking_5"},
}

# 1人しか出勤できない日（＝1人編成しか組めない日）
THIN_DAY = "2026-08-04"
# 全員休みの日を1日入れてスラック付きフェーズ（不足を警告に降格するフェーズ）で解かせる。
# 実データで 6-8 不足警告が出ていたのはこのフェーズ。
EMPTY_DAY = "2026-08-09"


def _base_settings():
    return {
        "min_cooking_staff": 1,
        "cooking_types": COOKING_TYPES,
        "cooking_combo_rules": [
            {"id": i, "name": f"組{i}", "allowed_patterns": c, "is_active": True}
            for i, c in enumerate(COMBOS, 1)
        ],
        "cooking_pair_target": 0,
        "breakfast_off_start": "",
        "breakfast_off_end": "",
        "closed_dates": "",
    }


def _run(settings):
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    day_off = [{"staff_id": s["id"], "date": THIN_DAY} for s in STAFF if s["id"] != 2]
    day_off += [{"staff_id": s["id"], "date": EMPTY_DAY} for s in STAFF]
    shifts, warnings = _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, STAFF, day_off, settings,
        allowed_patterns=ALLOWED, locked_assignments={},
    )
    return shifts, warnings


def test_breakfast_day_uses_morning_shift_instead_of_9_16():
    shifts, warnings = _run(_base_settings())

    thin = [s["assignment"] for s in shifts if s["date"] == THIN_DAY]
    assert thin == ["cooking_9"], f"朝食あり日は6:30-14:30で組むこと: {thin}"

    # 9:00-16:00 は朝食あり期間では使わない
    assert not [s for s in shifts if s["assignment"] == "cooking_6"]

    # 全員休みの日以外に朝[6-8)不足の警告を出さない
    morning_short = [
        w for w in warnings
        if w["warning_type"] == "understaffed_cook_interval_0" and w["date"] != EMPTY_DAY
    ]
    assert morning_short == [], morning_short


def test_breakfast_day_avoids_morning_less_combo_without_9_16():
    """9-16 を含まない「朝を賄えない編成」(⑤+⑧)も朝食あり日は避けること。

    置換版(6:30-14:30)を作れるのは9-16を含む編成だけなので、それ以外の朝なし編成が
    無罰のままだと ⑤9-15+⑧13-19 に逃げて 6-8 不足が出続ける。
    """
    settings = _base_settings()
    settings["cooking_combo_rules"] = [
        {"id": 1, "name": "朝あり", "allowed_patterns": ["cooking_4", "cooking_8"],
         "is_active": True},
        {"id": 2, "name": "朝なし", "allowed_patterns": ["cooking_5", "cooking_8"],
         "is_active": True},
    ]
    shifts, warnings = _run(settings)

    # ④6-13 を組める職員が居る日に ⑤9-15 の朝なし編成へ逃げていないこと
    assert not [s for s in shifts if s["assignment"] == "cooking_5"], "朝なし編成が選ばれた"
    morning_short = [
        w for w in warnings
        if w["warning_type"] == "understaffed_cook_interval_0" and w["date"] != EMPTY_DAY
    ]
    assert morning_short == [], morning_short


def test_breakfast_day_avoids_9_16_even_without_morning_shift_in_master():
    """種類マスタに 6:30-14:30 が無くても、朝食あり日は 9-16 の朝なし編成を避ける。

    本番8月で「調理 6:00-8:00 1名不足」が8日出ていた状況＝マスタに朝の1人勤務が
    無いケース。旧実装はそれが無いと朝なし編成のペナルティ自体を作らないため、
    朝あり編成を組める日でも公休合わせを優先して ⑥9-16 に居座っていた。
    """
    types = [t for t in COOKING_TYPES if t["code"] != "cooking_9"]
    combos = [
        ["cooking_4", "cooking_8"],  # 朝あり（④6-13 + ⑧13-19）
        ["cooking_6"],               # 朝なし（⑥9-16 のみ）
    ]
    # X は朝担当(④)を組めるが公休目標が多く、休ませた方が公休ペナルティは軽い。
    # 旧実装ではこの日が ⑥ 単独＝朝不足になっていた。
    staff = [
        {"id": 1, "name": "X", "employment_type": "パート", "available_days": ALL_DAYS,
         "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 20},
        {"id": 2, "name": "Y", "employment_type": "常勤", "available_days": ALL_DAYS,
         "max_days_per_week": 7, "max_consecutive_days": 31, "public_holiday_count": 0},
    ]
    settings = _base_settings()
    settings["cooking_types"] = types
    settings["cooking_combo_rules"] = [
        {"id": i, "name": f"組{i}", "allowed_patterns": c, "is_active": True}
        for i, c in enumerate(combos, 1)
    ]
    days = calendar.monthrange(YEAR, MONTH)[1]
    all_dates = [datetime.date(YEAR, MONTH, d) for d in range(1, days + 1)]
    # 全員休みの日を1日入れてスラック付きフェーズ（＝実データで不足警告が出た
    # フェーズ）で解かせる。ハードフェーズだけだと不足自体が起きない。
    day_off = [{"staff_id": s["id"], "date": EMPTY_DAY} for s in staff]
    shifts, warnings = _solve_cooking_with_fallback(
        YEAR, MONTH, all_dates, staff, day_off, settings,
        allowed_patterns={1: {"cooking_4", "cooking_6"}, 2: {"cooking_8", "cooking_6"}},
        locked_assignments={},
    )

    morning_short = [
        w for w in warnings
        if w["warning_type"] == "understaffed_cook_interval_0" and w["date"] != EMPTY_DAY
    ]
    assert morning_short == [], morning_short
    assert not [s for s in shifts if s["assignment"] == "cooking_6"], "朝なし編成が選ばれた"


def test_no_breakfast_period_still_uses_9_16():
    settings = _base_settings()
    settings["breakfast_off_start"] = "2026-08-01"
    settings["breakfast_off_end"] = "2026-08-31"

    shifts, _ = _run(settings)

    thin = [s["assignment"] for s in shifts if s["date"] == THIN_DAY]
    assert thin == ["cooking_6"], f"朝食なし期間は9:00-16:00のまま: {thin}"
    assert not [s for s in shifts if s["assignment"] == "cooking_9"]


def test_breakfast_off_converts_six_start_to_eight_start():
    """朝食なし日は 6:00 開始（④6-13・⑦6-12）が 8:00 開始（②8-13）に置き換わる。

    ユーザー依頼（2026-08）:「朝ごはんがない日は 6-13 の部分が 8-13 に変換される
    ようにして」。終了時刻が同じ 8時開始の種類（②8:00-13:00）があればそれを使う。
    """
    from solver import _bf_off_replacement_map

    ranges = {
        "cooking_2": (8 * 60, 13 * 60),
        "cooking_4": (6 * 60, 13 * 60),
        "cooking_7": (6 * 60, 12 * 60),
        "cooking_9": (6 * 60 + 30, 14 * 60 + 30),
        "cooking_3": (12 * 60, 19 * 60),
        "cooking_1": (6 * 60, 8 * 60),
    }
    m = _bf_off_replacement_map(list(ranges), ranges)

    assert m["cooking_4"] == "cooking_2", "④6-13 は ②8-13 になる"
    assert m["cooking_7"] == "cooking_2", "⑦6-12 も 8時開始へ寄せる"
    # 8時以降開始・朝専用・該当する8時開始の種類が無いものは変換しない
    assert "cooking_3" not in m
    assert "cooking_1" not in m
    # ⑨6:30-14:30 は終了時刻が同じ種類が無いので、終了が最も近い種類へ寄せる
    assert m["cooking_9"] == "cooking_2"

    # 8:00-15:00 の種類を用意すれば ⑨6:30-14:30 はそこへ変換される（ユーザー依頼）
    ranges2 = dict(ranges, cooking_10=(8 * 60, 15 * 60))
    m2 = _bf_off_replacement_map(list(ranges2), ranges2)
    assert m2["cooking_9"] == "cooking_10"
    assert m2["cooking_4"] == "cooking_2", "終了時刻が一致する種類が最優先" 


def test_breakfast_off_day_has_no_six_oclock_start():
    """朝食なし期間の生成結果に 6:00 開始の勤務が残らない（②8-13 等に変換済み）。"""
    settings = _base_settings()
    settings["cooking_types"] = COOKING_TYPES + [
        {"code": "cooking_2", "label": "② 8:00-13:00",
         "start_time": "08:00", "end_time": "13:00"},
    ]
    settings["breakfast_off_start"] = "2026-08-01"
    settings["breakfast_off_end"] = "2026-08-31"
    shifts, _w = _run(settings)
    assert shifts is not None
    assert not [s for s in shifts if s["assignment"] in ("cooking_4", "cooking_7")], (
        "朝食なし日に 6:00 開始のシフトが残っている"
    )
