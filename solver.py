"""
solver.py -- 介護・調理シフト自動作成ソルバーエンジン
OR-Tools CP-SAT を使用した制約充足・最適化ソルバー

介護職員と調理職員を独立した2つのソルバーで処理し、結果をマージして返す。
"""

import calendar
import datetime
import json
import jpholiday
from ortools.sat.python import cp_model


# ===========================================================================
# 介護職員向け アサインメント定数
# ===========================================================================
CARE_ASSIGNMENTS = [
    "off",               # 休み
    "day_pattern1",      # デイ① 8:30-17:30
    "day_pattern2",      # デイ② 9:00-16:00
    "day_pattern3",      # デイ③ 8:30-12:30（午前半日）
    "day_pattern4",      # デイ④ 13:30-17:30（午後半日）
    "visit_am",          # 訪問介護午前
    "visit_pm",          # 訪問介護午後
    "day_p3_visit_pm",   # ③デイ+PM訪問（兼務パターンA）
    "visit_am_day_p4",   # AM訪問+④デイ（兼務パターンB）
]

CARE_WORKING_ASSIGNMENTS = [a for a in CARE_ASSIGNMENTS if a != "off"]

# 訪問系アサインメント（can_visit=False の職員は不可）
VISIT_ASSIGNMENTS = {"visit_am", "visit_pm", "day_p3_visit_pm", "visit_am_day_p4"}

# 兼務パターン
DUAL_ASSIGNMENTS = {"day_p3_visit_pm", "visit_am_day_p4"}

# 各カテゴリに寄与するアサインメントの集合
DAY_AM_ASSIGNMENTS = {"day_pattern1", "day_pattern2", "day_pattern3", "day_p3_visit_pm"}
DAY_PM_ASSIGNMENTS = {"day_pattern1", "day_pattern2", "day_pattern4", "visit_am_day_p4"}
VISIT_AM_ASSIGNMENTS = {"visit_am", "visit_am_day_p4"}
VISIT_PM_ASSIGNMENTS = {"visit_pm", "day_p3_visit_pm"}

# 9時在籍（事業所に物理的にいる人。訪問外出中は含まない）
PRESENT_AT_9 = {
    "day_pattern1", "day_pattern2", "day_pattern3",
    "day_p3_visit_pm",     # AM事業所→PM訪問、9時は事業所にいる
}

# 11時在籍（午前配置 + フルタイム + 午前兼務）
PRESENT_AT_11 = {
    "day_pattern1", "day_pattern2", "day_pattern3",
    "day_p3_visit_pm",
}

# 13時在籍（昼食介助帯。午後半日/午後兼務は13:30開始のため含めない）
PRESENT_AT_13 = {
    "day_pattern1", "day_pattern2",
}

# 15時在籍（事業所に物理的にいる人。訪問外出中は含まない）
PRESENT_AT_15 = {
    "day_pattern1", "day_pattern2", "day_pattern4",
    "visit_am_day_p4",     # AM訪問→PM事業所、15時は事業所にいる
}

# 時間帯制限: am_only の職員が取れないアサインメント（午後を含む全パターン）
AM_ONLY_FORBIDDEN = {
    "day_pattern1", "day_pattern2", "day_pattern4", "visit_pm",
    "day_p3_visit_pm", "visit_am_day_p4",
}
# 時間帯制限: pm_only の職員が取れないアサインメント（午前を含む全パターン）
PM_ONLY_FORBIDDEN = {
    "day_pattern1", "day_pattern2", "day_pattern3", "visit_am",
    "visit_am_day_p4", "day_p3_visit_pm",
}

# 事業所にいるアサインメント（電話当番可能 = デイ系のみ）
DAY_SERVICE_ASSIGNMENTS = {
    "day_pattern1", "day_pattern2", "day_pattern3", "day_pattern4",
}
DAY_PATTERN_ASSIGNMENTS = set(DAY_SERVICE_ASSIGNMENTS)

# 全日在籍（9時〜16時を通して事業所にいるパターン）
# 半日パターン・兼務パターンは含まない
PRESENT_FULL_DAY = PRESENT_AT_9 & PRESENT_AT_15
# = {"day_pattern1", "day_pattern2"}

_NURSE_QUAL_CODES = {"nurse"}
_NURSE_QUAL_NAMES = {"看護師"}
_NURSE_PT_QUAL_CODES = {"nurse", "pt"}
_NURSE_PT_QUAL_NAMES = {"看護師", "PT", "理学療法士"}


# ===========================================================================
# 調理職員向け アサインメント定数
# ===========================================================================
COOK_ASSIGNMENTS = [
    "cook_off",       # 休み
    "cook_early",     # ① 6:00-8:00
    "cook_morning",   # ② 8:00-12:00
    "cook_late",      # ③ 13:00-19:00
    "cook_long",      # ④ 6:00-12:00
]

COOK_WORKING_ASSIGNMENTS = [a for a in COOK_ASSIGNMENTS if a != "cook_off"]

# 時間帯カバレッジマップ
# intervals: [6-8), [8-12), [13-19)
COOK_COVERAGE = {
    "cook_early":   (1, 0, 0),     # ① [6-8)
    "cook_morning": (0, 1, 0),     # ② [8-12)
    "cook_late":    (0, 0, 1),     # ③ [13-19)
    "cook_long":    (1, 1, 0),     # ④ [6-8), [8-12)
}


def _staff_has_any_qualification(
    staff: dict,
    *,
    codes: set[str],
    names: set[str],
) -> bool:
    """資格コード優先で判定し、旧DBの名称差分も補完する。"""
    qual_codes = {
        code for code in staff.get("qualification_codes", [])
        if isinstance(code, str) and code
    }
    if qual_codes.intersection(codes):
        return True

    qual_names = {
        name for name in staff.get("qualification_names", [])
        if isinstance(name, str) and name
    }
    return bool(qual_names.intersection(names))


def _get_counselor_qualification_ids(placement_rules: list[dict]) -> set[int]:
    """配置ルールから相談員資格IDを特定する。"""
    counselor_qual_ids: set[int] = set()
    for rule in placement_rules:
        rule_name = rule.get("name", "")
        if "相談" in rule_name or "counselor" in rule_name.lower():
            counselor_qual_ids.update(rule.get("target_qualification_ids", []))
    return counselor_qual_ids


# ===========================================================================
# メインエントリーポイント
# ===========================================================================
def generate_shift(
    year: int,
    month: int,
    care_staff: list,
    cook_staff: list,
    day_off_requests: list,
    settings: dict,
    allowed_patterns: dict = None,
) -> tuple[list, list]:
    """
    シフトを自動生成する。

    介護職員と調理職員を独立したソルバーで処理し、結果をマージして返す。

    allowed_patterns: {staff_id: set(assignment_codes)} or None
        エントリがある職員 → そのアサインメントのみ許可
        エントリがない職員 → 全アサインメント許可
    """
    if allowed_patterns is None:
        allowed_patterns = {}

    num_days = calendar.monthrange(year, month)[1]
    all_dates = [datetime.date(year, month, d) for d in range(1, num_days + 1)]

    # --- 介護ソルバー ---
    care_shifts, care_warnings = _solve_care_with_fallback(
        year, month, all_dates, care_staff, day_off_requests, settings,
        allowed_patterns=allowed_patterns,
    )

    # --- ① 休憩時間ずらし後処理 ---
    # 2026-03-02 23:24 要件: 看護師は休憩11:00固定
    fixed_break_by_staff = {}
    for s in care_staff:
        if _staff_has_any_qualification(
            s,
            codes=_NURSE_QUAL_CODES,
            names=_NURSE_QUAL_NAMES,
        ):
            fixed_break_by_staff[s["id"]] = "11:00"
    care_shifts = _assign_break_times(
        care_shifts,
        all_dates,
        fixed_break_by_staff=fixed_break_by_staff,
    )

    # --- ③ 相談員ローテーション後処理（設定で有効時のみ）---
    if settings.get("counselor_desk_enabled", False):
        care_shifts, counselor_warnings = _assign_counselor_rotation(
            care_shifts, care_staff, settings, all_dates
        )
        care_warnings.extend(counselor_warnings)

        # --- 後処理バリデーション: 休憩・相談除外後の現場人数チェック ---
        nurse_pt_sids = set()
        for s in care_staff:
            if _staff_has_any_qualification(
                s,
                codes=_NURSE_PT_QUAL_CODES,
                names=_NURSE_PT_QUAL_NAMES,
            ):
                nurse_pt_sids.add(s["id"])
        care_shifts = _repair_breaks_for_onsite_staffing(
            care_shifts,
            all_dates,
            min_required=settings.get("min_day_service", 4),
            nurse_pt_staff_ids=nurse_pt_sids,
        )
        onsite_warnings = _validate_onsite_staffing(
            care_shifts, all_dates,
            min_required=settings.get("min_day_service", 4),  # バッファなしの元の値
            nurse_pt_staff_ids=nurse_pt_sids,
        )
        care_warnings.extend(onsite_warnings)

    # --- 調理ソルバー ---
    cook_shifts, cook_warnings = _solve_cooking_with_fallback(
        year, month, all_dates, cook_staff, day_off_requests, settings,
        allowed_patterns=allowed_patterns,
    )

    # --- ① 調理の休憩時間（固定のみ）---
    cook_shifts = _assign_break_times(cook_shifts, all_dates)

    return care_shifts + cook_shifts, care_warnings + cook_warnings


# ===========================================================================
# ① 休憩時間ずらし（後処理）
# ===========================================================================
# 固定休憩時間（兼務パターン・調理通しは休憩時刻が決まっている）
_FIXED_BREAK = {
    "day_p3_visit_pm": "12:30",  # 兼務A: 12:30-13:30
    "visit_am_day_p4": "12:30",  # 兼務B: 12:30-13:30
    "cook_long":       "08:00",  # 調理通し: 8:00-9:00
}

# ずらし対象パターン（フルタイム勤務）
_STAGGER_PATTERNS = {"day_pattern1", "day_pattern2"}

# 利用可能な休憩スロット（開始時刻）
# 各スロット60分間隔で重複なし。7スロットで最大7名を個別に分散。
# 相談員ローテーション(常時1名)と合わせても同時に2名までしか現場を離れない。
_BREAK_SLOTS = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"]

# パターン別の許可スロット（退勤時刻を超える休憩を防止）
_ALLOWED_BREAK_SLOTS = {
    "day_pattern1": _BREAK_SLOTS,                        # 8:30-17:30 → 全スロットOK
    "day_pattern2": [s for s in _BREAK_SLOTS if s < "15:00"],  # 9:00-16:00 → 14:00まで
}


