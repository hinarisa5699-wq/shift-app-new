"""
export.py — Excel / CSV エクスポートモジュール
介護シフト自動作成アプリ

生成されたシフトデータを、整形済みの Excel ファイル (.xlsx) または
CSV ファイルとして出力する。
"""

import calendar
import csv
import io
from datetime import date
from io import BytesIO

import jpholiday
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins
from openpyxl.worksheet.properties import PageSetupProperties

# ---------------------------------------------------------------------------
# 定数: アサインメント → 日本語表示ラベル
# ---------------------------------------------------------------------------
ASSIGNMENT_LABELS = {
    "day_pattern1":    "訪問8:30-17:30",
    "day_pattern2":    "訪問9:00-16:00",
    "day_pattern3":    "訪問午前のみ",
    "day_pattern4":    "訪問午後のみ",
    "early":           "早番7:30-16:30",
    "late":            "遅番9:30-18:30",
    "visit_am":        "デイ午前のみ",
    "visit_pm":        "デイ午後のみ",
    "day_p3_visit_pm": "兼務(訪問→デイ)",
    "visit_am_day_p4": "兼務(デイ→訪問)",
    "cook_early":      "調理①6-8",
    "cook_morning":    "調理②8-13",
    "cook_late":       "調理③12-19",
    "cook_long":       "調理④6-13",
    "cook_mid":        "調理⑤9-15",
    # 旧名の後方互換
    "day_am":          "訪問午前のみ",
    "day_pm":          "訪問午後のみ",
    "day_am_visit_pm": "兼務(訪問→デイ)",
    "visit_am_day_pm": "兼務(デイ→訪問)",
}

# カテゴリごとの背景色 (アサインメントセル)
ASSIGNMENT_FILL = {
    "day_pattern1":    PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
    "day_pattern2":    PatternFill(start_color="BFDBFE", end_color="BFDBFE", fill_type="solid"),
    "day_pattern3":    PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type="solid"),
    "day_pattern4":    PatternFill(start_color="BAE6FD", end_color="BAE6FD", fill_type="solid"),
    "early":           PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
    "late":            PatternFill(start_color="BFDBFE", end_color="BFDBFE", fill_type="solid"),
    "visit_am":        PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
    "visit_pm":        PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
    "day_p3_visit_pm": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
    "visit_am_day_p4": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
    "cook_early":      PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
    "cook_morning":    PatternFill(start_color="FDE68A", end_color="FDE68A", fill_type="solid"),
    "cook_late":       PatternFill(start_color="FCD34D", end_color="FCD34D", fill_type="solid"),
    "cook_long":       PatternFill(start_color="FBBF24", end_color="FBBF24", fill_type="solid"),
    "cook_mid":        PatternFill(start_color="FDE68A", end_color="FDE68A", fill_type="solid"),
    # 旧名の後方互換
    "day_am":          PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type="solid"),
    "day_pm":          PatternFill(start_color="BAE6FD", end_color="BAE6FD", fill_type="solid"),
    "day_am_visit_pm": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
    "visit_am_day_pm": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
}

# 曜日名
WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

# サマリー列ヘッダー (ケア)
# 注: 値の並びは [day_am, day_pm, visit_am, visit_pm, ...] のまま。
# 表記入れ替え（訪問⇄デイ）の要望によりラベルのみ入れ替えている。
SUMMARY_HEADERS = ["訪問午前", "訪問午後", "デイ午前", "デイ午後", "兼務者数", "オンコール"]

# サマリー列ヘッダー (調理)
COOK_SUMMARY_HEADERS = ["調理配置数"]

