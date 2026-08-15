"""依頼文39：検証用エクスポート（読み取り専用）。

全職員のマスタ設定・条件設定・配置ルールを CSV に書き出す。
- config.resolve_database_path() のDBを参照（親フォルダ shift.db 優先／本番は /var/data）。
- sqlite3 を read-only(mode=ro) で開き、INSERT/UPDATE/DELETE/マイグレーション/コミットは
  一切行わない（純粋な読み取りのみ）。
- 値は基本そのまま。コード列はヘッダが指定するラベルへ表示変換し、真偽値は true/false。
- CSV はDBと同じディレクトリへ出力（UTF-8 BOM付き＝Excelで文字化けしない）。

使い方:  python export_verification.py
"""
import csv
import json
import os
import sqlite3

from config import resolve_database_path

WD = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}


def tf(v):
    """0/1/None/真偽 → 'true'/'false'。"""
    return "true" if (v not in (0, None, "", "0", False)) else "false"


def weekdays(csv_str):
    """'0,1,3,4' → '月・火・木・金'。空なら ''。"""
    s = (csv_str or "").strip()
    if not s:
        return ""
    return "・".join(WD.get(int(x), x) for x in s.split(",") if x.strip() != "")


def map_value(table, value, default=None):
    return table.get(value, value if default is None else default)


JOB = {"caregiver": "介護", "nurse_rehab": "看護", "cooking": "調理"}
ROLE = {"": "", "manager": "管理者", "sekinin": "サ責", "sekinin_assist": "サ責補佐",
        "executive": "役員"}
GENDER = {"": "未設定", "female": "女性", "male": "男性"}
WEEKEND = {"": "制約なし", "one_off": "土日どちらか休み"}
TIMESLOT = {"full_day": "終日", "am": "午前", "pm": "午後", "": ""}
EXPERIENCE = {"": "", "new": "新人", "veteran": "ベテラン"}
ONOFF = {1: "有効", 0: "無効"}