def _to_minutes(hhmm: str) -> int:
    """HH:MM -> 分"""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _break_overlaps_slot(break_start: str, slot_idx: int) -> bool:
    """休憩(1時間)が相談スロット(2時間)と重なるか。"""
    if not break_start:
        return False
    slot_windows = [
        (_to_minutes("9:00"), _to_minutes("11:00")),
        (_to_minutes("11:00"), _to_minutes("13:00")),
        (_to_minutes("13:00"), _to_minutes("15:00")),
        (_to_minutes("15:00"), _to_minutes("17:00")),
    ]
    break_start_min = _to_minutes(break_start)
    break_end_min = break_start_min + 60
    slot_start_min, slot_end_min = slot_windows[slot_idx]
    return max(break_start_min, slot_start_min) < min(break_end_min, slot_end_min)


def _assign_break_times(shifts_data, all_dates, fixed_break_by_staff=None):
    """各シフトに個人別の休憩開始時刻を割り当てる。

    - day_pattern1/day_pattern2: 7スロットにずらして割り当て（パターン別に許可スロットをフィルタ）
    - 兼務・調理通し: 固定時刻
    - 半日パターン(day_pattern3/4)・訪問のみ: 休憩なし
    """
    if fixed_break_by_staff is None:
        fixed_break_by_staff = {}

    # 日付別にグルーピング
    date_items = {}
    for item in shifts_data:
        date_items.setdefault(item["date"], []).append(item)

    # 月間の各スロット別割り当て回数（公平性用）
    # {staff_id: {slot: count}}
    staff_slot_history = {}
    day_counter = 0

    for dt in all_dates:
        d_str = dt.strftime("%Y-%m-%d")
        day_items = date_items.get(d_str, [])

        # 1. 固定休憩を割り当て
        daily_slot_usage = {slot: 0 for slot in _BREAK_SLOTS}
        for item in day_items:
            sid = item.get("staff_id")
            forced = fixed_break_by_staff.get(sid)
            if forced and item["assignment"] in _STAGGER_PATTERNS:
                item["break_start"] = forced
            elif item["assignment"] in _FIXED_BREAK:
                item["break_start"] = _FIXED_BREAK[item["assignment"]]
            if item.get("break_start") in daily_slot_usage:
                daily_slot_usage[item["break_start"]] += 1

        # 2. ずらし対象を収集
        stagger_items = [
            item for item in day_items
            if item["assignment"] in _STAGGER_PATTERNS and not item.get("break_start")
        ]
        if not stagger_items:
            continue

        all_slots = list(_BREAK_SLOTS)
        available_slots = [slot for slot in all_slots if daily_slot_usage.get(slot, 0) == 0]
        if not available_slots:
            available_slots = list(all_slots)
        n_slots = len(available_slots)

        # スロット開始位置を日ごとにローテーション
        slot_offset = day_counter % n_slots
        day_counter += 1

        # 各スロットに対して、そのスロットの割り当て回数が最少の人を選択
        assigned_indices = set()
        slot_assignment = {}  # item_idx -> slot
        ordered_slots = [
            available_slots[(slot_offset + i) % n_slots]
            for i in range(n_slots)
        ]

        for slot in ordered_slots:
            # まだ割り当てられていない人の中で、このスロットの回数が最少の人を選択
            best_idx = None
            best_count = float('inf')
            for j, item in enumerate(stagger_items):
                if j in assigned_indices:
                    continue
                # パターン別の許可スロットチェック
                allowed = _ALLOWED_BREAK_SLOTS.get(item["assignment"], _BREAK_SLOTS)
                if slot not in allowed:
                    continue
                sid = item["staff_id"]
                count = staff_slot_history.get(sid, {}).get(slot, 0)
                if count < best_count:
                    best_count = count
                    best_idx = j
            if best_idx is not None:
                assigned_indices.add(best_idx)
                slot_assignment[best_idx] = slot
                daily_slot_usage[slot] = daily_slot_usage.get(slot, 0) + 1

        # 溢れた人は、その日の重複数が最小になるスロットへ割り当て
        for j, item in enumerate(stagger_items):
            if j not in assigned_indices:
                sid = item["staff_id"]
                # パターン別の許可スロットのみから選択
                allowed = _ALLOWED_BREAK_SLOTS.get(item["assignment"], _BREAK_SLOTS)
                slot = min(
                    allowed,
                    key=lambda s: (
                        daily_slot_usage.get(s, 0),
                        staff_slot_history.get(sid, {}).get(s, 0),
                    ),
                )
                slot_assignment[j] = slot
                assigned_indices.add(j)
                daily_slot_usage[slot] = daily_slot_usage.get(slot, 0) + 1

        # 割り当てを適用 & 履歴更新
        for j, item in enumerate(stagger_items):
            slot = slot_assignment[j]
            item["break_start"] = slot
            sid = item["staff_id"]
            if sid not in staff_slot_history:
                staff_slot_history[sid] = {}
            staff_slot_history[sid][slot] = staff_slot_history[sid].get(slot, 0) + 1

    return shifts_data


# ===========================================================================
# ③ 相談員2時間ローテーション（後処理）
# ===========================================================================
COUNSELOR_DESK_SLOTS = ["9:00-11:00", "11:00-13:00", "13:00-15:00", "15:00-17:00"]
_ONSITE_CHECK_POINTS = [
    ("9:00", 540),
    ("9:30", 570),
    ("10:00", 600),
    ("10:30", 630),
    ("11:00", 660),
    ("11:30", 690),
    ("12:00", 720),
    ("12:30", 750),
    ("13:00", 780),
    ("13:30", 810),
    ("14:00", 840),
    ("14:30", 870),
    ("15:00", 900),
    ("15:30", 930),
    ("16:00", 960),
]

# 各シフトパターンがカバーする事務スロットインデックス
_SLOT_COVERAGE = {
    "day_pattern1":    [0, 1, 2, 3],  # 8:30-17:30 → 全スロット可
    "day_pattern2":    [0, 1, 2],      # 9:00-16:00 → 15-17は16時退勤のため不可
    "day_pattern3":    [0, 1],         # 8:30-12:30 → 午前のみ
    "day_pattern4":    [2, 3],         # 13:30-17:30 → 午後のみ
    "day_p3_visit_pm": [0],            # ③→訪問: 施設8:30-12:30 → 9-11のみ
    "visit_am_day_p4": [3],            # 訪問→④: 施設13:30-17:30 → 15-17のみ
}


def _assign_counselor_rotation(shifts_data, care_staff, settings, all_dates):
    """相談員の2時間事務ローテーションを割り当てる。
    Returns: (shifts_data, warnings)
    """
    counselor_warnings = []
    placement_rules = settings.get("placement_rules", [])

    # 相談員の資格IDを特定
    counselor_qual_ids = _get_counselor_qualification_ids(placement_rules)

    if not counselor_qual_ids:
        return shifts_data, []

    # 相談員資格を持つ職員IDセット
    counselor_staff_ids = set()
    for s in care_staff:
        if set(s.get("qualification_ids", [])) & counselor_qual_ids:
            counselor_staff_ids.add(s["id"])

    if not counselor_staff_ids:
        return shifts_data, []

    # 日付別に出勤中の相談員をマップ
    date_staff_assignment = {}  # {date_str: {staff_id: assignment}}
    for item in shifts_data:
        d_str = item["date"]
        if d_str not in date_staff_assignment:
            date_staff_assignment[d_str] = {}
        date_staff_assignment[d_str][item["staff_id"]] = item["assignment"]

    date_staff_break = {}
    for item in shifts_data:
        bs = item.get("break_start")
        if not bs:
            continue
        d_str = item["date"]
        if d_str not in date_staff_break:
            date_staff_break[d_str] = {}
        date_staff_break[d_str][item["staff_id"]] = bs

    # 月間の事務回数カウンタ（公平性用）
    desk_count = {sid: 0 for sid in counselor_staff_ids}

    daily_target = len(COUNSELOR_DESK_SLOTS)
    slot_use_count = {i: 0 for i in range(len(COUNSELOR_DESK_SLOTS))}

    for dt in all_dates:
        d_str = dt.strftime("%Y-%m-%d")
        day_assignments = date_staff_assignment.get(d_str, {})

        # その日出勤中の相談員を抽出
        working_counselors = []
        for sid in counselor_staff_ids:
            asgn = day_assignments.get(sid)
            if asgn and asgn != "off" and asgn in _SLOT_COVERAGE:
                working_counselors.append((sid, asgn))

        if not working_counselors:
            continue

        # --- 2段階で全4スロットを埋める ---
        slot_assignments = {}  # {staff_id: [slot_idx, ...]}
        used_slots = set()

        # 各相談員の担当可能スロットを事前計算（休憩と重ならないもの）
        counselor_possible = {}  # {sid: [slot_idx, ...]}
        for sid, asgn in working_counselors:
            break_start = date_staff_break.get(d_str, {}).get(sid, "")
            possible = [
                idx for idx in _SLOT_COVERAGE.get(asgn, [])
                if not _break_overlaps_slot(break_start, idx)
            ]
            if possible:
                counselor_possible[sid] = possible

        if not counselor_possible:
            continue

        # Phase 1: 各相談員に1スロットずつ分散割当（公平性）
        assigned_phase1 = set()
        for _ in range(min(daily_target, len(counselor_possible))):
            candidates = []
            for sid, possible in counselor_possible.items():
                if sid in assigned_phase1:
                    continue
                available = [s for s in possible if s not in used_slots]
                if not available:
                    continue
                candidates.append((sid, available))

            if not candidates:
                break

            candidates.sort(key=lambda c: (len(c[1]), desk_count.get(c[0], 0), c[0]))
            chosen_sid, avail_slots = candidates[0]
            chosen_slot = min(avail_slots, key=lambda s: (slot_use_count.get(s, 0), s))

            slot_assignments.setdefault(chosen_sid, []).append(chosen_slot)
            assigned_phase1.add(chosen_sid)
            used_slots.add(chosen_slot)
            desk_count[chosen_sid] = desk_count.get(chosen_sid, 0) + 1
            slot_use_count[chosen_slot] = slot_use_count.get(chosen_slot, 0) + 1

        # Phase 2: 未割当スロットを既存の相談員に追加割当
        remaining_slots = [i for i in range(daily_target) if i not in used_slots]
        for slot_idx in remaining_slots:
            candidates = []
            for sid, possible in counselor_possible.items():
                if slot_idx not in possible:
                    continue
                # 既に持っているスロットとの連続を避ける（可能な限り）
                already = slot_assignments.get(sid, [])
                is_adjacent = any(abs(slot_idx - a) == 1 for a in already)
                candidates.append((sid, is_adjacent, len(already), desk_count.get(sid, 0)))

            if not candidates:
                continue

            # 連続でない人 → 担当数が少ない人 → 月間回数が少ない人
            candidates.sort(key=lambda c: (c[1], c[2], c[3], c[0]))
            chosen_sid = candidates[0][0]

            slot_assignments.setdefault(chosen_sid, []).append(slot_idx)
            used_slots.add(slot_idx)
            desk_count[chosen_sid] = desk_count.get(chosen_sid, 0) + 1
            slot_use_count[slot_idx] = slot_use_count.get(slot_idx, 0) + 1

        # Phase 3: まだ未割当スロットがあれば、相談員の休憩を非相談員とスワップして埋める
        still_remaining = [i for i in range(daily_target) if i not in used_slots]
        if still_remaining:
            # この日の全スタッフの break_start を取得
            day_breaks = date_staff_break.get(d_str, {})
            counselor_sids_today = {sid for sid, _ in working_counselors}
            # 非相談員のスタッフ(同日出勤中、ずらし対象パターン)を取得
            non_counselor_breaks = {}
            for item in shifts_data:
                if item["date"] == d_str and item["staff_id"] not in counselor_sids_today:
                    asgn = item.get("assignment", "")
                    if asgn in _STAGGER_PATTERNS and item.get("break_start"):
                        non_counselor_breaks[item["staff_id"]] = {
                            "break_start": item["break_start"],
                            "assignment": asgn,
                        }

            for slot_idx in list(still_remaining):
                # このスロットを取れる休憩時間を探す
                good_breaks = []
                for b in _BREAK_SLOTS:
                    if not _break_overlaps_slot(b, slot_idx):
                        good_breaks.append(b)

                swapped = False
                for sid in counselor_sids_today:
                    if swapped:
                        break
                    c_break = day_breaks.get(sid, "")
                    if not c_break or not _break_overlaps_slot(c_break, slot_idx):
                        continue  # この相談員の休憩はスロットと被っていない（別の理由で割当不可）
                    # この相談員の休憩をスワップすればスロット取れる
                    for nc_sid, nc_info in non_counselor_breaks.items():
                        nc_break = nc_info["break_start"]
                        nc_assignment = nc_info["assignment"]
                        allowed_for_non_counselor = _ALLOWED_BREAK_SLOTS.get(nc_assignment, _BREAK_SLOTS)
                        if (
                            nc_break in good_breaks
                            and c_break != nc_break
                            and c_break in allowed_for_non_counselor
                        ):
                            # 既存の割当スロットが新しい休憩と衝突しないか確認
                            existing = slot_assignments.get(sid, [])
                            if any(_break_overlaps_slot(nc_break, es) for es in existing):
                                continue
                            # スワップ: 相談員に good_break, 非相談員に相談員の元の休憩
                            # shifts_data 内の break_start を更新
                            for item in shifts_data:
                                if item["date"] == d_str:
                                    if item["staff_id"] == sid:
                                        item["break_start"] = nc_break
                                    elif item["staff_id"] == nc_sid:
                                        item["break_start"] = c_break
                            # マップも更新
                            day_breaks[sid] = nc_break
                            day_breaks[nc_sid] = c_break
                            non_counselor_breaks[nc_sid]["break_start"] = c_break
                            # counselor_possible を再計算
                            asgn_for_sid = day_assignments.get(sid, "")
                            counselor_possible[sid] = [
                                idx for idx in _SLOT_COVERAGE.get(asgn_for_sid, [])
                                if not _break_overlaps_slot(nc_break, idx)
                            ]
                            # スロット割当
                            if slot_idx in counselor_possible.get(sid, []):
                                slot_assignments.setdefault(sid, []).append(slot_idx)
                                used_slots.add(slot_idx)
                                desk_count[sid] = desk_count.get(sid, 0) + 1
                                slot_use_count[slot_idx] = slot_use_count.get(slot_idx, 0) + 1
                                swapped = True
                            break

        # 未充足スロットの警告を生成
        unfilled = [i for i in range(daily_target) if i not in used_slots]
        if unfilled:
            unfilled_names = [COUNSELOR_DESK_SLOTS[i] for i in unfilled]
            counselor_warnings.append({
                "date": d_str,
                "warning_type": "counselor_slot_unfilled",
                "message": f"相談スロット未充足: {', '.join(unfilled_names)}"
                           f"（出勤相談員{len(working_counselors)}名）",
            })

        # shifts_data の対応エントリに counselor_desk_slots を付与
        if slot_assignments:
            for item in shifts_data:
                if item["date"] == d_str and item["staff_id"] in slot_assignments:
                    item["counselor_desk_slots"] = slot_assignments[item["staff_id"]]

    return shifts_data, counselor_warnings


