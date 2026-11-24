"""2026-11-24 ユーザー依頼「調理は朝開始は全員7時から」。"""
from solver import _seven_start_replacement_map


def test_six_oclock_codes_map_to_seven_with_same_end():
    ranges = {
        "c4": (6 * 60, 13 * 60), "c14": (7 * 60, 13 * 60),
        "c7": (6 * 60, 12 * 60), "c712": (7 * 60, 12 * 60),
        "c630": (6 * 60 + 30, 14 * 60 + 30), "c7143": (7 * 60, 14 * 60 + 30),
        "c1": (6 * 60, 8 * 60), "c21": (7 * 60, 8 * 60),
        "c3": (12 * 60, 19 * 60), "c_none": (6 * 60, 17 * 60 + 15),
    }
    m = _seven_start_replacement_map(list(ranges), ranges)
    assert m == {"c4": "c14", "c7": "c712", "c630": "c7143", "c1": "c21"}
    # 7:00 開始の同じ終了時刻が無いものはそのまま、7時以降開始もそのまま
    assert "c_none" not in m and "c3" not in m