def fetch_all_dicts(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main():
    db_path = resolve_database_path()
    out_dir = os.path.dirname(db_path)
    # 読み取り専用で開く（書き込み不可を強制）
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    # 相談員資格を持つ staff_id 集合（相談員可の判定）
    cur.execute(
        "SELECT id FROM qualification WHERE code IN ('counselor','social_worker') "
        "OR name IN ('相談員','生活相談員')"
    )
    counselor_qids = {r[0] for r in cur.fetchall()}
    qual_name = {}
    cur.execute("SELECT id, name FROM qualification")
    for qid, qname in cur.fetchall():
        qual_name[qid] = qname

    staff_counselor = set()
    if counselor_qids:
        qmarks = ",".join("?" for _ in counselor_qids)
        cur.execute(
            f"SELECT DISTINCT staff_id FROM staff_qualification WHERE qualification_id IN ({qmarks})",
            tuple(counselor_qids),
        )
        staff_counselor = {r[0] for r in cur.fetchall()}

    # 許可シフトパターン / 休み希望日 / 出勤可能日 を staff_id ごとに集約
    allowed = {}
    for r in fetch_all_dicts(cur, "SELECT staff_id, assignment_code FROM staff_allowed_pattern"):
        allowed.setdefault(r["staff_id"], []).append(r["assignment_code"])
    dayoff = {}
    for r in fetch_all_dicts(cur, "SELECT staff_id, date FROM day_off_request ORDER BY date"):
        dayoff.setdefault(r["staff_id"], []).append(str(r["date"]))
    workable = {}
    for r in fetch_all_dicts(cur, "SELECT staff_id, date FROM staff_workable_date ORDER BY date"):
        workable.setdefault(r["staff_id"], []).append(str(r["date"]))

    # ---- 出力1: staff_export.csv ----
    staff_rows = fetch_all_dicts(cur, "SELECT * FROM staff ORDER BY id")
    cooking_count = sum(1 for s in staff_rows if s.get("staff_group") == "cooking")
    staff_path = os.path.join(out_dir, "staff_export.csv")
    headers = [
        "氏名", "区分", "役割", "雇用形態", "性別",
        "訪問兼務可", "オンコール担当(★)", "相談員可", "入浴介助可",
        "経験区分", "初出勤日",
        "勤務可能曜日", "勤務不可曜日", "勤務可能時間帯",
        "勤務開始", "勤務終了", "許可シフトパターン",
        "土日休み希望", "祝日出勤不可",
        "連勤上限", "週勤務日数_下限", "週勤務日数_上限", "車通勤", "固定枠番号",
        "休み希望日", "出勤可能日",
    ]
    with open(staff_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for s in staff_rows:
            sid = s.get("id")
            w.writerow([
                s.get("name", ""),
                map_value(JOB, s.get("job_category", "")),
                map_value(ROLE, s.get("role", "")),
                s.get("employment_type", ""),
                map_value(GENDER, s.get("gender", "")),
                tf(s.get("can_visit")),
                tf(s.get("has_phone_duty")),
                tf(sid in staff_counselor),
                tf(s.get("can_bath_assist")),
                map_value(EXPERIENCE, s.get("cooking_experience", "")),
                s.get("first_work_date") or "",
                weekdays(s.get("available_days")),
                weekdays(s.get("fixed_days_off")),
                map_value(TIMESLOT, s.get("available_time_slots", "")),
                s.get("work_start_time", "") or "",
                s.get("work_end_time", "") or "",
                ";".join(allowed.get(sid, [])),
                map_value(WEEKEND, s.get("weekend_constraint", "")),
                tf(s.get("holiday_ng")),
                s.get("max_consecutive_days", ""),
                s.get("min_days_per_week", ""),
                s.get("max_days_per_week", ""),
                tf(s.get("car_commute")),
                s.get("parking_slot", "") or "",
                ";".join(dayoff.get(sid, [])),
                ";".join(workable.get(sid, [])),
            ])

    # ---- 出力2: settings_export.csv ----
    settings_rows = fetch_all_dicts(cur, "SELECT * FROM shift_settings ORDER BY id LIMIT 1")
    st = settings_rows[0] if settings_rows else {}
    settings_path = os.path.join(out_dir, "settings_export.csv")
    pairs = [
        ("デイ最低", st.get("min_day_service", "")),
        ("デイ最大", st.get("max_day_service", "")),
        ("在籍9時", st.get("min_staff_at_9", "")),
        ("在籍15時", st.get("min_staff_at_15", "")),
        ("男性午前配置モード", st.get("male_am_constraint_mode", "")),
        ("訪問午前最低", st.get("min_visit_am", "")),
        ("訪問午後最低", st.get("min_visit_pm", "")),
        ("訪問営業曜日", weekdays(st.get("visit_operating_days", ""))),
        ("早番人数", st.get("min_early_staff", "")),
        ("遅番人数", st.get("min_late_staff", "")),
        ("休業曜日", weekdays(st.get("closed_days", "")) or "なし"),
        ("調理最低", st.get("min_cooking_staff", "")),
        ("調理引継ぎ重複(12-13)", st.get("min_cooking_overlap", "")),
        ("調理ペア成立回数目標", st.get("cooking_pair_target", "")),
        ("相談員モード(off/soft/hard)", st.get("counselor_care_mode", "")),
        ("オンコール自動割当", map_value(ONOFF, st.get("phone_duty_enabled", ""))),
        ("連続当番上限", st.get("phone_duty_max_consecutive", "")),
    ]
    with open(settings_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["項目名", "値"])
        for k, v in pairs:
            w.writerow([k, v])

    # ---- 出力3: rules_export.csv ----
    rule_rows = fetch_all_dicts(cur, "SELECT * FROM placement_rule ORDER BY id")
    rules_path = os.path.join(out_dir, "rules_export.csv")
    with open(rules_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ルール名", "種別", "対象資格", "開始", "終了", "最低人数", "ハード/ソフト", "有効"])
        for r in rule_rows:
            try:
                qids = json.loads(r.get("target_qualification_ids_json") or "[]")
            except (ValueError, TypeError):
                qids = []
            qnames = ";".join(qual_name.get(q, str(q)) for q in qids)
            tgt = qnames
            if not tgt and r.get("target_gender"):
                tgt = f"（性別:{r.get('target_gender')}）"
            w.writerow([
                r.get("name", ""),
                r.get("rule_type", ""),
                tgt,
                r.get("time_start", "") or "",
                r.get("time_end", "") or "",
                r.get("min_count", ""),
                "ハード" if r.get("is_hard") else "ソフト",
                tf(r.get("is_active")),
            ])

    con.close()

    print("参照DB:", db_path)
    print("出力1 staff_export.csv   :", staff_path)
    print("出力2 settings_export.csv:", settings_path)
    print("出力3 rules_export.csv   :", rules_path)
    print(f"職員数: {len(staff_rows)}  調理職員数: {cooking_count}  配置ルール件数: {len(rule_rows)}")


if __name__ == "__main__":
    main()