# ===========================================================================
# 後処理バリデーション: 現場在籍人数チェック
# ===========================================================================
def _is_onsite_at(assignment, check_min):
    """指定時刻(分)に事業所にいるか（訪問外出中は含まない）"""
    _PRESENCE = {
        "day_pattern1":    (510, 1050),   # 8:30-17:30
        "day_pattern2":    (540, 960),    # 9:00-16:00
        "day_pattern3":    (510, 750),    # 8:30-12:30
        "day_pattern4":    (810, 1050),   # 13:30-17:30
        "day_p3_visit_pm": (510, 750),    # 午前のみ施設
        "visit_am_day_p4": (810, 1050),   # 午後のみ施設
    }
    rng = _PRESENCE.get(assignment)
    return rng is not None and rng[0] <= check_min < rng[1]


_COUNSELOR_SLOT_WINDOWS = [
    (540, 660), (660, 780), (780, 900), (900, 1020)  # 9-11, 11-13, 13-15, 15-17
]


def _count_effective_onsite_staff(items, check_min, nurse_pt_staff_ids):
    """指定時刻の実効現場人数を返す。"""
    count = 0
    for item in items:
        sid = item["staff_id"]
        asgn = item.get("assignment", "")
        if sid in nurse_pt_staff_ids:
            continue
        if not _is_onsite_at(asgn, check_min):
            continue
        bs = item.get("break_start", "")
        if bs:
            bs_min = _to_minutes(bs)
            if bs_min <= check_min < bs_min + 60:
                continue
        desk_slots = item.get("counselor_desk_slots", [])
        on_desk = False
        for si in desk_slots:
            sw = _COUNSELOR_SLOT_WINDOWS[si]
            if sw[0] <= check_min < sw[1]:
                on_desk = True
                break
        if on_desk:
            continue
        count += 1
    return count


def _get_daily_onsite_counts(items, nurse_pt_staff_ids):
    """日内チェックポイントごとの実効現場人数を返す。"""
    return {
        label: _count_effective_onsite_staff(items, check_min, nurse_pt_staff_ids)
        for label, check_min in _ONSITE_CHECK_POINTS
    }


def _repair_breaks_for_onsite_staffing(shifts_data, all_dates, min_required, nurse_pt_staff_ids):
    """休憩時刻を微調整し、日中の実効現場人数不足を避ける。"""
    date_items = {}
    for item in shifts_data:
        date_items.setdefault(item["date"], []).append(item)

    def _is_break_slot_valid(item, slot):
        if slot not in _ALLOWED_BREAK_SLOTS.get(item["assignment"], _BREAK_SLOTS):
            return False
        return not any(
            _break_overlaps_slot(slot, slot_idx)
            for slot_idx in item.get("counselor_desk_slots", [])
        )

    for dt in all_dates:
        d_str = dt.strftime("%Y-%m-%d")
        items = date_items.get(d_str, [])
        if not items:
            continue

        while True:
            before_counts = _get_daily_onsite_counts(items, nurse_pt_staff_ids)
            shortage_labels = [
                label for label, count in before_counts.items()
                if count < min_required
            ]
            if not shortage_labels:
                break

            target_label = min(shortage_labels, key=lambda label: before_counts[label])
            target_min = next(
                check_min for label, check_min in _ONSITE_CHECK_POINTS
                if label == target_label
            )
            used_slots = {
                item.get("break_start")
                for item in items
                if item.get("break_start") in _BREAK_SLOTS
            }

            repaired = False
            for item in items:
                current_break = item.get("break_start")
                if item.get("assignment") not in _STAGGER_PATTERNS:
                    continue
                if not current_break or current_break not in _BREAK_SLOTS:
                    continue
                if item["staff_id"] in nurse_pt_staff_ids:
                    continue
                if not _is_onsite_at(item.get("assignment", ""), target_min):
                    continue
                if any(
                    _COUNSELOR_SLOT_WINDOWS[slot_idx][0] <= target_min < _COUNSELOR_SLOT_WINDOWS[slot_idx][1]
                    for slot_idx in item.get("counselor_desk_slots", [])
                ):
                    continue

                current_break_min = _to_minutes(current_break)
                if not (current_break_min <= target_min < current_break_min + 60):
                    continue

                allowed_slots = [
                    slot for slot in _ALLOWED_BREAK_SLOTS.get(item["assignment"], _BREAK_SLOTS)
                    if slot != current_break and _is_break_slot_valid(item, slot)
                ]
                occupied_without_current = used_slots - {current_break}
                for new_slot in allowed_slots:
                    if new_slot in occupied_without_current:
                        continue

                    item["break_start"] = new_slot
                    after_counts = _get_daily_onsite_counts(items, nurse_pt_staff_ids)
                    after_shortage_labels = {
                        label for label, count in after_counts.items()
                        if count < min_required
                    }
                    if (
                        target_label not in after_shortage_labels
                        and after_shortage_labels.issubset(set(shortage_labels))
                    ):
                        used_slots.remove(current_break)
                        used_slots.add(new_slot)
                        repaired = True
                        break
                    item["break_start"] = current_break

                if repaired:
                    break

                for preferred_slot in allowed_slots:
                    donor = next(
                        (
                            other for other in items
                            if other is not item
                            and other.get("break_start") == preferred_slot
                            and other.get("assignment") in _STAGGER_PATTERNS
                        ),
                        None,
                    )
                    if donor is None:
                        continue

                    donor_current_break = donor["break_start"]
                    donor_allowed_slots = [
                        slot
                        for slot in _ALLOWED_BREAK_SLOTS.get(donor["assignment"], _BREAK_SLOTS)
                        if slot != donor_current_break and _is_break_slot_valid(donor, slot)
                    ]
                    occupied_without_pair = used_slots - {current_break, donor_current_break}
                    for donor_new_slot in donor_allowed_slots:
                        if donor_new_slot in occupied_without_pair:
                            continue

                        item["break_start"] = preferred_slot
                        donor["break_start"] = donor_new_slot
                        after_counts = _get_daily_onsite_counts(items, nurse_pt_staff_ids)
                        after_shortage_labels = {
                            label for label, count in after_counts.items()
                            if count < min_required
                        }
                        if (
                            target_label not in after_shortage_labels
                            and after_shortage_labels.issubset(set(shortage_labels))
                        ):
                            used_slots.remove(current_break)
                            used_slots.remove(donor_current_break)
                            used_slots.add(preferred_slot)
                            used_slots.add(donor_new_slot)
                            repaired = True
                            break

                        item["break_start"] = current_break
                        donor["break_start"] = donor_current_break

                    if repaired:
                        break

                if repaired:
                    break

            if not repaired:
                break

    return shifts_data