# ---------------------------------------------------------------------------
# スタイル定義
# ---------------------------------------------------------------------------
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_FONT = Font(name="メイリオ", bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(name="メイリオ", bold=True, size=16)
NORMAL_FONT = Font(name="メイリオ", size=10)
SATURDAY_FILL = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
SUNDAY_FILL = PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid")
ALERT_FILL = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
ALERT_FONT = Font(name="メイリオ", size=10, color="CC0000")
WARNING_HEADER_FILL = PatternFill(start_color="CC0000", end_color="CC0000", fill_type="solid")
WARNING_HEADER_FONT = Font(name="メイリオ", bold=True, color="FFFFFF", size=11)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
CENTER_ALIGN = Alignment(horizontal="center", vertical="center")

# ⑧ 祝日行の背景色
HOLIDAY_FILL = PatternFill(start_color="FFF3E0", end_color="FFF3E0", fill_type="solid")

# ③ 相談員事務スロットラベル
DESK_SLOT_LABELS = ["9-11時", "11-13時", "13-15時", "15-17時"]

# 兼務パターンは休憩はあるが相談業務なし
_NO_COUNSELOR_PATTERNS = {"day_p3_visit_pm", "visit_am_day_p4"}

# デイ午前に寄与するアサインメント
_DAY_AM_SET = {"day_pattern1", "day_pattern2", "day_pattern3", "day_p3_visit_pm",
               "day_am", "day_am_visit_pm", "late"}
# デイ午後に寄与するアサインメント
_DAY_PM_SET = {"day_pattern1", "day_pattern2", "day_pattern4", "visit_am_day_p4",
               "day_pm", "visit_am_day_pm", "early", "late"}
# 訪問午前
_VISIT_AM_SET = {"visit_am", "visit_am_day_p4", "visit_am_day_pm"}
# 訪問午後
_VISIT_PM_SET = {"visit_pm", "day_p3_visit_pm", "day_am_visit_pm"}
# 兼務
_DUAL_SET = {"day_p3_visit_pm", "visit_am_day_p4", "day_am_visit_pm", "visit_am_day_pm"}
# 調理
_COOK_SET = {"cook_early", "cook_morning", "cook_late", "cook_long", "cook_mid"}
_NURSE_PT_NAME_ALIASES = {"看護師", "PT", "理学療法士"}
_NURSE_PT_CODE_ALIASES = {"nurse", "pt"}


def _is_nurse_or_pt_staff(staff: dict) -> bool:
    """看護師/PTはコード優先で判定し、旧名称も受け入れる。"""
    qual_codes = {
        code for code in staff.get("qualification_codes", [])
        if isinstance(code, str) and code
    }
    if qual_codes.intersection(_NURSE_PT_CODE_ALIASES):
        return True

    qual_names = {
        name for name in staff.get("qualifications", [])
        if isinstance(name, str) and name
    }
    return bool(qual_names.intersection(_NURSE_PT_NAME_ALIASES))


# ---------------------------------------------------------------------------
# ヘルパー: 日ごとの配置データを集計する
# ---------------------------------------------------------------------------
def _build_daily_data(shifts_data, staff_list, year, month):
    """
    日付別・職員別の配置マップと、日付別サマリーを構築する。
    """
    num_days = calendar.monthrange(year, month)[1]
    dates = [date(year, month, d) for d in range(1, num_days + 1)]

    assignment_map = {}
    phone_duty_map = {}
    desk_slot_map = {}  # ③ {date_str: {staff_id: [slot_idx, ...]}}
    break_map = {}      # ① {date_str: {staff_id: "12:00"}}
    bath_map = {}       # お風呂当番 {date_str: {staff_id: "中"/"外"}}
    meal_map = {}       # 食事介助 {date_str: {staff_id: "12:00-13:00"}}
    for item in shifts_data:
        d_str = item["date"]
        sid = item["staff_id"]
        asgn = item.get("assignment", "")
        if d_str not in assignment_map:
            assignment_map[d_str] = {}
        assignment_map[d_str][sid] = asgn
        if item.get("is_phone_duty"):
            if d_str not in phone_duty_map:
                phone_duty_map[d_str] = []
            phone_duty_map[d_str].append(item.get("staff_name", f"ID:{sid}"))
        # ③ 相談員事務スロット
        slots = item.get("counselor_desk_slots")
        if slots:
            if d_str not in desk_slot_map:
                desk_slot_map[d_str] = {}
            desk_slot_map[d_str][sid] = slots
        # ① 休憩開始時刻
        bs = item.get("break_start")
        if bs:
            if d_str not in break_map:
                break_map[d_str] = {}
            break_map[d_str][sid] = bs
        # お風呂当番（中/外）
        bath = item.get("bath_role")
        if bath:
            bath_map.setdefault(d_str, {})[sid] = bath
        # 食事介助
        meal = item.get("meal_assist")
        if meal:
            meal_map.setdefault(d_str, {})[sid] = meal

    # ② 看護師/PTはデイ人数カウントから除外
    nurse_pt_ids = set()
    for st in staff_list:
        if _is_nurse_or_pt_staff(st):
            nurse_pt_ids.add(st["id"])

    summary_map = {}
    for d in dates:
        d_str = d.isoformat()
        day_assignments = assignment_map.get(d_str, {})

        day_am = 0
        day_pm = 0
        visit_am = 0
        visit_pm = 0
        dual = 0
        cook_total = 0

        for sid, asgn in day_assignments.items():
            is_nurse_pt = sid in nurse_pt_ids
            if asgn in _DAY_AM_SET and not is_nurse_pt:
                day_am += 1
            if asgn in _DAY_PM_SET and not is_nurse_pt:
                day_pm += 1
            if asgn in _VISIT_AM_SET:
                visit_am += 1
            if asgn in _VISIT_PM_SET:
                visit_pm += 1
            if asgn in _DUAL_SET:
                dual += 1
            if asgn in _COOK_SET:
                cook_total += 1

        summary_map[d_str] = {
            "day_am": day_am,
            "day_pm": day_pm,
            "visit_am": visit_am,
            "visit_pm": visit_pm,
            "dual": dual,
            "cook_total": cook_total,
        }

    return dates, assignment_map, summary_map, phone_duty_map, desk_slot_map, break_map, bath_map, meal_map


# ---------------------------------------------------------------------------
# ヘルパー: 縦＝職員名・横＝日付 のシートを 1 枚書き込む
# ---------------------------------------------------------------------------
def _date_column_fill(d):
    """日付列の背景色（祝日 > 土 > 日）。平日は None。"""
    if jpholiday.is_holiday(d):
        return HOLIDAY_FILL
    weekday_idx = d.weekday()
    if weekday_idx == 5:
        return SATURDAY_FILL
    if weekday_idx == 6:
        return SUNDAY_FILL
    return None


def _staff_name_label(staff: dict, is_cook: bool) -> str:
    """職員名セルの表示（ケアは資格を併記）。"""
    if is_cook:
        return staff["name"]
    quals = staff.get("qualifications", [])
    if quals:
        return f"{staff['name']}\n({'/'.join(quals)})"
    return staff["name"]


def _care_cell_text(d_str, sid, assignment_map, bath_map, desk_slot_map):
    """ケアスタッフ 1 セルの (assignment, 表示テキスト) を組み立てる。"""
    asgn = assignment_map.get(d_str, {}).get(sid, "")
    text = ASSIGNMENT_LABELS.get(asgn, "")

    # お風呂当番（中/外）
    bath_role = bath_map.get(d_str, {}).get(sid)
    if bath_role:
        text += f"\n{bath_role}介助"

    # 休憩時間・食事介助ラベルは表示しない（要望により非表示）

    # ③ 相談員事務スロット
    desk_slots = desk_slot_map.get(d_str, {}).get(sid)
    if desk_slots:
        if set(desk_slots) >= {0, 1, 2, 3}:
            text += "\n相談（終日）"
        else:
            slot_texts = [DESK_SLOT_LABELS[si] for si in desk_slots if si < len(DESK_SLOT_LABELS)]
            if slot_texts:
                text += f"\n相談:{','.join(slot_texts)}"
    return asgn, text


def _write_group_sheet(
    ws,
    group_staff,
    dates,
    year,
    month,
    *,
    assignment_map,
    summary_map,
    phone_duty_map,
    desk_slot_map,
    bath_map,
    warnings_data,
    is_cook,
):
    """1 グループ（介護 or 調理）を「縦＝職員名・横＝日付」で 1 シートに書き込む。"""
    num_days = len(dates)
    name_col = 1
    first_date_col = 2
    last_date_col = first_date_col + num_days - 1
    total_col = last_date_col + 1   # 出勤日数列

    title_label = "調理スタッフ" if is_cook else "介護スタッフ"
    off_token = "cook_off" if is_cook else "off"

    header_font_wrap = Font(name="メイリオ", bold=True, color="FFFFFF", size=11)
    label_font = Font(name="メイリオ", bold=True, size=10)

    # --- タイトル行 ---
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_col)
    title_cell = ws.cell(row=1, column=1, value=f"{year}年{month}月 シフト表（{title_label}）")
    title_cell.font = TITLE_FONT
    title_cell.alignment = CENTER_ALIGN

    header_row1 = 2   # 日付（M/D）
    header_row2 = 3   # 曜日
    data_start_row = 4

    # --- 職員名ヘッダー（2 行ぶち抜き）---
    ws.merge_cells(start_row=header_row1, start_column=name_col, end_row=header_row2, end_column=name_col)
    c = ws.cell(row=header_row1, column=name_col, value="職員名")
    c.font = header_font_wrap
    c.fill = HEADER_FILL
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.border = THIN_BORDER
    ws.cell(row=header_row2, column=name_col).border = THIN_BORDER

    # --- 出勤日数ヘッダー（2 行ぶち抜き）---
    ws.merge_cells(start_row=header_row1, start_column=total_col, end_row=header_row2, end_column=total_col)
    c = ws.cell(row=header_row1, column=total_col, value="出勤\n日数")
    c.font = header_font_wrap
    c.fill = HEADER_FILL
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.border = THIN_BORDER
    ws.cell(row=header_row2, column=total_col).border = THIN_BORDER

    # --- 日付ヘッダー（1 行目＝M/D、2 行目＝曜日 / 祝日名）---
    for i, d in enumerate(dates):
        col = first_date_col + i
        col_fill = _date_column_fill(d)

        c1 = ws.cell(row=header_row1, column=col, value=f"{d.month}/{d.day}")
        c1.font = header_font_wrap
        c1.fill = HEADER_FILL
        c1.alignment = CENTER_ALIGN
        c1.border = THIN_BORDER

        weekday_name = WEEKDAY_NAMES[d.weekday()]
        if jpholiday.is_holiday(d):
            dow_value = f"{weekday_name}\n{jpholiday.is_holiday_name(d)}"
        else:
            dow_value = weekday_name
        c2 = ws.cell(row=header_row2, column=col, value=dow_value)
        c2.font = NORMAL_FONT
        c2.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c2.border = THIN_BORDER
        if col_fill:
            c2.fill = col_fill

    # --- 職員ごとのデータ行 ---
    for r_off, s in enumerate(group_staff):
        row = data_start_row + r_off
        sid = s["id"]

        name_cell = ws.cell(row=row, column=name_col, value=_staff_name_label(s, is_cook))
        name_cell.font = NORMAL_FONT
        name_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        name_cell.border = THIN_BORDER

        work_days = 0
        for i, d in enumerate(dates):
            col = first_date_col + i
            d_str = d.isoformat()
            col_fill = _date_column_fill(d)

            if is_cook:
                asgn = assignment_map.get(d_str, {}).get(sid, "")
                text = ASSIGNMENT_LABELS.get(asgn, "")
            else:
                asgn, text = _care_cell_text(d_str, sid, assignment_map, bath_map, desk_slot_map)

            cell = ws.cell(row=row, column=col, value=text)
            cell.font = NORMAL_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = THIN_BORDER

            if asgn in ASSIGNMENT_FILL:
                cell.fill = ASSIGNMENT_FILL[asgn]
            elif col_fill:
                cell.fill = col_fill

            if asgn not in (off_token, ""):
                work_days += 1

        total_cell = ws.cell(row=row, column=total_col, value=work_days)
        total_cell.font = label_font
        total_cell.alignment = CENTER_ALIGN
        total_cell.border = THIN_BORDER

    # --- サマリー行（日付ごとの配置数。横＝日付に合わせて行として並べる）---
    summary_start_row = data_start_row + len(group_staff)
    if is_cook:
        summary_rows = [("調理配置数", "cook_total", "understaffed_cook")]
    else:
        summary_rows = [
            ("訪問午前", "day_am", "understaffed_day_am"),
            ("訪問午後", "day_pm", "understaffed_day_pm"),
            ("デイ午前", "visit_am", "understaffed_visit_am"),
            ("デイ午後", "visit_pm", "understaffed_visit_pm"),
            ("兼務者数", "dual", "dual_shortage"),
            ("オンコール", "_phone", None),
        ]

    for r_off, (label, key, warn_type) in enumerate(summary_rows):
        row = summary_start_row + r_off
        label_cell = ws.cell(row=row, column=name_col, value=label)
        label_cell.font = label_font
        label_cell.fill = SATURDAY_FILL
        label_cell.alignment = Alignment(horizontal="left", vertical="center")
        label_cell.border = THIN_BORDER

        for i, d in enumerate(dates):
            col = first_date_col + i
            d_str = d.isoformat()
            summary = summary_map.get(d_str, {})

            if key == "_phone":
                names = phone_duty_map.get(d_str, [])
                val = ", ".join(names) if names else ""
            else:
                val = summary.get(key, 0)

            cell = ws.cell(row=row, column=col, value=val)
            cell.font = NORMAL_FONT
            cell.alignment = CENTER_ALIGN
            cell.border = THIN_BORDER

            is_alert = False
            if warn_type:
                for w in warnings_data:
                    if w.get("date") != d_str:
                        continue
                    wt = w.get("warning_type", "")
                    if warn_type == "understaffed_cook":
                        if wt.startswith("understaffed_cook"):
                            is_alert = True
                            break
                    elif wt == warn_type:
                        is_alert = True
                        break
            if is_alert:
                cell.fill = ALERT_FILL
                cell.font = ALERT_FONT

        ws.cell(row=row, column=total_col, value="").border = THIN_BORDER

    # --- 列幅・行高・固定・印刷設定 ---
    ws.column_dimensions[get_column_letter(name_col)].width = 18   # 職員名・資格
    date_width = 14 if is_cook else 13   # 調理は時間ラベルが長い
    for i in range(num_days):
        ws.column_dimensions[get_column_letter(first_date_col + i)].width = date_width
    ws.column_dimensions[get_column_letter(total_col)].width = 7

    ws.row_dimensions[header_row2].height = 30   # 祝日名の折り返し用

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 0    # 横は日付ぶん複数ページに分割
    ws.page_setup.fitToHeight = 1   # 縦（職員）は 1 ページに収める
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins = PageMargins(
        left=0.3, right=0.3, top=0.5, bottom=0.5, header=0.2, footer=0.2
    )
    ws.print_title_rows = "1:3"      # タイトル＋日付見出しを各ページ先頭に繰り返す
    ws.print_title_cols = "A:A"      # 職員名列を各ページ左端に繰り返す
    ws.freeze_panes = ws.cell(row=data_start_row, column=first_date_col).coordinate


