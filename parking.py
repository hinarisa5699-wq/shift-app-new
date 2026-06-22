"""駐車場の自動割り当て（依頼文24）。

シフト生成(solver)とは完全に独立した後処理。各営業日に出勤する「車通勤」職員へ
駐車枠（例 4/7/8）を割り当て、枠が足りない人は「コイン」（コインパーキング）にする。

- 固定枠番号を持つ人はその番号を優先（同日に同番号が競合したら先着、残りはローテーション）。
- 残りは月内で公平にローテーション（コインに偏らないよう、コイン回数の多い人を優先で実枠へ）。
- 完全に決定的（乱数なし）。同じ入力なら常に同じ結果。
"""

COIN_LABEL = "コイン"


def assign_parking(month_dates, working_by_date, car_staff, slot_numbers):
    """駐車枠を割り当てる。

    引数:
      month_dates:      対象月の date のリスト（昇順）
      working_by_date:  {date_iso: set(staff_id)} その日に出勤する車通勤職員のID集合
      car_staff:        [{"id": int, "parking_slot": str}, ...] 車通勤職員（fixed枠は空欄可）
      slot_numbers:     ["4", "7", "8", ...] 有効な枠番号（表示順）

    戻り値:
      {date_iso: {staff_id: label}}  label は枠番号 or "コイン"
    """
    slot_numbers = [str(s) for s in slot_numbers]
    slot_set = set(slot_numbers)
    fixed_of = {}
    for s in car_staff:
        fx = str(s.get("parking_slot") or "").strip()
        if fx and fx in slot_set:
            fixed_of[s["id"]] = fx

    coin_count = {}   # staff_id -> これまでにコインになった回数（公平化用）
    result = {}

    for d in month_dates:
        d_iso = d.isoformat()
        working = sorted(working_by_date.get(d_iso, set()))
        if not working:
            continue

        used = set()
        day_assign = {}

        # 1) 固定枠を優先（競合は先着＝staff_id昇順）
        rotation_pool = []
        for sid in working:
            fx = fixed_of.get(sid)
            if fx and fx not in used:
                day_assign[sid] = fx
                used.add(fx)
            else:
                rotation_pool.append(sid)

        # 2) 残りはコイン回数の多い人を優先して空き枠へ（公平化）。
        #    タイブレークは staff_id 昇順で決定的に。
        free_slots = [s for s in slot_numbers if s not in used]
        rotation_pool.sort(key=lambda sid: (-coin_count.get(sid, 0), sid))

        for i, sid in enumerate(rotation_pool):
            if i < len(free_slots):
                day_assign[sid] = free_slots[i]
            else:
                day_assign[sid] = COIN_LABEL
                coin_count[sid] = coin_count.get(sid, 0) + 1

        result[d_iso] = day_assign

    return result