def _validate_onsite_staffing(shifts_data, all_dates, min_required, nurse_pt_staff_ids):
    """休憩・相談を除外した現場在籍人数を検証し、不足があれば警告を返す。
    1日あたり最も不足する時間帯のみを1件報告する（警告スパム防止）。
    """
    warnings = []
    date_items = {}
    for item in shifts_data:
        date_items.setdefault(item["date"], []).append(item)

    for dt in all_dates:
        d_str = dt.strftime("%Y-%m-%d")
        items = date_items.get(d_str, [])
        if not items:
            continue

        worst_count = min_required
        worst_label = ""
        for label, t_min in _ONSITE_CHECK_POINTS:
            count = _count_effective_onsite_staff(items, t_min, nurse_pt_staff_ids)
            if count < worst_count:
                worst_count = count
                worst_label = label

        if worst_count < min_required:
            shortage = min_required - worst_count
            warnings.append({
                "date": d_str,
                "warning_type": "onsite_understaffed",
                "message": f"現場人数不足: {worst_label}時点で{worst_count}名"
                           f"（必要{min_required}名、{shortage}名不足）"
                           f"※休憩中・相談中を除外",
            })

    return warnings


# ===========================================================================
# 介護職員ソルバー（フォールバック付き）
# ===========================================================================
def _solve_care_with_fallback(
    year, month, all_dates, care_staff, day_off_requests, settings,
    allowed_patterns=None,
):
    """
    介護職員のシフトを生成する。
    1. ハード制約のみで解を試みる
    2. 不可能ならスラック変数付きで再実行
    3. それでも不可能なら全員休み + 警告
    """
    if not care_staff:
        return [], []

    # 休み希望を (staff_id, date) の集合に変換
    off_request_set = set()
    for req in day_off_requests:
        d = req["date"]
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d)
        off_request_set.add((req["staff_id"], d))

    # 職員データの正規化
    staff_by_id = {}
    for s in care_staff:
        sid = s["id"]
        raw_avail = s.get("available_days", [0, 1, 2, 3, 4])
        if isinstance(raw_avail, str):
            raw_avail = [int(x) for x in raw_avail.split(",") if x.strip()]
        raw_fixed = s.get("fixed_days_off", [])
        if isinstance(raw_fixed, str):
            raw_fixed = [int(x) for x in raw_fixed.split(",") if x.strip()] if raw_fixed else []
        staff_by_id[sid] = {
            "id": sid,
            "name": s.get("name", f"Staff_{sid}"),
            "employment_type": s.get("employment_type", "常勤"),
            "can_visit": s.get("can_visit", False),
            "max_consecutive_days": s.get("max_consecutive_days", 5),
            "max_days_per_week": s.get("max_days_per_week", 5),
            "available_days": raw_avail,
            "available_time_slots": s.get("available_time_slots", "full_day"),
            "fixed_days_off": raw_fixed,
            "gender": s.get("gender", ""),
            "has_phone_duty": s.get("has_phone_duty", False),
            "qualification_ids": s.get("qualification_ids", []),
            "weekend_constraint": s.get("weekend_constraint", ""),
            "min_days_per_week": s.get("min_days_per_week", 0),
            "holiday_ng": s.get("holiday_ng", False),
        }

    staff_ids = list(staff_by_id.keys())

    # 設定値
    _base_min_day = settings.get("min_day_service", 4)
    min_visit_am = settings.get("min_visit_am", 1)
    min_visit_pm = settings.get("min_visit_pm", 1)
    min_dual = settings.get("min_dual_assignment", 2)
    closed_days_set = set(settings.get("closed_days", [5, 6]))
    visit_operating_days = settings.get("visit_operating_days", [0, 1, 3, 4])
    am_preferred_gender = settings.get("am_preferred_gender", "")
    phone_duty_enabled = settings.get("phone_duty_enabled", False)
    phone_duty_max_consecutive = settings.get("phone_duty_max_consecutive", 1)
    min_staff_at_9 = settings.get("min_staff_at_9", 4)
    min_staff_at_15 = settings.get("min_staff_at_15", 4)
    male_am_constraint_mode = settings.get("male_am_constraint_mode", "hard")
    placement_rules = settings.get("placement_rules", [])

    # 休憩・相談で現場を離れる人数分のバッファを追加
    # 要件: 「休憩中・相談中はカウントしない」(3/5クライアント指摘)
    # ケア職の休憩は同時間帯最大1名、相談員ローテは同時間帯最大1名。
    counselor_desk_enabled = settings.get("counselor_desk_enabled", False)
    _break_buffer = 1
    _counselor_buffer = 1 if counselor_desk_enabled else 0
    _midday_buffer = _break_buffer + _counselor_buffer
    counselor_qual_ids = _get_counselor_qualification_ids(placement_rules)
    counselor_staff_ids = [
        sid for sid in staff_ids
        if counselor_qual_ids.intersection(set(staff_by_id[sid].get("qualification_ids", [])))
    ] if counselor_qual_ids else []
    min_counselor_staff = 2 if counselor_desk_enabled and counselor_staff_ids else 0
    # 昼帯(11-15時)の4スロットを安定して埋めるには、
    # 端パターンだけでなくフルタイム相談員が最低2名必要になる日がある。
    min_full_day_counselor = 2 if counselor_desk_enabled and counselor_staff_ids else 0

    min_day_service = _base_min_day + _midday_buffer
    min_staff_at_9 = min_staff_at_9 + _counselor_buffer
    min_staff_at_11 = _base_min_day + _midday_buffer
    min_staff_at_13 = _base_min_day + _midday_buffer
    min_staff_at_15 = min_staff_at_15 + _midday_buffer

    # デイ出勤上限もバッファ分を加算（上限が下限を下回らないように）
    _raw_max_day = settings.get("max_day_service", 0) or 0
    if _raw_max_day > 0:
        max_day_service = _raw_max_day + _midday_buffer
    else:
        max_day_service = min_day_service

    # 電話当番対象者ゼロの事前チェック（全Phase共通）
    _phone_no_eligible_warning = None
    if phone_duty_enabled:
        _phone_eligible_check = [
            sid for sid in staff_ids
            if staff_by_id[sid].get("has_phone_duty")
            and staff_by_id[sid].get("employment_type") in ("常勤", "時短正社員", "管理者")
        ]
        if not _phone_eligible_check:
            _phone_no_eligible_warning = {
                "date": all_dates[0].strftime("%Y-%m-%d"),
                "warning_type": "phone_duty_no_eligible",
                "message": "電話当番が有効ですが、対象となる社員（常勤・時短正社員・管理者で電話当番可の職員）がいません。",
            }

    # Phase 1: ハード制約のみ
    shifts_data, warnings_data = _solve_care(
        year, month, all_dates, staff_ids, staff_by_id,
        off_request_set, min_day_service, min_visit_am, min_visit_pm,
        min_dual, closed_days_set, visit_operating_days,
        am_preferred_gender=am_preferred_gender,
        phone_duty_enabled=phone_duty_enabled,
        phone_duty_max_consecutive=phone_duty_max_consecutive,
        min_staff_at_9=min_staff_at_9,
        min_staff_at_11=min_staff_at_11,
        min_staff_at_13=min_staff_at_13,
        min_staff_at_15=min_staff_at_15,
        male_am_constraint_mode=male_am_constraint_mode,
        placement_rules=placement_rules,
        counselor_staff_ids=counselor_staff_ids,
        min_counselor_staff=min_counselor_staff,
        min_full_day_counselor=min_full_day_counselor,
        allowed_patterns=allowed_patterns or {},
        max_day_service=max_day_service,
        use_slack=False,
    )
    if shifts_data is not None:
        if _phone_no_eligible_warning:
            warnings_data.append(_phone_no_eligible_warning)
        return shifts_data, warnings_data

    # Phase 2: スラック変数付き
    shifts_data, warnings_data = _solve_care(
        year, month, all_dates, staff_ids, staff_by_id,
        off_request_set, min_day_service, min_visit_am, min_visit_pm,
        min_dual, closed_days_set, visit_operating_days,
        am_preferred_gender=am_preferred_gender,
        phone_duty_enabled=phone_duty_enabled,
        phone_duty_max_consecutive=phone_duty_max_consecutive,
        min_staff_at_9=min_staff_at_9,
        min_staff_at_11=min_staff_at_11,
        min_staff_at_13=min_staff_at_13,
        min_staff_at_15=min_staff_at_15,
        male_am_constraint_mode=male_am_constraint_mode,
        placement_rules=placement_rules,
        counselor_staff_ids=counselor_staff_ids,
        min_counselor_staff=min_counselor_staff,
        min_full_day_counselor=min_full_day_counselor,
        allowed_patterns=allowed_patterns or {},
        max_day_service=max_day_service,
        use_slack=True,
    )
    if shifts_data is not None:
        if _phone_no_eligible_warning:
            warnings_data.append(_phone_no_eligible_warning)
        return shifts_data, warnings_data

    # Phase 3: 配置ルールの hard を soft に緩和して再試行
    relaxed_rules = []
    relaxed_rule_names = []
    for pr in placement_rules:
        relaxed = dict(pr)
        if relaxed.get("is_hard", False):
            relaxed["is_hard"] = False
            relaxed["penalty_weight"] = max(int(relaxed.get("penalty_weight", 100) or 100), 300)
            relaxed_rule_names.append(relaxed.get("name", ""))
        relaxed_rules.append(relaxed)

    shifts_data, warnings_data = _solve_care(
        year, month, all_dates, staff_ids, staff_by_id,
        off_request_set, min_day_service, min_visit_am, min_visit_pm,
        min_dual, closed_days_set, visit_operating_days,
        am_preferred_gender=am_preferred_gender,
        phone_duty_enabled=phone_duty_enabled,
        phone_duty_max_consecutive=phone_duty_max_consecutive,
        min_staff_at_9=min_staff_at_9,
        min_staff_at_11=min_staff_at_11,
        min_staff_at_13=min_staff_at_13,
        min_staff_at_15=min_staff_at_15,
        male_am_constraint_mode=male_am_constraint_mode,
        placement_rules=relaxed_rules,
        counselor_staff_ids=counselor_staff_ids,
        min_counselor_staff=min_counselor_staff,
        min_full_day_counselor=min_full_day_counselor,
        allowed_patterns=allowed_patterns or {},
        max_day_service=max_day_service,
        use_slack=True,
    )
    if shifts_data is not None:
        if relaxed_rule_names:
            warnings_data.append({
                "date": all_dates[0].strftime("%Y-%m-%d"),
                "warning_type": "placement_rules_relaxed",
                "message": "一部の必須配置ルールを緩和してシフトを生成しました: "
                + ", ".join(name for name in relaxed_rule_names if name),
            })
        if _phone_no_eligible_warning:
            warnings_data.append(_phone_no_eligible_warning)
        return shifts_data, warnings_data

    # Phase 4: 完全フォールバック（全員休み）
    shifts_data = []
    warnings_data = [
        {
            "date": dt.strftime("%Y-%m-%d"),
            "warning_type": "no_solution",
            "message": "介護ソルバーが解を見つけられませんでした。制約を見直してください。",
        }
        for dt in all_dates
    ]
    if _phone_no_eligible_warning:
        warnings_data.append(_phone_no_eligible_warning)

    return shifts_data, warnings_data


