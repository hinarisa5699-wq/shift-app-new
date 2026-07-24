"""
test_new_shift_rules.py — 2026年改定ルール（変更1〜4）の検証

pytest 不要。bundle 済み venv で次のように実行できる:
    venv\\Scripts\\python.exe test_new_shift_rules.py

検証対象:
  変更1 相談員 …… 1日1名のみ・終日相談(counselor_desk_slots=[0,1,2,3])・休憩12:30
  変更2 お風呂 …… 中介助2(「中」)/外介助1(「外」)を均等ローテで自動選出
  変更3 休憩  …… 中=11:30 / 外・残り介護=12:30 / 看護師・リハ=13:00
  変更4 食事  …… 看護師・リハ=12:00-13:00 / 中介助=12:30-13:00 / 介護担当2名=12:00-12:30
"""
import datetime

from solver import _assign_care_roles, generate_shift


def _care(staff_id, codes, names=None):
    return {
        "id": staff_id,
        "qualification_codes": codes,
        "qualification_names": names or [],
        "qualification_ids": [],
    }


def _run(care_staff, assignments, day="2026-07-01"):
    """care_staff と {staff_id: assignment} から1日分の後処理結果を返す。"""
    dt = datetime.date.fromisoformat(day)
    shifts = [
        {"date": day, "staff_id": sid, "assignment": asgn}
        for sid, asgn in assignments.items()
    ]
    out, warns = _assign_care_roles(shifts, care_staff, {"placement_rules": []}, [dt])
    return {it["staff_id"]: it for it in out}, warns


def test_standard_floor_team():
    """介護6名(うち相談員1)+看護師1 の標準構成。"""
    care = [
        _care(1, ["care_worker"]),
        _care(2, ["care_worker"]),
        _care(3, ["care_worker"]),
        _care(4, ["care_worker"]),
        _care(5, ["care_worker"]),
        _care(6, ["counselor"], ["相談員"]),
        _care(7, ["nurse"], ["看護師"]),
    ]
    asg = {i: "day_pattern1" for i in range(1, 8)}
    res, warns = _run(care, asg)

    # 変更1: 相談員はちょうど1名・終日desk・休憩12:30
    desk = [sid for sid, it in res.items() if it.get("counselor_desk_slots")]
    assert desk == [6], f"相談員は1名のみのはず: {desk}"
    assert res[6]["counselor_desk_slots"] == [0, 1, 2, 3], "終日相談スロットのはず"
    assert res[6]["break_start"] == "12:30"
    assert not res[6].get("bath_role"), "相談員はお風呂当番から除外"

    # 変更2: 中2・外1
    mids = [sid for sid, it in res.items() if it.get("bath_role") == "中"]
    outs = [sid for sid, it in res.items() if it.get("bath_role") == "外"]
    assert len(mids) == 2, f"中介助は2名: {mids}"
    assert len(outs) == 1, f"外介助は1名: {outs}"
    assert 6 not in mids + outs, "相談員はお風呂当番に選ばれない"

    # 変更3: 休憩スキーム
    for sid in mids:
        assert res[sid]["break_start"] == "11:30", "中介助は11:30休憩"
    for sid in outs:
        assert res[sid]["break_start"] == "12:30", "外介助は12:30休憩"
    assert res[7]["break_start"] == "13:00", "看護師は13:00休憩"
    # 残りの介護職員(相談員含む)は12:30
    others = [s for s in range(1, 7) if s not in mids + outs]
    for sid in others:
        assert res[sid]["break_start"] == "12:30", f"残り介護は12:30: {sid}"

    # 変更4: 食事介助
    assert res[7]["meal_assist"] == "12:00-13:00", "看護師は通し食事介助"
    for sid in mids:
        assert res[sid]["meal_assist"] == "12:30-13:00", "中介助は休憩明けに後半カバー"
    early = [sid for sid, it in res.items() if it.get("meal_assist") == "12:00-12:30"]
    assert len(early) == 2, f"介護の食事介助担当は2名: {early}"
    assert 6 not in early, "相談員は食事介助担当に選ばれない"
    assert all(s not in mids for s in early), "中介助は前半担当に重複しない"
    assert not warns, f"標準構成では警告なし: {warns}"
    print("OK test_standard_floor_team")


def test_counselor_rotates_across_days():
    """相談員資格が2名いる日は、日替わりで均等に1名ずつ選ばれる。"""
    care = [_care(1, ["counselor"], ["相談員"]), _care(2, ["counselor"], ["相談員"]),
            _care(3, ["care_worker"]), _care(4, ["care_worker"]), _care(5, ["care_worker"])]
    days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]
    dts = [datetime.date.fromisoformat(d) for d in days]
    shifts = [
        {"date": d, "staff_id": sid, "assignment": "day_pattern1"}
        for d in days for sid in range(1, 6)
    ]
    out, _ = _assign_care_roles(shifts, care, {"placement_rules": []}, dts)
    chosen = {}
    for it in out:
        if it.get("counselor_desk_slots"):
            chosen[it["date"]] = it["staff_id"]
    assert len(chosen) == 4, "毎日1名は相談員が立つ"
    counts = {1: 0, 2: 0}
    for sid in chosen.values():
        counts[sid] += 1
    assert counts[1] == 2 and counts[2] == 2, f"2名で均等に分担: {counts}"
    print("OK test_counselor_rotates_across_days")