# ---------------------------------------------------------------------------
# Excel エクスポート
# ---------------------------------------------------------------------------
def export_excel(
    shifts_data: list,
    warnings_data: list,
    staff_list: list,
    year: int,
    month: int,
    oncall_map: dict = None,
) -> BytesIO:
    """Excel 形式でシフト表を出力する。

    レイアウトは「縦＝職員名・横＝日付」。介護スタッフと調理スタッフを
    別シート（1 枚目＝介護、2 枚目＝調理）に分けて出力する。
    """
    wb = Workbook()

    dates, assignment_map, summary_map, phone_duty_map, desk_slot_map, break_map, bath_map, meal_map = _build_daily_data(
        shifts_data, staff_list, year, month
    )
    # オンコール（電話当番）は出勤と独立した別データで上書き
    if oncall_map is not None:
        phone_duty_map = {d: [n] for d, n in oncall_map.items() if n}

    care_staff = [s for s in staff_list if s.get("department") != "cooking"]
    cook_staff = [s for s in staff_list if s.get("department") == "cooking"]

    # --- 1 枚目: 介護スタッフ ---
    ws_care = wb.active
    ws_care.title = "介護スタッフ"
    _write_group_sheet(
        ws_care, care_staff, dates, year, month,
        assignment_map=assignment_map,
        summary_map=summary_map,
        phone_duty_map=phone_duty_map,
        desk_slot_map=desk_slot_map,
        bath_map=bath_map,
        warnings_data=warnings_data,
        is_cook=False,
    )

    # --- 2 枚目: 調理スタッフ ---
    if cook_staff:
        ws_cook = wb.create_sheet(title="調理スタッフ")
        _write_group_sheet(
            ws_cook, cook_staff, dates, year, month,
            assignment_map=assignment_map,
            summary_map=summary_map,
            phone_duty_map=phone_duty_map,
            desk_slot_map=desk_slot_map,
            bath_map=bath_map,
            warnings_data=warnings_data,
            is_cook=True,
        )

    # --- 警告シート ---
    if warnings_data:
        ws_warn = wb.create_sheet(title="警告一覧")

        warn_headers = ["日付", "種別", "内容"]
        for col_idx, header in enumerate(warn_headers, start=1):
            cell = ws_warn.cell(row=1, column=col_idx, value=header)
            cell.font = WARNING_HEADER_FONT
            cell.fill = WARNING_HEADER_FILL
            cell.alignment = CENTER_ALIGN
            cell.border = THIN_BORDER

        for row_offset, warn in enumerate(warnings_data):
            row = 2 + row_offset
            ws_warn.cell(row=row, column=1, value=warn.get("date", "")).font = NORMAL_FONT
            ws_warn.cell(row=row, column=1).border = THIN_BORDER
            ws_warn.cell(row=row, column=1).alignment = CENTER_ALIGN

            ws_warn.cell(
                row=row, column=2, value=warn.get("warning_type", "")
            ).font = NORMAL_FONT
            ws_warn.cell(row=row, column=2).border = THIN_BORDER
            ws_warn.cell(row=row, column=2).alignment = CENTER_ALIGN

            ws_warn.cell(
                row=row, column=3, value=warn.get("message", "")
            ).font = NORMAL_FONT
            ws_warn.cell(row=row, column=3).border = THIN_BORDER

        ws_warn.column_dimensions["A"].width = 12
        ws_warn.column_dimensions["B"].width = 18
        ws_warn.column_dimensions["C"].width = 50

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# CSV エクスポート
# ---------------------------------------------------------------------------
def export_csv(
    shifts_data: list,
    warnings_data: list,
    staff_list: list,
    year: int,
    month: int,
    oncall_map: dict = None,
) -> str:
    """CSV 形式でシフト表を出力する。

    レイアウトは Excel と同じ「縦＝職員名・横＝日付」。1 ファイル内に
    介護スタッフ・調理スタッフのブロックを順に出力する（CSV はシートを
    持てないため、空行で区切る）。
    """
    dates, assignment_map, summary_map, phone_duty_map, desk_slot_map, break_map, bath_map, meal_map = _build_daily_data(
        shifts_data, staff_list, year, month
    )
    # オンコール（電話当番）は出勤と独立した別データで上書き
    if oncall_map is not None:
        phone_duty_map = {d: [n] for d, n in oncall_map.items() if n}

    care_staff = [s for s in staff_list if s.get("department") != "cooking"]
    cook_staff = [s for s in staff_list if s.get("department") == "cooking"]

    output = io.StringIO()
    writer = csv.writer(output)

    # 日付ヘッダー（M/D(曜)）。各ブロックは 職員名列＋日付列＋出勤日数列。
    date_headers = []
    for d in dates:
        date_headers.append(f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})")

    def _care_cell(d_str, sid):
        asgn = assignment_map.get(d_str, {}).get(sid, "")
        label = ASSIGNMENT_LABELS.get(asgn, "")
        parts = [label] if label else []
        bath_role = bath_map.get(d_str, {}).get(sid)
        if bath_role:
            parts.append(f"{bath_role}介助")
        # 休憩時間は表示しない（要望により非表示）
        meal = meal_map.get(d_str, {}).get(sid)
        if meal:
            parts.append(f"食事:{meal}")
        desk_slots = desk_slot_map.get(d_str, {}).get(sid)
        if desk_slots:
            parts.append("相談（終日）" if set(desk_slots) >= {0, 1, 2, 3}
                         else "相談:" + ",".join(
                             DESK_SLOT_LABELS[si] for si in desk_slots if si < len(DESK_SLOT_LABELS)))
        return asgn, " ".join(parts)

    def _write_block(title, group_staff, is_cook):
        off_token = "cook_off" if is_cook else "off"
        writer.writerow([title])
        writer.writerow(["職員名"] + date_headers + ["出勤日数"])

        for s in group_staff:
            sid = s["id"]
            name = s["name"]
            quals = s.get("qualifications", [])
            if not is_cook and quals:
                name = f"{name}({'/'.join(quals)})"

            cells = []
            work_days = 0
            for d in dates:
                d_str = d.isoformat()
                if is_cook:
                    asgn = assignment_map.get(d_str, {}).get(sid, "")
                    cells.append(ASSIGNMENT_LABELS.get(asgn, ""))
                else:
                    asgn, text = _care_cell(d_str, sid)
                    cells.append(text)
                if asgn not in (off_token, ""):
                    work_days += 1
            writer.writerow([name] + cells + [work_days])

        # サマリー行（日付ごとの配置数）
        if is_cook:
            summary_rows = [("調理配置数", "cook_total")]
        else:
            summary_rows = [
                ("訪問午前", "day_am"),
                ("訪問午後", "day_pm"),
                ("デイ午前", "visit_am"),
                ("デイ午後", "visit_pm"),
                ("兼務者数", "dual"),
                ("オンコール", "_phone"),
            ]
        for label, key in summary_rows:
            cells = []
            for d in dates:
                d_str = d.isoformat()
                if key == "_phone":
                    names = phone_duty_map.get(d_str, [])
                    cells.append(", ".join(names) if names else "")
                else:
                    cells.append(summary_map.get(d_str, {}).get(key, 0))
            writer.writerow([label] + cells + [""])

    _write_block("【介護スタッフ】", care_staff, is_cook=False)
    if cook_staff:
        writer.writerow([])
        _write_block("【調理スタッフ】", cook_staff, is_cook=True)

    csv_string = "\ufeff" + output.getvalue()
    return csv_string