# ===========================================================================
# 介護職員ソルバー本体
# ===========================================================================
def _solve_care(
    year, month, all_dates, staff_ids, staff_by_id, off_request_set,
    min_day_service, min_visit_am, min_visit_pm, min_dual,
    closed_days_set, visit_operating_days,
    am_preferred_gender: str = "",
    phone_duty_enabled: bool = False,
    phone_duty_max_consecutive: int = 1,
    min_staff_at_9: int = 4,
    min_staff_at_11: int = None,
    min_staff_at_13: int = None,
    min_staff_at_15: int = 4,
    male_am_constraint_mode: str = "hard",
    placement_rules: list = None,
    counselor_staff_ids: list = None,
    min_counselor_staff: int = 0,
    min_full_day_counselor: int = 0,
    allowed_patterns: dict = None,
    max_day_service: int = 0,
    use_slack: bool = False,
):
    """
    介護職員の CP-SAT モデルを構築し解を求める。
    """
    if placement_rules is None:
        placement_rules = []
    if counselor_staff_ids is None:
        counselor_staff_ids = []

    model = cp_model.CpModel()
    num_days = len(all_dates)
    visit_operating_set = set(visit_operating_days)

    if min_staff_at_11 is None:
        min_staff_at_11 = min_day_service
    if min_staff_at_13 is None:
        min_staff_at_13 = min_day_service

    # ==================================================================
    # 決定変数: x[s, d, a] = 1 iff 職員 s が日 d にアサインメント a
    # ==================================================================
    x = {}
    for s in staff_ids:
        for d_idx in range(num_days):
            for a in CARE_ASSIGNMENTS:
                x[s, d_idx, a] = model.new_bool_var(f"x_s{s}_d{d_idx}_{a}")

    # ==================================================================
    # 制約 0: 相互排他 -- 各職員・各日にちょうど1つのアサインメント
    # ==================================================================
    for s in staff_ids:
        for d_idx in range(num_days):
            model.add_exactly_one(x[s, d_idx, a] for a in CARE_ASSIGNMENTS)

    # ==================================================================
    # 制約: 休業日は全員 off
    # ==================================================================
    closed_day_indices = set()
    for d_idx, dt in enumerate(all_dates):
        if dt.weekday() in closed_days_set:
            closed_day_indices.add(d_idx)
            for s in staff_ids:
                model.add(x[s, d_idx, "off"] == 1)

    non_closed_days = [d_idx for d_idx in range(num_days) if d_idx not in closed_day_indices]

    # ==================================================================
    # ② 看護師/PT判定（デイ人数・9時/15時制約の両方で使用）
    # ==================================================================
    nurse_pt_qual_ids = set()
    for pr in placement_rules:
        pr_name = pr.get("name", "")
        if "看護" in pr_name or "nurse" in pr_name.lower() or "PT" in pr_name:
            nurse_pt_qual_ids.update(pr.get("target_qualification_ids", []))

    non_nurse_pt_staff = [
        s for s in staff_ids
        if not nurse_pt_qual_ids.intersection(set(staff_by_id[s].get("qualification_ids", [])))
    ] if nurse_pt_qual_ids else staff_ids

    # ==================================================================
    # 制約: 訪問非営業日は訪問系アサインメント不可
    # ==================================================================
    for d_idx, dt in enumerate(all_dates):
        if d_idx in closed_day_indices:
            continue
        if dt.weekday() not in visit_operating_set:
            for s in staff_ids:
                for a in VISIT_ASSIGNMENTS:
                    model.add(x[s, d_idx, a] == 0)

    # ==================================================================
    # 制約: 勤務可能曜日の遵守
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        avail_weekdays = set(info["available_days"])
        for d_idx, dt in enumerate(all_dates):
            if dt.weekday() not in avail_weekdays:
                model.add(x[s, d_idx, "off"] == 1)

    # ==================================================================
    # 制約: 固定休曜日の遵守
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        fixed_off = set(info["fixed_days_off"])
        for d_idx, dt in enumerate(all_dates):
            if dt.weekday() in fixed_off:
                model.add(x[s, d_idx, "off"] == 1)

    # ==================================================================
    # 制約: 希望休の遵守
    # ==================================================================
    for s in staff_ids:
        for d_idx, dt in enumerate(all_dates):
            if (s, dt) in off_request_set:
                model.add(x[s, d_idx, "off"] == 1)

    # ==================================================================
    # 制約: 祝日NG（⑨）
    # ==================================================================
    for s in staff_ids:
        if staff_by_id[s].get("holiday_ng"):
            for d_idx, dt in enumerate(all_dates):
                if jpholiday.is_holiday(dt):
                    model.add(x[s, d_idx, "off"] == 1)

    # ==================================================================
    # 制約: 土日どちらかは休み（weekend_constraint == "one_off"）
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        if info.get("weekend_constraint") == "one_off":
            # 週ごとの土日ペアを探して、少なくとも一方をoffにする
            weeks = _get_week_ranges(all_dates)
            for week_indices in weeks:
                weekend_indices = [
                    d_idx for d_idx in week_indices
                    if all_dates[d_idx].weekday() in (5, 6)  # 土=5, 日=6
                ]
                if len(weekend_indices) >= 2:
                    # 土日が両方ある週: 少なくとも1日は休み
                    model.add(
                        sum(x[s, d_idx, "off"] for d_idx in weekend_indices) >= 1
                    )

    # ==================================================================
    # 制約: 勤務可能時間帯の遵守
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        ts = info["available_time_slots"]
        if ts == "am_only":
            for d_idx in range(num_days):
                for a in AM_ONLY_FORBIDDEN:
                    model.add(x[s, d_idx, a] == 0)
        elif ts == "pm_only":
            for d_idx in range(num_days):
                for a in PM_ONLY_FORBIDDEN:
                    model.add(x[s, d_idx, a] == 0)

    # ==================================================================
    # 制約: 兼務不可の職員は訪問系アサインメント不可
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        if not info["can_visit"]:
            for d_idx in range(num_days):
                for a in VISIT_ASSIGNMENTS:
                    model.add(x[s, d_idx, a] == 0)

    # ==================================================================
    # 制約: 許可アサインメント制限 (StaffAllowedPattern)
    # ==================================================================
    if allowed_patterns:
        for s in staff_ids:
            if s in allowed_patterns:
                allowed = set(allowed_patterns[s])
                if not allowed:
                    continue

                # UIの許可パターンは主にデイ①〜④を制御するため、
                # ここで訪問/兼務まで閉じると解が極端に出にくくなる。
                allowed_day_patterns = allowed & DAY_PATTERN_ASSIGNMENTS
                if allowed_day_patterns:
                    for a in DAY_PATTERN_ASSIGNMENTS:
                        if a not in allowed_day_patterns:
                            for d_idx in range(num_days):
                                model.add(x[s, d_idx, a] == 0)

                # API等で訪問/兼務を明示指定した場合のみ、その範囲で制限する。
                allowed_visit_assignments = allowed & VISIT_ASSIGNMENTS
                if allowed_visit_assignments:
                    for a in VISIT_ASSIGNMENTS:
                        if a not in allowed_visit_assignments:
                            for d_idx in range(num_days):
                                model.add(x[s, d_idx, a] == 0)

    # ==================================================================
    # 制約: 連勤上限
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        max_con = info["max_consecutive_days"]
        window = max_con + 1
        for start in range(num_days - window + 1):
            model.add(
                sum(x[s, start + k, "off"] for k in range(window)) >= 1
            )

    # ==================================================================
    # 制約: 週の勤務日数上限・下限（月曜始まり）
    # ==================================================================
    min_pw_penalties = []
    min_pw_hard_penalties = []
    for s in staff_ids:
        info = staff_by_id[s]
        max_pw = info["max_days_per_week"]
        min_pw = info.get("min_days_per_week", 0)
        avail_set = set(info["available_days"])
        weeks = _get_week_ranges(all_dates)
        for w_idx, week_indices in enumerate(weeks):
            working_vars = []
            for d_idx in week_indices:
                w = model.new_bool_var(f"care_work_s{s}_d{d_idx}")
                model.add(w == 1 - x[s, d_idx, "off"])
                working_vars.append(w)
            # 上限: 不完全週は日数に合わせて調整
            adj_max = min(max_pw, len(week_indices))
            model.add(sum(working_vars) <= adj_max)
            # 下限
            if min_pw > 0:
                avail_in_week = sum(
                    1 for d_idx in week_indices
                    if all_dates[d_idx].weekday() in avail_set
                )
                adj_min = min(min_pw, avail_in_week)
                if len(week_indices) < 7:
                    adj_min = min(adj_min, max(0, round(min_pw * len(week_indices) / 7)))
                if adj_min > 0:
                    deficit = model.new_int_var(0, adj_min, f"care_min_pw_deficit_s{s}_w{w_idx}")
                    model.add(sum(working_vars) + deficit >= adj_min)
                    if min_pw == max_pw:
                        # 固定日数（min==max）: 高ペナルティで強く強制
                        min_pw_hard_penalties.append(deficit)
                    else:
                        min_pw_penalties.append(deficit)

    # ==================================================================
    # スラック変数（use_slack=True のとき）
    # ==================================================================
    slack_day_am = {}
    slack_day_pm = {}
    slack_visit_am = {}
    slack_visit_pm = {}
    slack_dual = {}
    slack_staff_9 = {}
    slack_staff_11 = {}
    slack_staff_13 = {}
    slack_staff_15 = {}
    slack_counselor_staff = {}
    slack_counselor_full_day = {}

    if use_slack:
        for d_idx in range(num_days):
            slack_day_am[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_day_am_{d_idx}"
            )
            slack_day_pm[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_day_pm_{d_idx}"
            )
            slack_visit_am[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_visit_am_{d_idx}"
            )
            slack_visit_pm[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_visit_pm_{d_idx}"
            )
            slack_staff_9[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_staff_9_{d_idx}"
            )
            slack_staff_11[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_staff_11_{d_idx}"
            )
            slack_staff_13[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_staff_13_{d_idx}"
            )
            slack_staff_15[d_idx] = model.new_int_var(
                0, len(staff_ids), f"slack_staff_15_{d_idx}"
            )
            if min_counselor_staff > 0 and counselor_staff_ids:
                slack_counselor_staff[d_idx] = model.new_int_var(
                    0, len(counselor_staff_ids), f"slack_counselor_staff_{d_idx}"
                )
            if min_full_day_counselor > 0 and counselor_staff_ids:
                slack_counselor_full_day[d_idx] = model.new_int_var(
                    0, len(counselor_staff_ids), f"slack_counselor_full_day_{d_idx}"
                )
            if min_dual > 0:
                slack_dual[d_idx] = model.new_int_var(
                    0, len(staff_ids), f"slack_dual_{d_idx}"
                )

    # ==================================================================
    # 制約: デイサービス午前・午後の最低/最大人数（休業日はスキップ）
    # ② 看護師/PTは人数カウントから除外（追加配置扱い）
    # ==================================================================
    for d_idx in range(num_days):
        if d_idx in closed_day_indices:
            continue
        day_am_count = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in DAY_AM_ASSIGNMENTS
        )
        day_pm_count = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in DAY_PM_ASSIGNMENTS
        )
        if use_slack:
            model.add(day_am_count + slack_day_am[d_idx] >= min_day_service)
            model.add(day_pm_count + slack_day_pm[d_idx] >= min_day_service)
        else:
            model.add(day_am_count >= min_day_service)
            model.add(day_pm_count >= min_day_service)
        # 上限制約（スラック有無に関わらず常に有効）
        model.add(day_am_count <= max_day_service)
        model.add(day_pm_count <= max_day_service)

    # ==================================================================
    # 制約: 訪問介護午前はちょうど指定人数（休業日・訪問非営業日はスキップ）
    # ==================================================================
    for d_idx, dt in enumerate(all_dates):
        if d_idx in closed_day_indices:
            continue
        if dt.weekday() not in visit_operating_set:
            continue
        visit_am_count = sum(
            x[s, d_idx, a] for s in staff_ids for a in VISIT_AM_ASSIGNMENTS
        )
        if use_slack:
            model.add(visit_am_count + slack_visit_am[d_idx] >= min_visit_am)
        else:
            model.add(visit_am_count >= min_visit_am)
        # 上限も設定: ちょうど指定人数
        model.add(visit_am_count <= min_visit_am)

    # ==================================================================
    # 制約: 訪問介護午後はちょうど指定人数（休業日・訪問非営業日はスキップ）
    # ==================================================================
    for d_idx, dt in enumerate(all_dates):
        if d_idx in closed_day_indices:
            continue
        if dt.weekday() not in visit_operating_set:
            continue
        visit_pm_count = sum(
            x[s, d_idx, a] for s in staff_ids for a in VISIT_PM_ASSIGNMENTS
        )
        if use_slack:
            model.add(visit_pm_count + slack_visit_pm[d_idx] >= min_visit_pm)
        else:
            model.add(visit_pm_count >= min_visit_pm)
        # 上限も設定: ちょうど指定人数
        model.add(visit_pm_count <= min_visit_pm)

    # ==================================================================
    # 制約: 兼務者の最低人数（休業日・訪問非営業日はスキップ）
    # ==================================================================
    if min_dual > 0:
        for d_idx, dt in enumerate(all_dates):
            if d_idx in closed_day_indices:
                continue
            if dt.weekday() not in visit_operating_set:
                continue
            dual_count = sum(
                x[s, d_idx, a] for s in staff_ids for a in DUAL_ASSIGNMENTS
            )
            if use_slack:
                model.add(dual_count + slack_dual[d_idx] >= min_dual)
            else:
                model.add(dual_count >= min_dual)

    # ==================================================================
    # 制約: 9時・11時・13時・15時で最低人数必須
    # ② 看護師/PTを4名カウントから除外（nurse_pt判定は上で定義済み）
    # ==================================================================
    for d_idx in non_closed_days:
        count_9 = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in PRESENT_AT_9
        )
        count_11 = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in PRESENT_AT_11
        )
        count_13 = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in PRESENT_AT_13
        )
        count_15 = sum(
            x[s, d_idx, a] for s in non_nurse_pt_staff for a in PRESENT_AT_15
        )
        if use_slack:
            model.add(count_9 + slack_staff_9[d_idx] >= min_staff_at_9)
            model.add(count_11 + slack_staff_11[d_idx] >= min_staff_at_11)
            model.add(count_13 + slack_staff_13[d_idx] >= min_staff_at_13)
            model.add(count_15 + slack_staff_15[d_idx] >= min_staff_at_15)
        else:
            model.add(count_9 >= min_staff_at_9)
            model.add(count_11 >= min_staff_at_11)
            model.add(count_13 >= min_staff_at_13)
            model.add(count_15 >= min_staff_at_15)

    # ==================================================================
    # 制約: 相談員ローテON時は、原則として相談員を最低2名出勤させる
    # 1名だけでは4スロットを休憩非重複で埋め切れないため。
    # ==================================================================
    if min_counselor_staff > 0 and counselor_staff_ids:
        for d_idx in non_closed_days:
            counselor_working_count = sum(
                x[s, d_idx, a] for s in counselor_staff_ids for a in CARE_WORKING_ASSIGNMENTS
            )
            if use_slack:
                model.add(
                    counselor_working_count + slack_counselor_staff[d_idx] >= min_counselor_staff
                )
            else:
                model.add(counselor_working_count >= min_counselor_staff)

    # ==================================================================
    # 制約: 相談員ローテON時は、原則として相談員を最低2名はフルタイムで出勤させる
    # 端パターン2名だけだと 11-15 時帯の相談員枠が埋まらない日が出るため。
    # ==================================================================
    if min_full_day_counselor > 0 and counselor_staff_ids:
        for d_idx in non_closed_days:
            counselor_full_day_count = sum(
                x[s, d_idx, a] for s in counselor_staff_ids for a in PRESENT_FULL_DAY
            )
            if use_slack:
                model.add(
                    counselor_full_day_count + slack_counselor_full_day[d_idx]
                    >= min_full_day_counselor
                )
            else:
                model.add(counselor_full_day_count >= min_full_day_counselor)

    # ==================================================================
    # 制約: 配置ルール（PlacementRule テーブルから動的生成）
    # ==================================================================
    placement_soft_penalties = []
    placement_soft_trackers = []
    _add_placement_rules(
        model, x, staff_ids, staff_by_id, placement_rules,
        non_closed_days, all_dates, closed_day_indices,
        use_slack, placement_soft_penalties, placement_soft_trackers,
    )

    # ==================================================================
    # 目的関数
    # ==================================================================

    # --- ソフト制約: 公平性 (min-max 勤務日数) ---
    work_count = {}
    for s in staff_ids:
        work_count[s] = sum(
            x[s, d_idx, a]
            for d_idx in range(num_days)
            for a in CARE_WORKING_ASSIGNMENTS
        )

    max_work = model.new_int_var(0, num_days, "care_max_work")
    min_work = model.new_int_var(0, num_days, "care_min_work")
    for s in staff_ids:
        model.add(max_work >= work_count[s])
        model.add(min_work <= work_count[s])

    fairness_diff = model.new_int_var(0, num_days, "care_fairness_diff")
    model.add(fairness_diff == max_work - min_work)

    # 総出勤日数（最小化対象）
    total_working_days = sum(work_count[s] for s in staff_ids)
    total_min_pw_penalty = sum(min_pw_penalties) if min_pw_penalties else 0
    day2_count = sum(
        x[s, d_idx, "day_pattern2"]
        for s in staff_ids
        for d_idx in range(num_days)
    )
    # min==maxの固定日数制約は高ペナルティ（通常の10倍）
    hard_pw_weight = (num_days + 1) * 10
    total_min_pw_hard_penalty = (
        sum(min_pw_hard_penalties) * hard_pw_weight if min_pw_hard_penalties else 0
    )

    # 男性午前制約は PlacementRule に統一（solver直接実装を廃止）
    gender_penalty = 0

    # --- 電話当番ローテーション ---
    phone = {}
    phone_fairness = 0

    if phone_duty_enabled:
        # 電話当番は社員のみ（常勤、時短正社員、管理者）
        phone_eligible = [
            s for s in staff_ids
            if staff_by_id[s].get("has_phone_duty")
            and staff_by_id[s].get("employment_type") in ("常勤", "時短正社員", "管理者")
        ]
    else:
        phone_eligible = []

    if phone_eligible:
        for s in phone_eligible:
            for d_idx in range(num_days):
                phone[s, d_idx] = model.new_bool_var(f"phone_s{s}_d{d_idx}")

        for d_idx in range(num_days):
            if d_idx in closed_day_indices:
                for s in phone_eligible:
                    model.add(phone[s, d_idx] == 0)
            else:
                model.add_exactly_one(phone[s, d_idx] for s in phone_eligible)

        # 電話当番は全日勤務（半日パターン除外）で事業所にいる日のみ
        phone_eligible_assignments = {"day_pattern1", "day_pattern2"}
        for s in phone_eligible:
            for d_idx in range(num_days):
                is_at_office_fullday = sum(x[s, d_idx, a] for a in phone_eligible_assignments)
                model.add(phone[s, d_idx] <= is_at_office_fullday)

        # 連続当番制限
        if phone_duty_max_consecutive > 0:
            window = phone_duty_max_consecutive + 1
            for s in phone_eligible:
                for start in range(num_days - window + 1):
                    model.add(sum(phone[s, start + k] for k in range(window)) <= phone_duty_max_consecutive)

        # 電話当番の公平性
        phone_counts = {s: sum(phone[s, d] for d in range(num_days)) for s in phone_eligible}
        max_phone = model.new_int_var(0, num_days, "max_phone")
        min_phone = model.new_int_var(0, num_days, "min_phone")
        for s in phone_eligible:
            model.add(max_phone >= phone_counts[s])
            model.add(min_phone <= phone_counts[s])
        phone_fair_var = model.new_int_var(0, num_days, "phone_fairness")
        model.add(phone_fair_var == max_phone - min_phone)
        phone_fairness = phone_fair_var

    # --- 配置ルールのソフトペナルティ合計 ---
    placement_penalty_total = sum(placement_soft_penalties) if placement_soft_penalties else 0

    # 重み: 出勤1日削減(num_days+1) > 公平性の最大改善(num_days) を保証
    headcount_weight = num_days + 1

    if use_slack:
        all_slack_terms = []
        for d in range(num_days):
            all_slack_terms.extend([slack_day_am[d], slack_day_pm[d], slack_visit_am[d], slack_visit_pm[d]])
            all_slack_terms.extend([slack_staff_9[d], slack_staff_11[d], slack_staff_13[d], slack_staff_15[d]])
            if min_counselor_staff > 0 and counselor_staff_ids:
                all_slack_terms.append(slack_counselor_staff[d])
            if min_full_day_counselor > 0 and counselor_staff_ids:
                all_slack_terms.append(slack_counselor_full_day[d])
            if min_dual > 0:
                all_slack_terms.append(slack_dual[d])
        max_slack_terms_per_day = (
            8
            + (1 if min_counselor_staff > 0 and counselor_staff_ids else 0)
            + (1 if min_full_day_counselor > 0 and counselor_staff_ids else 0)
            + (1 if min_dual > 0 else 0)
        )
        total_slack = model.new_int_var(
            0,
            len(staff_ids) * num_days * max_slack_terms_per_day,
            "care_total_slack",
        )
        model.add(total_slack == sum(all_slack_terms))
        slack_weight = (num_days + 1) * len(staff_ids) + 1
        model.minimize(
            total_slack * slack_weight
            + total_working_days * headcount_weight
            + fairness_diff + total_min_pw_penalty + total_min_pw_hard_penalty
            + gender_penalty + phone_fairness + placement_penalty_total
            + day2_count
        )
    else:
        model.minimize(
            total_working_days * headcount_weight
            + fairness_diff + total_min_pw_penalty + total_min_pw_hard_penalty
            + gender_penalty + phone_fairness + placement_penalty_total
            + day2_count
        )

    # ==================================================================
    # ソルバー実行
    # ==================================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 45
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0

    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None

    # ==================================================================
    # 解の読み取り
    # ==================================================================
    shifts_data = []
    warnings_data = []

    for d_idx, dt in enumerate(all_dates):
        date_str = dt.strftime("%Y-%m-%d")
        for s in staff_ids:
            for a in CARE_ASSIGNMENTS:
                if solver.value(x[s, d_idx, a]) == 1:
                    if a != "off":
                        shifts_data.append({
                            "date": date_str,
                            "staff_id": s,
                            "assignment": a,
                            "is_phone_duty": bool(phone_eligible and (s, d_idx) in phone and solver.value(phone[s, d_idx])),
                        })
                    break

    # ------------------------------------------------------------------
    # 警告の生成
    # ------------------------------------------------------------------
    if use_slack:
        for d_idx, dt in enumerate(all_dates):
            date_str = dt.strftime("%Y-%m-%d")

            val = solver.value(slack_day_am[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_day_am",
                    "message": f"デイサービス午前: {val}名不足",
                })

            val = solver.value(slack_day_pm[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_day_pm",
                    "message": f"デイサービス午後: {val}名不足",
                })

            val = solver.value(slack_visit_am[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_visit_am",
                    "message": f"訪問介護午前: {val}名不足",
                })

            val = solver.value(slack_visit_pm[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_visit_pm",
                    "message": f"訪問介護午後: {val}名不足",
                })

            if min_dual > 0:
                val = solver.value(slack_dual[d_idx])
                if val > 0:
                    warnings_data.append({
                        "date": date_str,
                        "warning_type": "dual_shortage",
                        "message": f"兼務者: {val}名不足",
                    })

            val = solver.value(slack_staff_9[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_at_9",
                    "message": f"9時在籍人数: {val}名不足",
                })

            val = solver.value(slack_staff_11[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_at_11",
                    "message": f"11時在籍人数: {val}名不足",
                })

            val = solver.value(slack_staff_13[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_at_13",
                    "message": f"13時在籍人数: {val}名不足",
                })

            val = solver.value(slack_staff_15[d_idx])
            if val > 0:
                warnings_data.append({
                    "date": date_str,
                    "warning_type": "understaffed_at_15",
                    "message": f"15時在籍人数: {val}名不足",
                })

            if min_counselor_staff > 0 and counselor_staff_ids:
                val = solver.value(slack_counselor_staff[d_idx])
                if val > 0:
                    warnings_data.append({
                        "date": date_str,
                        "warning_type": "understaffed_counselor_staff",
                        "message": f"相談員出勤人数: {val}名不足",
                    })

            if min_full_day_counselor > 0 and counselor_staff_ids:
                val = solver.value(slack_counselor_full_day[d_idx])
                if val > 0:
                    warnings_data.append({
                        "date": date_str,
                        "warning_type": "understaffed_counselor_full_day",
                        "message": f"相談員フルタイム人数: {val}名不足",
                    })

    # ------------------------------------------------------------------
    # ソフト配置ルール違反の警告
    # ------------------------------------------------------------------
    for miss_var, rule_name, d_idx in placement_soft_trackers:
        if solver.value(miss_var):
            date_str = all_dates[d_idx].strftime("%Y-%m-%d")
            warnings_data.append({
                "date": date_str,
                "warning_type": "placement_rule_unmet",
                "message": f"配置ルール未達: {rule_name}",
            })

    return shifts_data, warnings_data


