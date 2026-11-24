"""2026-11-24 ユーザー依頼「休憩は2人ずつ」「14時までに行くように」。"""
from solver import limit_simultaneous_breaks


def _it(a, b, **kw):
    d = {"assignment": a, "break_start": b}
    d.update(kw)
    return d


def test_at_most_two_on_break_and_start_by_14():
    items = [_it("late", "12:30"), _it("day_pattern1", "12:30"),
             _it("day_pattern1", "12:30"), _it("early", "12:30"),
             _it("day_pattern2", "13:00")]
    limit_simultaneous_breaks(items)
    starts = sorted(i["break_start"] for i in items)
    for t in range(11 * 60, 15 * 60, 15):
        on = 0
        for s in starts:
            h, m = map(int, s.split(":"))
            if h * 60 + m <= t < h * 60 + m + 60:
                on += 1
        assert on <= 2, (t, starts)
    assert all(s <= "14:00" for s in starts), starts


def test_fixed_breaks_not_moved_and_meal_assist_respected():
    items = [_it("day_pattern1", "11:30", bath_role="中"),
             _it("visit_am_day_p4", "12:30"),
             _it("day_pattern1", "12:30", meal_assist="12:00-12:30"),
             _it("late", "12:30")]
    limit_simultaneous_breaks(items)
    assert items[0]["break_start"] == "11:30"
    assert items[1]["break_start"] == "12:30"
    assert items[2]["break_start"] not in ("11:30", "12:00")


def test_cooking_ignored():
    items = [_it("cooking_3", "12:30")] * 3 + [_it("late", "12:30")]
    assert limit_simultaneous_breaks(items) == 0


def test_care_counted_nurse_included():
    items = [_it("day_pattern2", "13:00", meal_assist="12:00-13:00", staff_id=12),
             _it("late", "12:30", staff_id=1), _it("day_pattern1", "12:30", staff_id=2)]
    limit_simultaneous_breaks(items, care_nurse_ids={12})
    assert items[0]["break_start"] == "13:00"
    starts = [i["break_start"] for i in items]
    for t in range(12 * 60, 15 * 60, 15):
        on = sum(1 for s in starts
                 if int(s[:2]) * 60 + int(s[3:]) <= t < int(s[:2]) * 60 + int(s[3:]) + 60)
        assert on <= 2, starts


def test_early_takes_break_first():
    """2026-11-24 依頼「早番1番で休憩」。"""
    items = [_it("late", "12:30"), _it("early", "12:30"), _it("day_pattern1", "12:30")]
    limit_simultaneous_breaks(items)
    assert items[1]["break_start"] == "11:30"
    # 中介助2人が11:30なら、早番は重ならない一番早い時刻
    items = [_it("day_pattern1", "11:30", bath_role="中"), _it("late", "11:30", bath_role="中"),
             _it("early", "12:30"), _it("day_pattern1", "12:30")]
    limit_simultaneous_breaks(items)
    assert items[2]["break_start"] == "12:30"
    # 食事介助(12:00-12:30)の早番は12:30
    items = [_it("early", "12:30", meal_assist="12:00-12:30"), _it("late", "12:30")]
    limit_simultaneous_breaks(items)
    assert items[0]["break_start"] == "12:30"


def test_early_without_break_gets_one():
    items = [{"assignment": "early", "break_start": None}, _it("late", "12:30")]
    limit_simultaneous_breaks(items)
    assert items[0]["break_start"] == "11:30"