def test_short_team_warns_bath():
    """お風呂当番(中介助/外介助)に満たない日は警告を出す（落ちない）。依頼文40で役割別に分割。"""
    care = [_care(1, ["care_worker"]), _care(2, ["care_worker"])]
    res, warns = _run(care, {1: "day_pattern1", 2: "day_pattern1"})
    assert any(
        w["warning_type"] in ("bath_mid_short", "bath_out_short") for w in warns
    ), warns
    # それでも休憩は割り当たる
    assert res[1]["break_start"] in ("11:30", "12:30")
    print("OK test_short_team_warns_bath")


def test_half_day_and_dual_breaks_preserved():
    """半日は休憩なし、兼務は従来どおり12:30固定。"""
    care = [
        _care(1, ["care_worker"]), _care(2, ["care_worker"]), _care(3, ["care_worker"]),
        _care(10, ["care_worker"]),  # 兼務
        _care(11, ["care_worker"]),  # 半日午前
    ]
    res, _ = _run(care, {
        1: "day_pattern1", 2: "day_pattern1", 3: "day_pattern1",
        10: "day_p3_visit_pm", 11: "day_pattern3",
    })
    assert res[10]["break_start"] == "12:30", "兼務は12:30固定"
    assert not res[11].get("break_start"), "半日午前は休憩なし"
    assert not res[11].get("bath_role"), "半日はお風呂当番対象外"
    print("OK test_half_day_and_dual_breaks_preserved")


def _solver_staff(staff_id, can_visit=True):
    return {
        "id": staff_id, "name": f"s{staff_id}", "employment_type": "常勤",
        "can_visit": can_visit, "max_consecutive_days": 7, "max_days_per_week": 7,
        "min_days_per_week": 0, "available_days": [0, 1, 2, 3, 4, 5, 6],
        "available_time_slots": "full_day", "fixed_days_off": [], "staff_group": "care",
        "gender": "female", "has_phone_duty": False,
        "qualification_ids": [], "qualification_names": [], "qualification_codes": ["care_worker"],
        "weekend_constraint": "", "holiday_ng": False,
    }


def test_visit_is_always_dual_and_exactly_one(month=7):
    """変更5: 訪問営業日は訪問午前・午後がちょうど1名ずつ、純粋訪問は出現しない。

    訪問午前の1名は「早番(7:30-16:30 = AM訪問＋PMデイ)」か「兼務B(visit_am_day_p4)」
    のいずれか（2026-07・ユーザー確認）。早番が置かれた日は兼務Bは0になる。
    """
    care = [_solver_staff(i, can_visit=True) for i in range(1, 9)]
    settings = {
        "min_day_service": 3, "min_visit_am": 1, "min_visit_pm": 1,
        "min_dual_assignment": 2, "closed_days": [], "visit_operating_days": [0, 1, 3, 4],
        "min_cooking_staff": 0, "min_cooking_overlap": 0, "placement_rules": [],
        "cooking_combo_rules": [], "min_staff_at_9": 3, "min_staff_at_15": 3,
        "max_day_service": 0,
    }
    shifts, _warns = generate_shift(2026, month, care, [], [], settings)

    by_date = {}
    for s in shifts:
        by_date.setdefault(s["date"], []).append(s)

    visit_op = {0, 1, 3, 4}
    checked = 0
    for d, items in by_date.items():
        wd = datetime.date.fromisoformat(d).weekday()
        assert not any(it["assignment"] in ("visit_am", "visit_pm") for it in items), \
            f"純粋訪問が出現: {d}"
        am = sum(1 for it in items if it["assignment"] == "visit_am_day_p4")
        pm = sum(1 for it in items if it["assignment"] == "day_p3_visit_pm")
        early = sum(1 for it in items if it["assignment"] == "early")
        if wd in visit_op:
            checked += 1
            assert am + early == 1, \
                f"{d}: 訪問午前は早番か兼務Bで合計1名のはず (兼務B={am}/早番={early})"
            assert pm == 1, f"{d}: PM訪問兼務は1名のはず (={pm})"
            for it in items:
                # visit_am_day_p4(午前訪問→午後デイ)は従来どおり休憩12:30。
                if it["assignment"] == "visit_am_day_p4":
                    assert it["break_start"] == "12:30", f"{d}: 訪問担当の休憩は12:30"
                # day_p3_visit_pm(午前デイ→午後訪問)はお風呂当番(中介助)候補に含めたため、
                # 中介助に選ばれた日は11:30休憩（休憩明け食事介助12:30-13:00）になり得る。
                # それ以外は従来どおり12:30休憩。
                elif it["assignment"] == "day_p3_visit_pm":
                    assert it["break_start"] in ("11:30", "12:30"), \
                        f"{d}: 訪問兼務(午前デイ午後訪問)の休憩は11:30か12:30"
        else:
            assert am == 0 and pm == 0, f"{d}: 非営業日に訪問が出現"
    assert checked > 0, "営業日が検査されていない"
    print(f"OK test_visit_is_always_dual_and_exactly_one (営業日{checked}日検査)")


if __name__ == "__main__":
    test_standard_floor_team()
    test_counselor_rotates_across_days()
    test_short_team_warns_bath()
    test_half_day_and_dual_breaks_preserved()
    test_visit_is_always_dual_and_exactly_one()
    print("\nすべて成功しました。")