# ===========================================================================
# 配置ルール制約追加（PlacementRule → CP-SAT制約）
# ===========================================================================
def _add_placement_rules(
    model, x, staff_ids, staff_by_id, placement_rules,
    non_closed_days, all_dates, closed_day_indices,
    use_slack, soft_penalties, soft_trackers=None,
):
    """PlacementRuleテーブルのルールをCP-SAT制約に変換する。
    soft_trackers: Noneでなければ (miss_var, rule_name, d_idx) を追記する。
    """
    for rule in placement_rules:
        if not rule.get("is_active", True):
            continue

        rule_type = rule.get("rule_type", "")
        period = rule.get("period", "all")
        min_count = rule.get("min_count", 1)
        is_hard = rule.get("is_hard", True)
        penalty_weight = rule.get("penalty_weight", 100)

        # 適用曜日
        apply_weekdays_str = rule.get("apply_weekdays", "0,1,2,3,4,5,6")
        apply_weekdays = set(int(x) for x in apply_weekdays_str.split(",") if x.strip())

        # 対象職員の絞り込み
        target_staff = list(staff_ids)
        if rule_type == "qualification_min":
            target_qual_ids = set(rule.get("target_qualification_ids", []))
            if target_qual_ids:
                target_staff = [
                    s for s in staff_ids
                    if set(staff_by_id[s].get("qualification_ids", [])) & target_qual_ids
                ]
        elif rule_type == "gender_min":
            target_gender = rule.get("target_gender", "")
            if target_gender:
                target_staff = [
                    s for s in staff_ids
                    if staff_by_id[s].get("gender") == target_gender
                ]

        if not target_staff:
            continue

        # 対象アサインメント（時間帯で絞り込み）
        if period == "am":
            target_assignments = DAY_AM_ASSIGNMENTS
        elif period == "pm":
            target_assignments = DAY_PM_ASSIGNMENTS
        else:
            # "all" = 全日在籍パターン（9時〜16時を通して事業所にいる）
            # 半日パターン(day_pattern3/4)や兼務(day_p3_visit_pm等)は含まない
            target_assignments = PRESENT_FULL_DAY

        # 各日に制約を追加
        for d_idx in non_closed_days:
            dt = all_dates[d_idx]
            if dt.weekday() not in apply_weekdays:
                continue

            count = sum(
                x[s, d_idx, a] for s in target_staff for a in target_assignments
            )

            if is_hard:
                # ハード制約: use_slack関係なく常にハード
                model.add(count >= min_count)
            else:
                # ソフト制約（ペナルティ）
                miss = model.new_bool_var(f"rule_{rule.get('id', 0)}_miss_d{d_idx}")
                model.add(count >= min_count).only_enforce_if(miss.Not())
                model.add(count < min_count).only_enforce_if(miss)
                soft_penalties.append(miss * penalty_weight)
                if soft_trackers is not None:
                    soft_trackers.append((miss, rule.get("name", ""), d_idx))


# ===========================================================================
# 調理職員ソルバー（フォールバック付き）
# ===========================================================================
def _solve_cooking_with_fallback(
    year, month, all_dates, cook_staff, day_off_requests, settings,
    allowed_patterns=None,
):
    """
    調理職員のシフトを生成する。
    1. ハード制約のみで解を試みる
    2. 不可能ならスラック変数付きで再実行
    3. それでも不可能なら全員休み + 警告
    """
    if not cook_staff:
        return [], []

    # 休み希望を (staff_id, date) の集合に変換
    off_request_set = set()
    for req in day_off_requests:
        d = req["date"]
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d)
        off_request_set.add((req["staff_id"], d))

    # 職員データの正規化
    staff_by_id = {}
    for s in cook_staff:
        sid = s["id"]
        raw_avail = s.get("available_days", [0, 1, 2, 3, 4])
        if isinstance(raw_avail, str):
            raw_avail = [int(x) for x in raw_avail.split(",") if x.strip()]
        raw_fixed = s.get("fixed_days_off", [])
        if isinstance(raw_fixed, str):
            raw_fixed = [int(x) for x in raw_fixed.split(",") if x.strip()] if raw_fixed else []
        staff_by_id[sid] = {
            "id": sid,
            "name": s.get("name", f"Cook_{sid}"),
            "employment_type": s.get("employment_type", "常勤"),
            "max_consecutive_days": s.get("max_consecutive_days", 5),
            "max_days_per_week": s.get("max_days_per_week", 5),
            "available_days": raw_avail,
            "fixed_days_off": raw_fixed,
            "weekend_constraint": s.get("weekend_constraint", ""),
            "min_days_per_week": s.get("min_days_per_week", 0),
            "holiday_ng": s.get("holiday_ng", False),
        }

    staff_ids = list(staff_by_id.keys())
    closed_days_set = set(settings.get("closed_days", [5, 6]))
    cooking_combo_rules = settings.get("cooking_combo_rules", [])

    # 設定値から時間帯別最低人数を動的に構築
    # intervals: [6-8), [8-12), [13-19)
    min_cooking = settings.get("min_cooking_staff") or 1
    cook_min_staff = (min_cooking, min_cooking, min_cooking)

    # Phase 1: ハード制約のみ
    shifts_data, warnings_data = _solve_cooking(
        year, month, all_dates, staff_ids, staff_by_id,
        off_request_set, closed_days_set, cook_min_staff,
        cooking_combo_rules=cooking_combo_rules,
        allowed_patterns=allowed_patterns or {},
        use_slack=False,
    )
    if shifts_data is not None:
        return shifts_data, warnings_data

    # Phase 2: スラック変数付き
    shifts_data, warnings_data = _solve_cooking(
        year, month, all_dates, staff_ids, staff_by_id,
        off_request_set, closed_days_set, cook_min_staff,
        cooking_combo_rules=cooking_combo_rules,
        allowed_patterns=allowed_patterns or {},
        use_slack=True,
    )
    if shifts_data is not None:
        return shifts_data, warnings_data

    # Phase 3: 完全フォールバック（全員休み）
    shifts_data = []
    warnings_data = [
        {
            "date": dt.strftime("%Y-%m-%d"),
            "warning_type": "no_solution",
            "message": "調理ソルバーが解を見つけられませんでした。制約を見直してください。",
        }
        for dt in all_dates
    ]
    return shifts_data, warnings_data


# ===========================================================================
# 調理職員ソルバー本体
# ===========================================================================
def _solve_cooking(
    year, month, all_dates, staff_ids, staff_by_id, off_request_set,
    closed_days_set, cook_min_staff,
    cooking_combo_rules: list = None,
    allowed_patterns: dict = None,
    use_slack: bool = False,
):
    """
    調理職員の CP-SAT モデルを構築し解を求める。
    """
    if cooking_combo_rules is None:
        cooking_combo_rules = []

    model = cp_model.CpModel()
    num_days = len(all_dates)
    num_intervals = len(cook_min_staff)  # 3

    # ==================================================================
    # 決定変数: x[s, d, a] = 1 iff 調理職員 s が日 d にアサインメント a
    # ==================================================================
    x = {}
    for s in staff_ids:
        for d_idx in range(num_days):
            for a in COOK_ASSIGNMENTS:
                x[s, d_idx, a] = model.new_bool_var(f"ck_s{s}_d{d_idx}_{a}")

    # ==================================================================
    # 制約 0: 相互排他 -- 各職員・各日にちょうど1つのアサインメント
    # ==================================================================
    for s in staff_ids:
        for d_idx in range(num_days):
            model.add_exactly_one(x[s, d_idx, a] for a in COOK_ASSIGNMENTS)

    # ==================================================================
    # 制約: 休業日は全員 cook_off
    # ==================================================================
    closed_day_indices = set()
    for d_idx, dt in enumerate(all_dates):
        if dt.weekday() in closed_days_set:
            closed_day_indices.add(d_idx)
            for s in staff_ids:
                model.add(x[s, d_idx, "cook_off"] == 1)

    # ==================================================================
    # 制約: 勤務可能曜日の遵守
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        avail_weekdays = set(info["available_days"])
        for d_idx, dt in enumerate(all_dates):
            if dt.weekday() not in avail_weekdays:
                model.add(x[s, d_idx, "cook_off"] == 1)

    # ==================================================================
    # 制約: 固定休曜日の遵守
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        fixed_off = set(info["fixed_days_off"])
        for d_idx, dt in enumerate(all_dates):
            if dt.weekday() in fixed_off:
                model.add(x[s, d_idx, "cook_off"] == 1)

    # ==================================================================
    # 制約: 希望休の遵守
    # ==================================================================
    for s in staff_ids:
        for d_idx, dt in enumerate(all_dates):
            if (s, dt) in off_request_set:
                model.add(x[s, d_idx, "cook_off"] == 1)

    # ==================================================================
    # 制約: 祝日NG（⑨）
    # ==================================================================
    for s in staff_ids:
        if staff_by_id[s].get("holiday_ng"):
            for d_idx, dt in enumerate(all_dates):
                if jpholiday.is_holiday(dt):
                    model.add(x[s, d_idx, "cook_off"] == 1)

    # ==================================================================
    # 制約: 土日どちらかは休み（weekend_constraint == "one_off"）
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        if info.get("weekend_constraint") == "one_off":
            weeks = _get_week_ranges(all_dates)
            for week_indices in weeks:
                weekend_indices = [
                    d_idx for d_idx in week_indices
                    if all_dates[d_idx].weekday() in (5, 6)
                ]
                if len(weekend_indices) >= 2:
                    model.add(
                        sum(x[s, d_idx, "cook_off"] for d_idx in weekend_indices) >= 1
                    )

    # ==================================================================
    # 制約: 許可アサインメント制限 (StaffAllowedPattern)
    # ==================================================================
    if allowed_patterns:
        for s in staff_ids:
            if s in allowed_patterns:
                allowed = allowed_patterns[s]
                for a in COOK_ASSIGNMENTS:
                    if a != "cook_off" and a not in allowed:
                        for d_idx in range(num_days):
                            model.add(x[s, d_idx, a] == 0)

    # ==================================================================
    # 制約: 連勤上限
    # ==================================================================
    for s in staff_ids:
        info = staff_by_id[s]
        max_con = info["max_consecutive_days"]
        window = max_con + 1
        for start in range(num_days - window + 1):
            model.add(
                sum(x[s, start + k, "cook_off"] for k in range(window)) >= 1
            )

    # ==================================================================
    # 制約: 週の勤務日数上限・下限（月曜始まり）
    # ==================================================================
    cook_min_pw_penalties = []
    for s in staff_ids:
        info = staff_by_id[s]
        max_pw = info["max_days_per_week"]
        min_pw = info.get("min_days_per_week", 0)
        avail_set = set(info["available_days"])
        weeks = _get_week_ranges(all_dates)
        for w_idx, week_indices in enumerate(weeks):
            working_vars = []
            for d_idx in week_indices:
                w = model.new_bool_var(f"cook_work_s{s}_d{d_idx}")
                model.add(w == 1 - x[s, d_idx, "cook_off"])
                working_vars.append(w)
            adj_max = min(max_pw, len(week_indices))
            model.add(sum(working_vars) <= adj_max)
            if min_pw > 0:
                avail_in_week = sum(
                    1 for d_idx in week_indices
                    if all_dates[d_idx].weekday() in avail_set
                )
                adj_min = min(min_pw, avail_in_week)
                if len(week_indices) < 7:
                    adj_min = min(adj_min, max(0, round(min_pw * len(week_indices) / 7)))
                if adj_min > 0:
                    deficit = model.new_int_var(0, adj_min, f"cook_min_pw_deficit_s{s}_w{w_idx}")
                    model.add(sum(working_vars) + deficit >= adj_min)
                    cook_min_pw_penalties.append(deficit)

    # ==================================================================
    # 制約: 調理の日単位組み合わせ（CookingComboRule）
    # ==================================================================
    for combo_rule in cooking_combo_rules:
        if not combo_rule.get("is_active", True):
            continue
        combo_patterns = combo_rule.get("allowed_patterns", [])
        if not combo_patterns:
            continue

        for d_idx in range(num_days):
            if d_idx in closed_day_indices:
                continue

            # パターン選択変数
            pat_vars = []
            for p_idx, pattern in enumerate(combo_patterns):
                pv = model.new_bool_var(f"cook_combo_r{combo_rule.get('id', 0)}_d{d_idx}_p{p_idx}")
                pat_vars.append(pv)

                # パターンに含まれないアサインメントを禁止
                forbidden_in_pattern = set(COOK_WORKING_ASSIGNMENTS) - set(pattern)
                for s in staff_ids:
                    for a in forbidden_in_pattern:
                        model.add(x[s, d_idx, a] == 0).only_enforce_if(pv)

            # 営業日はいずれかのパターンを選択
            model.add_exactly_one(pat_vars)

    # ==================================================================
    # スラック変数（use_slack=True のとき）
    # ==================================================================
    slack_interval = {}
    if use_slack:
        for d_idx in range(num_days):
            slack_interval[d_idx] = {}
            for iv in range(num_intervals):
                slack_interval[d_idx][iv] = model.new_int_var(
                    0, len(staff_ids), f"cook_slack_d{d_idx}_iv{iv}"
                )

    # ==================================================================
    # 制約: 各アサインメント（①②③④）は1日1名まで
    # ==================================================================
    for d_idx in range(num_days):
        if d_idx in closed_day_indices:
            continue
        for a in COOK_WORKING_ASSIGNMENTS:
            model.add(sum(x[s, d_idx, a] for s in staff_ids) <= 1)

    # ==================================================================
    # 制約: 時間帯カバレッジ（休業日はスキップ）
    # ==================================================================
    for d_idx in range(num_days):
        if d_idx in closed_day_indices:
            continue
        for iv in range(num_intervals):
            coverage_count = sum(
                x[s, d_idx, a] * COOK_COVERAGE[a][iv]
                for s in staff_ids
                for a in COOK_WORKING_ASSIGNMENTS
            )
            if use_slack:
                model.add(
                    coverage_count + slack_interval[d_idx][iv] >= cook_min_staff[iv]
                )
            else:
                model.add(coverage_count >= cook_min_staff[iv])

    # ==================================================================
    # 目的関数
    # ==================================================================

    # --- ソフト制約: 公平性 (min-max 勤務日数) ---
    work_count = {}
    for s in staff_ids:
        work_count[s] = sum(
            x[s, d_idx, a]
            for d_idx in range(num_days)
            for a in COOK_WORKING_ASSIGNMENTS
        )

    max_work = model.new_int_var(0, num_days, "cook_max_work")
    min_work = model.new_int_var(0, num_days, "cook_min_work")
    for s in staff_ids:
        model.add(max_work >= work_count[s])
        model.add(min_work <= work_count[s])

    fairness_diff = model.new_int_var(0, num_days, "cook_fairness_diff")
    model.add(fairness_diff == max_work - min_work)

    if use_slack:
        total_slack = model.new_int_var(
            0, len(staff_ids) * num_days * num_intervals, "cook_total_slack"
        )
        all_slack_terms = []
        for d in range(num_days):
            for iv in range(num_intervals):
                all_slack_terms.append(slack_interval[d][iv])
        model.add(total_slack == sum(all_slack_terms))
        penalty_weight = num_days + 1
        cook_pw_penalty = sum(cook_min_pw_penalties) * (num_days + 1) * 10 if cook_min_pw_penalties else 0
        model.minimize(total_slack * penalty_weight + fairness_diff + cook_pw_penalty)
    else:
        cook_pw_penalty = sum(cook_min_pw_penalties) * (num_days + 1) * 10 if cook_min_pw_penalties else 0
        model.minimize(fairness_diff + cook_pw_penalty)

    # ==================================================================
    # ソルバー実行
    # ==================================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 25
    solver.parameters.num_workers = 8

    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None

    # ==================================================================
    # 解の読み取り
    # ==================================================================
    shifts_data = []
    warnings_data = []

    for d_idx, dt in enumerate(all_dates):
        date_str = dt.strftime("%Y-%m-%d")
        for s in staff_ids:
            for a in COOK_ASSIGNMENTS:
                if solver.value(x[s, d_idx, a]) == 1:
                    if a != "cook_off":
                        shifts_data.append({
                            "date": date_str,
                            "staff_id": s,
                            "assignment": a,
                        })
                    break

    # ------------------------------------------------------------------
    # 警告の生成
    # ------------------------------------------------------------------
    interval_labels = ["6:00-8:00", "8:00-12:00", "13:00-19:00"]
    if use_slack:
        for d_idx, dt in enumerate(all_dates):
            date_str = dt.strftime("%Y-%m-%d")
            for iv in range(num_intervals):
                val = solver.value(slack_interval[d_idx][iv])
                if val > 0:
                    warnings_data.append({
                        "date": date_str,
                        "warning_type": f"understaffed_cook_interval_{iv}",
                        "message": f"調理 {interval_labels[iv]}: {val}名不足",
                    })

    return shifts_data, warnings_data


# ===========================================================================
# ユーティリティ
# ===========================================================================
def _get_week_ranges(all_dates: list[datetime.date]) -> list[list[int]]:
    """
    月内の日付リストを「月曜~日曜」の週単位に分割し、
    各週を日付インデックスのリストとして返す。
    """
    weeks = []
    current_week = []

    for d_idx, dt in enumerate(all_dates):
        current_week.append(d_idx)
        if dt.weekday() == 6:  # 日曜で週を区切る
            weeks.append(current_week)
            current_week = []

    if current_week:
        weeks.append(current_week)

    return weeks
