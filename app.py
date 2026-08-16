"""
app.py — Flask アプリケーション本体
介護シフト自動作成アプリ
"""

import csv
import io
import json
import logging
import os
import re
import shutil
import threading
import uuid
import sqlite3
import zipfile
import calendar
from datetime import date, datetime, timedelta, timezone

# 日本標準時（JST = UTC+9）
JST = timezone(timedelta(hours=9))


def _now_jst():
    """現在のJST日時（tzなしのwall-clock）を返す。"""
    return datetime.now(JST).replace(tzinfo=None)
from io import BytesIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    jsonify,
    flash,
    send_file,
    session,
)

from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash

import jpholiday
from config import Config
from models import (
    db, Staff, DayOffRequest, ShiftSettings, GeneratedShift, ShiftWarning,
    ShiftPattern, Qualification, StaffQualification, PlacementRule, CookingComboRule,
    StaffAllowedPattern, StaffWorkableDate, OncallAssignment, ShiftConfirmation,
    ParkingSlot, ParkingAssignment, ShiftFix,
)
from parking import assign_parking
from solver import (
    generate_shift, assign_oncall, CARE_ASSIGNMENTS, COOK_ASSIGNMENTS,
    _period_from_time_window,
)
from export import (
    export_excel, export_csv, export_pdf, export_pdf_individual,
    export_excel_group_half, export_pdf_from_excel,
    parse_uploaded_shift_excel, parse_shift_cell, state_to_cell_text,
    recompute_warnings_from_shifts, ASSIGNMENT_LABELS, configure_operating_days,
    register_day_off_requests,
)


def _parse_retired_month(raw):
    """退職月の入力（"YYYY-MM" / "YYYY-MM-DD"）を date に変換。空欄は None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def _public_holiday_target(st, year=None, month=None) -> int:
    """職員の公休目標日数。自動算出ONなら平日日数ベース（生成時と同じ）。"""
    if getattr(st, "oncall_only", False):
        # オンコールのみ当番の職員は出勤枠を持たないので公休の目標も課さない
        return 0
    if _is_plan_only_staff(st):
        # 役員・事務は自動作成の対象外なので公休の目標も持たない
        return 0
    settings_obj = ShiftSettings.query.first()
    manual = int(getattr(st, "public_holiday_count", 0) or 0)
    if not settings_obj or not getattr(settings_obj, "auto_public_holidays", False):
        return manual
    if not (year and month):
        return manual
    cal_days = calendar.monthrange(year, month)[1]
    include_hol = bool(getattr(settings_obj, "auto_ph_include_holidays", False))
    fulltime = sum(
        1 for d in range(1, cal_days + 1)
        if date(year, month, d).weekday() < 5
        and not (include_hol and jpholiday.is_holiday(date(year, month, d)))
    )
    week_days = st.max_days_per_week or 5
    shotei = max(0, fulltime - max(0, 5 - week_days) * 4)
    # 出勤可能日(whitelist)を登録している職員は、その月の登録日数が出勤日数の上限
    if (getattr(st, "workable_dates_mode", "only") or "only") == "only":
        _first = date(year, month, 1)
        _last = date(year, month, cal_days)
        _wl = StaffWorkableDate.query.filter(
            StaffWorkableDate.staff_id == st.id,
            StaffWorkableDate.date >= _first,
            StaffWorkableDate.date <= _last,
        ).count()
        if _wl:
            shotei = min(shotei, _wl)
    # 固定休・勤務可能曜日・休業日で物理的に出られない分は差し引く
    avail = {int(x) for x in (st.available_days or "").split(",") if x.strip()}
    fixed = {int(x) for x in (st.fixed_days_off or "").split(",") if x.strip()}
    closed_wd = {
        int(x) for x in (settings_obj.closed_days or "").split(",") if x.strip()
    }
    closed_iso = {
        x.strip() for x in (getattr(settings_obj, "closed_dates", "") or "").split(",")
        if x.strip()
    }
    hol_ng = bool(getattr(st, "holiday_ng", False))
    by_week = {}
    for d in range(1, cal_days + 1):
        dt = date(year, month, d)
        if avail and dt.weekday() not in avail:
            continue
        if dt.weekday() in fixed or dt.weekday() in closed_wd or dt.isoformat() in closed_iso:
            continue
        if hol_ng and jpholiday.is_holiday(dt):
            continue
        by_week[dt.isocalendar()[1]] = by_week.get(dt.isocalendar()[1], 0) + 1
    max_workable = sum(min(n, week_days or 7) for n in by_week.values())
    return max(0, cal_days - min(shotei, max_workable))


def _is_staff_active_in_month(st, year, month) -> bool:
    """その職員が対象年月のシフトに載るか（休職中・退職を判定）。

    退職月が入っていれば「その月まで」は表示する（ユーザー依頼 2026-08:
    「退職月を入れて、退職前は見れるように」）。退職チェックのみで月が空なら常に対象外。
    """
    if getattr(st, "on_leave", False):
        return False
    if not getattr(st, "retired", False):
        return True
    rd = getattr(st, "retired_date", None)
    if rd is None:
        return False
    return (rd.year, rd.month) >= (int(year), int(month))


def _active_staff_for_month(year, month, base_query=None):
    """対象年月に在籍している職員の一覧（id順）。"""
    q = base_query if base_query is not None else Staff.query
    return [
        st for st in q.order_by(Staff.id).all()
        if _is_staff_active_in_month(st, year, month)
    ]


def _parse_wd_counts(raw):
    """曜日ごとの人数設定 "3,3,,3,,2,0" を [int|None]×7 に変換（空/未設定は None）。"""
    out = [None] * 7
    if not raw:
        return out
    parts = str(raw).split(",")
    for wd in range(min(7, len(parts))):
        tok = (parts[wd] or "").strip()
        if tok == "":
            continue
        try:
            n = int(tok)
        except ValueError:
            continue
        if n >= 0:
            out[wd] = n
    return out


def safe_int(value, default=0):
    """安全に int 変換する。失敗時は default を返す。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ShiftPattern.code -> solver assignment code 変換
_PATTERN_CODE_TO_ASSIGNMENT = {
    "care_1": "day_pattern1",
    "care_2": "day_pattern2",
    "care_3": "day_pattern3",
    "care_4": "day_pattern4",
    "care_5": "early",
    "care_6": "late",
    "cooking_1": "cooking_1",
    "cooking_2": "cooking_2",
    "cooking_3": "cooking_3",
    "cooking_4": "cooking_4",
    "cooking_5": "cooking_5",
    "cooking_6": "cooking_6",
    "cooking_7": "cooking_7",
    "cooking_8": "cooking_8",
}

_VALID_ALLOWED_BY_GROUP = {
    "care": set(CARE_ASSIGNMENTS) - {"off"},
    # 調理は「調理シフト種類マスタ」が可変なので、静的セットは DB を読めない場合の
    # フォールバックにとどめる（実際の判定は _valid_allowed_codes を参照）。
    "cooking": (set(COOK_ASSIGNMENTS) | {"cooking_6", "cooking_7", "cooking_8"}) - {"cook_off"},
}

_COUNSELOR_QUALIFICATION_CODES = {"counselor", "social_worker"}
_COUNSELOR_QUALIFICATION_NAMES = {"相談員", "生活相談員"}

# ---------------------------------------------------------------------------
# 職員CSV一括取り込み用の列定義・パーサ
# ---------------------------------------------------------------------------
# CSVヘッダー（この順序で出力。取り込みはヘッダー名で対応づけ）
STAFF_CSV_COLUMNS = [
    "id", "name", "job_category", "role", "employment_type", "gender",
    "can_visit", "can_counsel", "can_bath_assist", "has_phone_duty",
    "oncall_only", "backup_only", "retired", "holiday_ng", "weekend_constraint",
    "work_start_time", "work_end_time",
    "available_time_slots", "available_days", "fixed_days_off",
    "max_consecutive_days", "max_days_per_week", "min_days_per_week",
    "qualifications", "workable_dates",
]

# 真偽値として True と解釈する文字列
_CSV_TRUE_VALUES = {"1", "true", "yes", "y", "○", "◯", "はい", "可", "on"}


def _csv_to_bool(value) -> bool:
    return str(value or "").strip().lower() in _CSV_TRUE_VALUES


def _normalize_staff_group(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("cooking", "調理", "調理スタッフ", "cook"):
        return "cooking"
    return "care"


# 自動作成の対象にしない区分（名前だけ表に出して、予定を手入力する）
PLAN_ONLY_CATEGORIES = ("executive", "office")


def _is_plan_only_staff(st) -> bool:
    """役員・事務（勤務シフトを自動で入れない職員）か。"""
    return (getattr(st, "job_category", "") or "") in PLAN_ONLY_CATEGORIES


# 役員のセルに入れる「休み」。自動作成では勤務を入れないので、
#   休みだけをこのコードで記録する（ユーザー依頼 2026-08）。
EXEC_OFF_CODE = "exec_off"
# 役員の予定（例: "exec:9:00-12:00 デイ面接"）。表にはこの文字をそのまま出す。
EXEC_PLAN_PREFIX = "exec:"

# --- 区分(job_category) / 役割(role) の選択肢とラベル ---
JOB_CATEGORIES = [
    ("caregiver", "介護"),
    ("nurse_rehab", "看護"),
    ("driver", "ドライバー"),
    ("cooking", "調理"),
    ("executive", "役員"),
    ("office", "事務"),
]
JOB_CATEGORY_LABELS = dict(JOB_CATEGORIES)

ROLES = [
    ("", "なし"),
    ("manager", "管理者"),
    ("sekinin", "サ責"),
    ("sekinin_assist", "サ責補佐"),
    ("executive", "役員"),
]
ROLE_LABELS = dict(ROLES)

EMPLOYMENT_TYPES = ["常勤", "時短正社員", "正社員", "パート"]


def _normalize_job_category(value: str) -> str:
    """入力（コード or 日本語ラベル）→ 区分コード。既定は介護職員。"""
    v = str(value or "").strip().lower()
    if v in ("cooking", "調理", "調理スタッフ", "調理師", "cook"):
        return "cooking"
    if v in ("nurse_rehab", "看護師・リハ", "看護師", "看護", "リハ", "リハビリ", "nurse", "pt"):
        return "nurse_rehab"
    if v in ("executive", "役員", "理事"):
        # 役員は自動作成の対象外。名前だけ表に出して予定を手入力する（ユーザー依頼 2026-08）
        return "executive"
    if v in ("office", "事務", "事務員", "事務職員"):
        # 事務も役員と同じく自動作成の対象外。介護の人数にも数えない
        return "office"
    if v in ("driver", "ドライバー", "運転手", "送迎", "運転"):
        # ドライバーは送迎担当。介護の配置人数には数えない（ユーザー依頼 2026-08）
        return "driver"
    return "caregiver"


def _job_category_to_group(job_category: str) -> str:
    """区分 → solver 用の staff_group(care/cooking) に連動。"""
    return "cooking" if job_category == "cooking" else "care"


def _normalize_role(value: str) -> str:
    """入力（コード or 日本語ラベル）→ 役割コード。既定は なし("")。"""
    v = str(value or "").strip()
    table = {
        "": "", "なし": "", "none": "",
        "manager": "manager", "管理者": "manager",
        "sekinin": "sekinin", "サ責": "sekinin", "サービス提供責任者": "sekinin",
        "sekinin_assist": "sekinin_assist", "サ責補佐": "sekinin_assist",
        "executive": "executive", "役員": "executive", "理事": "executive",
    }
    return table.get(v, table.get(v.lower(), ""))


def _normalize_cooking_experience(value: str) -> str:
    """調理スタッフの経験区分（依頼文28）。入力 → "new"/"veteran"/""（未設定）。"""
    v = str(value or "").strip().lower()
    table = {
        "": "", "未設定": "", "none": "",
        "new": "new", "新人": "new", "rookie": "new",
        "veteran": "veteran", "ベテラン": "veteran", "vet": "veteran",
    }
    return table.get(v, table.get(str(value or "").strip(), ""))


def _parse_first_work_date(value: str):
    """初出勤日（依頼文36）の入力 "YYYY-MM-DD" → date。空欄/不正は None。"""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _split_multi(value: str) -> list[str]:
    """; / 、/ , / ・ など複数区切りを許容して分割する。"""
    raw = str(value or "")
    for sep in ("；", ";", "、", "・", "／", "/", "|"):
        raw = raw.replace(sep, ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_qualification_ids(tokens: list[str]) -> tuple[list[int], list[str]]:
    """資格コードまたは日本語名称のリスト → (資格IDリスト, 未知トークン)。"""
    by_code = {q.code: q.id for q in Qualification.query.all()}
    by_name = {q.name: q.id for q in Qualification.query.all()}
    ids: list[int] = []
    unknown: list[str] = []
    for t in tokens:
        qid = by_code.get(t) or by_name.get(t)
        if qid is None:
            unknown.append(t)
        elif qid not in ids:
            ids.append(qid)
    return ids, unknown


def _counselor_qual_id():
    """資格「相談員」(counselor) のID。無ければ None。"""
    q = Qualification.query.filter_by(code="counselor").first()
    return q.id if q else None


def _staff_has_counselor(staff_id) -> bool:
    """職員が相談員資格を持つか（=相談員可）。"""
    qid = _counselor_qual_id()
    if qid is None:
        return False
    return (
        StaffQualification.query.filter_by(staff_id=staff_id, qualification_id=qid).first()
        is not None
    )


def _set_counselor(staff_id, enabled: bool) -> None:
    """相談員可チェックを既存の counselor 資格へ連動させる（付与/削除）。"""
    qid = _counselor_qual_id()
    if qid is None:
        return
    exists = StaffQualification.query.filter_by(
        staff_id=staff_id, qualification_id=qid
    ).first()
    if enabled and not exists:
        db.session.add(StaffQualification(staff_id=staff_id, qualification_id=qid))
    elif not enabled and exists:
        db.session.delete(exists)


def _valid_allowed_codes(staff_group):
    """許可シフトパターンとして保存してよいコード集合。

    調理は種類マスタ(ShiftPattern staff_group='cooking')をユーザーが自由に追加できる。
    静的セットで判定すると、後から追加した種類（7:00-15:00 等）はチェックしても
    保存時に捨てられ、solver へ渡らない＝そのシフトが一生割り当たらない。
    そのためマスタの現在のコードを毎回読み直す。
    """
    base = _VALID_ALLOWED_BY_GROUP.get(staff_group, set())
    if staff_group != "cooking":
        return base
    try:
        codes = {
            p.code for p in ShiftPattern.query.filter_by(staff_group="cooking").all()
            if p.code
        }
    except Exception:  # アプリコンテキスト外・DB未作成時は静的セットで判定
        return base
    return (base | codes) - {"cook_off"}


def normalize_allowed_pattern_codes(raw_codes, staff_group):
    """フォーム入力の allowed_patterns を solver が扱うコードへ正規化する。"""
    valid_codes = _valid_allowed_codes(staff_group)
    normalized = []
    seen = set()

    for raw in raw_codes:
        code = (raw or "").strip()
        if not code:
            continue
        mapped = _PATTERN_CODE_TO_ASSIGNMENT.get(code, code)
        if mapped not in valid_codes:
            continue
        if mapped in seen:
            continue
        seen.add(mapped)
        normalized.append(mapped)

    return normalized


# ---------------------------------------------------------------------------
# DBマイグレーション: 既存テーブルに新カラムを追加
# ---------------------------------------------------------------------------
def _run_migrations(app):
    """SQLiteは db.create_all() で既存テーブルにカラム追加できないため ALTER TABLE で対応"""
    db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return  # DB未作成ならスキップ（create_all で作られる）

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Staff テーブル
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(staff)").fetchall()]
    if "staff_group" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN staff_group VARCHAR(20) NOT NULL DEFAULT 'care'")
    if "has_phone_duty" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN has_phone_duty BOOLEAN DEFAULT 0")
    if "gender" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN gender VARCHAR(10) NOT NULL DEFAULT ''")
    if "weekend_constraint" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN weekend_constraint VARCHAR(20) NOT NULL DEFAULT ''")
    if "min_days_per_week" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN min_days_per_week INTEGER DEFAULT 0")
    if "holiday_ng" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN holiday_ng BOOLEAN DEFAULT 0")
    if "on_leave" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN on_leave BOOLEAN NOT NULL DEFAULT 0")
    if "public_holiday_count" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN public_holiday_count INTEGER NOT NULL DEFAULT 0")
    if "retired" not in columns:
        # 退職（在籍していない）。履歴は残したまま対象外にする
        cursor.execute("ALTER TABLE staff ADD COLUMN retired BOOLEAN NOT NULL DEFAULT 0")
    if "retired_date" not in columns:
        # 退職月（この月まではシフト表に出す）
        cursor.execute("ALTER TABLE staff ADD COLUMN retired_date DATE")
    if "backup_only" not in columns:
        # 応援職員（人手が足りないときだけ入れる）
        cursor.execute("ALTER TABLE staff ADD COLUMN backup_only BOOLEAN NOT NULL DEFAULT 0")
    if "oncall_only" not in columns:
        # オンコールのみ当番（出勤シフトは割り当てない）
        cursor.execute("ALTER TABLE staff ADD COLUMN oncall_only BOOLEAN NOT NULL DEFAULT 0")
    if "required_days" not in columns:
        # 必ず出勤する曜日
        cursor.execute("ALTER TABLE staff ADD COLUMN required_days VARCHAR(50) NOT NULL DEFAULT ''")
    if "oncall_when_off_ok" not in columns:
        # 出勤していない日でもオンコールを持てる例外職員
        cursor.execute("ALTER TABLE staff ADD COLUMN oncall_when_off_ok BOOLEAN NOT NULL DEFAULT 0")
    if "workable_dates_mode" not in columns:
        # 出勤可能日の扱い: only=その日しか出勤しない / extra=通常に加えて必ず出勤
        cursor.execute(
            "ALTER TABLE staff ADD COLUMN workable_dates_mode VARCHAR(10) NOT NULL DEFAULT 'only'"
        )
    # --- v3: 区分・役割・入浴介助可・勤務時間 ---
    if "job_category" not in columns:
        cursor.execute(
            "ALTER TABLE staff ADD COLUMN job_category VARCHAR(20) NOT NULL DEFAULT 'caregiver'"
        )
        # 既存データのバックフィル（この列追加時に一度だけ実行）
        cursor.execute("UPDATE staff SET job_category='cooking' WHERE staff_group='cooking'")
        # ケアのうち看護師/PT資格保持者は「看護師・リハ」、それ以外は「介護職員」
        cursor.execute(
            """
            UPDATE staff SET job_category='nurse_rehab'
            WHERE staff_group='care' AND id IN (
                SELECT sq.staff_id FROM staff_qualification sq
                JOIN qualification q ON q.id = sq.qualification_id
                WHERE q.code IN ('nurse', 'pt')
            )
            """
        )
    if "role" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT ''")
        # 雇用形態='管理者' を 役割='manager' へ移し、雇用形態は常勤へ寄せる
        cursor.execute("UPDATE staff SET role='manager' WHERE employment_type='管理者'")
        cursor.execute("UPDATE staff SET employment_type='常勤' WHERE employment_type='管理者'")
    if "can_bath_assist" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN can_bath_assist BOOLEAN DEFAULT 0")
    if "work_start_time" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN work_start_time VARCHAR(5) NOT NULL DEFAULT ''")
    if "work_end_time" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN work_end_time VARCHAR(5) NOT NULL DEFAULT ''")
    # --- 駐車場（依頼文24）---
    if "car_commute" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN car_commute BOOLEAN DEFAULT 0")
    if "parking_slot" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN parking_slot VARCHAR(10) NOT NULL DEFAULT ''")
    # --- 調理スタッフの経験区分（依頼文28・新人/ベテラン）---
    if "cooking_experience" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN cooking_experience VARCHAR(10) NOT NULL DEFAULT ''")
    # --- 初出勤日（依頼文36・任意・NULL許容）---
    if "first_work_date" not in columns:
        cursor.execute("ALTER TABLE staff ADD COLUMN first_work_date DATE")

    # ShiftSettings テーブル
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(shift_settings)").fetchall()]
    # 階別の営業曜日（0=月〜6=日）。既定は従来ハードコードされていた運用そのまま。
    if "floor3_day_service_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN floor3_day_service_days VARCHAR(50) NOT NULL DEFAULT '1,4,6'")
    if "floor3_visit_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN floor3_visit_days VARCHAR(50) NOT NULL DEFAULT '0,3'")
    if "floor2_day_service_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN floor2_day_service_days VARCHAR(50) NOT NULL DEFAULT '0,3,5'")
    if "floor2_visit_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN floor2_visit_days VARCHAR(50) NOT NULL DEFAULT '1,4'")
    if "external_day_service_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN external_day_service_days VARCHAR(50) NOT NULL DEFAULT '2'")
    # 以下3つは階別設定からの派生値（保存時に自動算出）
    if "visit_operating_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN visit_operating_days VARCHAR(50) DEFAULT '0,1,3,4'")
    if "no_day_service_days" not in columns:
        # デイは水曜のみ無し（＝外部デイの日）
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN no_day_service_days VARCHAR(50) NOT NULL DEFAULT '2'")
    if "day_service_operating_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN day_service_operating_days VARCHAR(50) NOT NULL DEFAULT '0,1,3,4,5,6'")
    if "closed_dates" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN closed_dates TEXT NOT NULL DEFAULT ''")
    if "min_cooking_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_cooking_staff INTEGER DEFAULT 1")
    if "min_early_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_early_staff INTEGER DEFAULT 1")
    if "min_late_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_late_staff INTEGER DEFAULT 1")
    if "min_cooking_overlap" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_cooking_overlap INTEGER DEFAULT 2")
    if "breakfast_off_start" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN breakfast_off_start VARCHAR(10) NOT NULL DEFAULT ''")
    if "breakfast_off_end" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN breakfast_off_end VARCHAR(10) NOT NULL DEFAULT ''")
    if "am_preferred_gender" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN am_preferred_gender VARCHAR(10) DEFAULT ''")
    if "phone_duty_enabled" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN phone_duty_enabled BOOLEAN DEFAULT 0")
    if "phone_duty_max_consecutive" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN phone_duty_max_consecutive INTEGER DEFAULT 1")
    if "male_am_constraint_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN male_am_constraint_mode VARCHAR(10) DEFAULT 'hard'")
    # 依頼文35: 相談員ローテーション（counselor_desk_enabled / counselor_desk_count）は
    #   機能ごと削除。既存DBの当該カラムは未使用のまま残置（SQLiteのDROP COLUMN回避）。
    # 調理：新人×ベテランのペア成立回数の目標値（依頼文28）
    if "cooking_pair_target" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN cooking_pair_target INTEGER DEFAULT 0")
    # 相談員の介護業務参加モード（依頼文32・既定 off）
    if "counselor_care_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN counselor_care_mode VARCHAR(10) NOT NULL DEFAULT 'off'")
    # 依頼文40: 中介助/外介助の最低人数・連日回避モード・早遅連日回避モード
    if "min_bath_mid" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_bath_mid INTEGER DEFAULT 0")
    if "min_bath_out" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_bath_out INTEGER DEFAULT 0")
    if "bath_role_alt_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN bath_role_alt_mode VARCHAR(10) NOT NULL DEFAULT 'off'")
    if "early_late_alt_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN early_late_alt_mode VARCHAR(10) NOT NULL DEFAULT 'off'")
    # 遅番を中介助とするモード（off/soft/hard・既定hard）
    if "late_as_mid_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN late_as_mid_mode VARCHAR(10) NOT NULL DEFAULT 'hard'")
    # 公休日数の自動算出（法定労働時間ベース）
    if "auto_public_holidays" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN auto_public_holidays BOOLEAN NOT NULL DEFAULT 0")
    if "daily_work_hours" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN daily_work_hours REAL NOT NULL DEFAULT 8.0")
    # 依頼文41: 遅番×オンコール禁止モード・訪問回数の平等化モード
    if "late_oncall_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN late_oncall_mode VARCHAR(10) NOT NULL DEFAULT 'off'")
    if "visit_fairness_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN visit_fairness_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    if "visit_fairness_max" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN visit_fairness_max INTEGER NOT NULL DEFAULT 1")
    if "nurse_early_late_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN nurse_early_late_mode VARCHAR(10) NOT NULL DEFAULT 'hard'")
    if "late_consecutive_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN late_consecutive_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    if "late_fairness_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN late_fairness_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    # 依頼文42-43: 早番の連日回避・早番/遅番/オンコール回数の平等化
    if "early_consecutive_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN early_consecutive_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    if "early_fairness_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN early_fairness_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    if "early_fairness_max" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN early_fairness_max INTEGER NOT NULL DEFAULT 1")
    if "late_fairness_max" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN late_fairness_max INTEGER NOT NULL DEFAULT 1")
    if "oncall_fairness_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN oncall_fairness_mode VARCHAR(10) NOT NULL DEFAULT 'soft'")
    if "oncall_fairness_max" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN oncall_fairness_max INTEGER NOT NULL DEFAULT 1")
    if "auto_ph_include_holidays" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN auto_ph_include_holidays BOOLEAN NOT NULL DEFAULT 0")
    if "viewer_password_hash" not in columns:
        # 閲覧専用ページのパスワード（ハッシュ）
        cursor.execute(
            "ALTER TABLE shift_settings ADD COLUMN viewer_password_hash VARCHAR(255) NOT NULL DEFAULT ''"
        )
    if "office_password_hash" not in columns:
        # 事務用ページのパスワード（ハッシュ）
        cursor.execute(
            "ALTER TABLE shift_settings ADD COLUMN office_password_hash VARCHAR(255) NOT NULL DEFAULT ''"
        )
    if "exec_password_hash" not in columns:
        # 役員用ページのパスワード（ハッシュ）
        cursor.execute(
            "ALTER TABLE shift_settings ADD COLUMN exec_password_hash VARCHAR(255) NOT NULL DEFAULT ''"
        )
    if "oncall_requires_work" not in columns:
        # オンコールは出勤している職員にだけ割り当てる（既定ON）
        cursor.execute(
            "ALTER TABLE shift_settings ADD COLUMN oncall_requires_work BOOLEAN NOT NULL DEFAULT 1"
        )
    if "care_min_by_weekday" not in columns:
        # 曜日ごとの介護配置人数（最低/最大）。空=未設定（従来動作）
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN care_min_by_weekday VARCHAR(50) NOT NULL DEFAULT ''")
    if "care_max_by_weekday" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN care_max_by_weekday VARCHAR(50) NOT NULL DEFAULT ''")

    # GeneratedShift テーブル
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(generated_shift)").fetchall()]
    if "shift_pattern_code" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN shift_pattern_code VARCHAR(30)")
    if "is_phone_duty" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN is_phone_duty BOOLEAN DEFAULT 0")
    if "counselor_desk_slots" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN counselor_desk_slots TEXT")
    if "visit_slot" not in columns:
        # 訪問へ出る時間帯（am/pm）。シフト本体とは別に持つ
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN visit_slot VARCHAR(5)")
    if "break_start" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN break_start VARCHAR(5)")
    if "bath_role" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN bath_role VARCHAR(5)")
    if "meal_assist" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN meal_assist VARCHAR(20)")

    # ShiftPattern テーブル — 新カラム追加
    tables = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "shift_pattern" in tables:
        columns = [row[1] for row in cursor.execute("PRAGMA table_info(shift_pattern)").fetchall()]
        if "period" not in columns:
            cursor.execute("ALTER TABLE shift_pattern ADD COLUMN period VARCHAR(10) DEFAULT 'full'")
        if "covers_am" not in columns:
            cursor.execute("ALTER TABLE shift_pattern ADD COLUMN covers_am BOOLEAN DEFAULT 1")
        if "covers_pm" not in columns:
            cursor.execute("ALTER TABLE shift_pattern ADD COLUMN covers_pm BOOLEAN DEFAULT 1")
        if "counts_as_cooking" not in columns:
            # 調理の充足人数（時間帯カバレッジ）に数えるか。既定は数える。
            cursor.execute(
                "ALTER TABLE shift_pattern ADD COLUMN counts_as_cooking BOOLEAN DEFAULT 1"
            )
            # 「事務」を含む調理種類は初期値を「数えない」にする（例: ⑤9-15 事務）。
            #   調理シフト表には載るが調理はしないため、昼夜の充足に数えると
            #   実際は調理0人の日を「足りている」と誤判定する。
            cursor.execute(
                "UPDATE shift_pattern SET counts_as_cooking=0 "
                "WHERE staff_group='cooking' AND label LIKE '%事務%'"
            )
        # 調理フォールバック用: 9:00-16:00 の調理パターンを保証（1人で昼夜をまかなう）
        cursor.execute(
            "SELECT COUNT(*) FROM shift_pattern "
            "WHERE staff_group='cooking' AND ("
            "  (start_time='09:00' AND end_time='16:00') OR code='cooking_6')"
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "SELECT COALESCE(MAX(display_order), 0) + 1 FROM shift_pattern "
                "WHERE staff_group='cooking'"
            )
            _co = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO shift_pattern "
                "(code, staff_group, label, start_time, end_time, has_break, "
                " break_minutes, display_order, period, covers_am, covers_pm) "
                "VALUES ('cooking_6','cooking','(6) 9:00-16:00','09:00','16:00',0,0,?,'full',1,1)",
                (_co,),
            )
        # 池田さん向け 6:00-12:00(⑦) / 13:00-19:00(⑧) パターンを保証（休憩なし）
        for _code, _label, _st, _et in (
            ("cooking_7", "(7) 6:00-12:00", "06:00", "12:00"),
            ("cooking_8", "(8) 13:00-19:00", "13:00", "19:00"),
        ):
            cursor.execute(
                "SELECT COUNT(*) FROM shift_pattern WHERE staff_group='cooking' AND code=?",
                (_code,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "SELECT COALESCE(MAX(display_order), 0) + 1 FROM shift_pattern "
                    "WHERE staff_group='cooking'"
                )
                _co2 = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO shift_pattern "
                    "(code, staff_group, label, start_time, end_time, has_break, "
                    " break_minutes, display_order, period, covers_am, covers_pm) "
                    "VALUES (?,'cooking',?,?,?,0,0,?,'full',1,1)",
                    (_code, _label, _st, _et, _co2),
                )
        # ※ 7:00-15:00（朝食あり日の1人勤務用）は _sync_cooking_patterns() で保証する
        #   （新規DB・既存DBの双方に効かせるため）。

    # 池田さん向け組み合わせ（⑦6-12＋③12-19 / ④6-13＋⑧13-19）を保証。無ければ追加。
    if "cooking_combo_rule" in tables:
        for _name, _pats in (
            ("池田朝(⑦6-12)+夜(③12-19)", '["cooking_7", "cooking_3"]'),
            ("朝(④6-13)+池田夜(⑧13-19)", '["cooking_4", "cooking_8"]'),
        ):
            cursor.execute(
                "SELECT COUNT(*) FROM cooking_combo_rule WHERE allowed_patterns_json=?",
                (_pats,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "INSERT INTO cooking_combo_rule (name, allowed_patterns_json, is_active) "
                    "VALUES (?,?,1)",
                    (_name, _pats),
                )

    # GeneratedShift: day_am → day_pattern3, day_pm → day_pattern4 リネーム
    if "generated_shift" in tables:
        cursor.execute("UPDATE generated_shift SET assignment = 'day_pattern3' WHERE assignment = 'day_am'")
        cursor.execute("UPDATE generated_shift SET assignment = 'day_pattern4' WHERE assignment = 'day_pm'")
        cursor.execute("UPDATE generated_shift SET assignment = 'day_p3_visit_pm' WHERE assignment = 'day_am_visit_pm'")
        cursor.execute("UPDATE generated_shift SET assignment = 'visit_am_day_p4' WHERE assignment = 'visit_am_day_pm'")

    # NOTE: 看護師/PT制約・電話当番設定はユーザーがUIから変更可能。
    # 起動時に強制上書きしない（設定変更が再起動で元に戻るバグを防止）。

    # CRIT-4: min_staff_at_9 / min_staff_at_15 カラム追加
    if "shift_settings" in tables:
        columns = [row[1] for row in cursor.execute("PRAGMA table_info(shift_settings)").fetchall()]
        if "min_staff_at_9" not in columns:
            cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_staff_at_9 INTEGER DEFAULT 4")
        if "min_staff_at_15" not in columns:
            cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_staff_at_15 INTEGER DEFAULT 4")
        if "max_day_service" not in columns:
            cursor.execute("ALTER TABLE shift_settings ADD COLUMN max_day_service INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def _merge_qualification_records(source: Qualification, target: Qualification) -> None:
    """重複した資格レコードをtargetへ統合する。"""
    if source.id == target.id:
        return

    target_staff_ids = {
        row.staff_id
        for row in StaffQualification.query.filter_by(qualification_id=target.id).all()
    }
    for row in StaffQualification.query.filter_by(qualification_id=source.id).all():
        if row.staff_id in target_staff_ids:
            db.session.delete(row)
            continue
        row.qualification_id = target.id
        target_staff_ids.add(row.staff_id)

    for rule in PlacementRule.query.all():
        qual_ids = json.loads(rule.target_qualification_ids_json or "[]")
        replaced_ids = []
        for qual_id in qual_ids:
            resolved_id = target.id if qual_id == source.id else qual_id
            if resolved_id not in replaced_ids:
                replaced_ids.append(resolved_id)
        if replaced_ids != qual_ids:
            rule.target_qualification_ids_json = json.dumps(replaced_ids)

    db.session.delete(source)


def _normalize_qualifications() -> None:
    """旧DBの生活相談員マスタを現行の相談員マスタへ寄せる。"""
    qualifications = Qualification.query.order_by(Qualification.id).all()
    counselor_candidates = [
        q for q in qualifications
        if q.code in _COUNSELOR_QUALIFICATION_CODES
        or q.name in _COUNSELOR_QUALIFICATION_NAMES
    ]

    if not counselor_candidates:
        return

    primary = next(
        (q for q in counselor_candidates if q.code == "counselor"),
        counselor_candidates[0],
    )
    primary.code = "counselor"
    primary.name = "相談員"
    primary.display_order = 1

    for candidate in counselor_candidates:
        if candidate.id != primary.id:
            _merge_qualification_records(candidate, primary)


def _build_staff_qualification_maps() -> tuple[dict[int, list[int]], dict[int, list[str]], dict[int, list[str]]]:
    """職員ごとの資格ID・名称・コード一覧をまとめて返す。"""
    qualification_name_map = {q.id: q.name for q in Qualification.query.all()}
    qualification_code_map = {q.id: q.code for q in Qualification.query.all()}

    staff_qual_ids: dict[int, list[int]] = {}
    staff_qual_names: dict[int, list[str]] = {}
    staff_qual_codes: dict[int, list[str]] = {}
    for sq in StaffQualification.query.order_by(StaffQualification.id).all():
        staff_qual_ids.setdefault(sq.staff_id, []).append(sq.qualification_id)

        qual_name = qualification_name_map.get(sq.qualification_id)
        if qual_name:
            staff_qual_names.setdefault(sq.staff_id, []).append(qual_name)

        qual_code = qualification_code_map.get(sq.qualification_id)
        if qual_code:
            staff_qual_codes.setdefault(sq.staff_id, []).append(qual_code)

    return staff_qual_ids, staff_qual_names, staff_qual_codes


# ---------------------------------------------------------------------------
# ShiftPattern 初期データ投入
# ---------------------------------------------------------------------------
_INITIAL_PATTERNS = [
    {"code": "care_1", "staff_group": "care", "label": "① 8:30-17:30",
     "start_time": "08:30", "end_time": "17:30", "has_break": False, "break_minutes": 0,
     "display_order": 1, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "care_2", "staff_group": "care", "label": "② 9:00-16:00",
     "start_time": "09:00", "end_time": "16:00", "has_break": False, "break_minutes": 0,
     "display_order": 2, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "care_3", "staff_group": "care", "label": "③ 8:30-12:30",
     "start_time": "08:30", "end_time": "12:30", "has_break": False, "break_minutes": 0,
     "display_order": 3, "period": "am", "covers_am": True, "covers_pm": False},
    {"code": "care_4", "staff_group": "care", "label": "④ 13:30-17:30",
     "start_time": "13:30", "end_time": "17:30", "has_break": False, "break_minutes": 0,
     "display_order": 4, "period": "pm", "covers_am": False, "covers_pm": True},
    {"code": "care_5", "staff_group": "care", "label": "⑤ 早番 7:30-16:30",
     "start_time": "07:30", "end_time": "16:30", "has_break": False, "break_minutes": 0,
     "display_order": 10, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "care_6", "staff_group": "care", "label": "⑥ 遅番 9:30-18:30",
     "start_time": "09:30", "end_time": "18:30", "has_break": False, "break_minutes": 0,
     "display_order": 11, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "cooking_1", "staff_group": "cooking", "label": "(1) 6:00-8:00",
     "start_time": "06:00", "end_time": "08:00", "has_break": False, "break_minutes": 0,
     "display_order": 5, "period": "full", "covers_am": True, "covers_pm": False},
    {"code": "cooking_2", "staff_group": "cooking", "label": "(2) 8:00-13:00",
     "start_time": "08:00", "end_time": "13:00", "has_break": False, "break_minutes": 0,
     "display_order": 6, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "cooking_3", "staff_group": "cooking", "label": "(3) 12:00-19:00",
     "start_time": "12:00", "end_time": "19:00", "has_break": False, "break_minutes": 0,
     "display_order": 7, "period": "full", "covers_am": False, "covers_pm": True},
    {"code": "cooking_4", "staff_group": "cooking", "label": "(4) 6:00-13:00",
     "start_time": "06:00", "end_time": "13:00", "has_break": False, "break_minutes": 0,
     "display_order": 8, "period": "full", "covers_am": True, "covers_pm": True},
    {"code": "cooking_5", "staff_group": "cooking", "label": "(5) 9:00-15:00",
     "start_time": "09:00", "end_time": "15:00", "has_break": False, "break_minutes": 0,
     "display_order": 9, "period": "full", "covers_am": True, "covers_pm": True},
]

# ---------------------------------------------------------------------------
# Qualification 初期データ
# ---------------------------------------------------------------------------
_INITIAL_QUALIFICATIONS = [
    {"code": "counselor", "name": "相談員", "display_order": 1},
    {"code": "nurse", "name": "看護師", "display_order": 2},
    {"code": "pt", "name": "PT", "display_order": 3},
    {"code": "care_worker", "name": "介護福祉士", "display_order": 4},
    {"code": "beginner", "name": "初任者研修", "display_order": 5},
    {"code": "chef", "name": "調理師", "display_order": 6},
    {"code": "practitioner_training", "name": "実務者研修", "display_order": 7},
    {"code": "service_manager", "name": "サ責", "display_order": 8},
    {"code": "service_manager_assist", "name": "サ責補佐", "display_order": 9},
]

# ---------------------------------------------------------------------------
# PlacementRule 初期データ
# ---------------------------------------------------------------------------
_INITIAL_PLACEMENT_RULES = [
    {
        "name": "相談員 午前1名以上",
        "rule_type": "qualification_min",
        "target_qualification_ids_json": "[]",  # 初期化後にID設定
        "target_gender": "",
        "period": "am",
        "min_count": 1,
        "is_hard": True,
        "penalty_weight": 100,
        "_qual_code": "counselor",
    },
    {
        "name": "相談員 午後1名以上",
        "rule_type": "qualification_min",
        "target_qualification_ids_json": "[]",
        "target_gender": "",
        "period": "pm",
        "min_count": 1,
        "is_hard": True,
        "penalty_weight": 100,
        "_qual_code": "counselor",
    },
    # 依頼文18-A: 「看護師/PT 9-16時 1名以上」ルールは廃止。
    #   看護師の配置条件は solver 内の「その日の看護師勤務 合計2時間以上」に統一。
    {
        "name": "男性 午前1名以上",
        "rule_type": "gender_min",
        "target_qualification_ids_json": "[]",
        "target_gender": "male",
        "period": "am",
        "min_count": 1,
        "is_hard": True,
        "penalty_weight": 100,
    },
]


# ---------------------------------------------------------------------------
# CookingComboRule 初期データ
# ---------------------------------------------------------------------------
# 調理の1日の組み合わせ（次の4通りのいずれか）
#   ・①②③⑤  ・①②③  ・④③⑤  ・④③
#   （③12-19は毎日必須。朝は「①+②」か「④」。⑤9-15は任意で追加）
_COOKING_COMBOS = [
    ["cooking_1", "cooking_2", "cooking_3", "cooking_5"],  # ①②③⑤
    ["cooking_1", "cooking_2", "cooking_3"],              # ①②③
    ["cooking_4", "cooking_3", "cooking_5"],                   # ④③⑤
    ["cooking_4", "cooking_3"],                               # ④③
]

_INITIAL_COOKING_COMBO = {
    "name": "調理の日単位組み合わせ",
    "allowed_patterns_json": json.dumps(_COOKING_COMBOS),
    "is_active": True,
}


# ---------------------------------------------------------------------------
# 調理シフトパターンの整合（既存DBにも変更1〜3を反映・冪等）
# ---------------------------------------------------------------------------
# 旧調理コード → 新コード（依頼文21: cooking_* に統一）
_COOK_CODE_MIGRATE = {
    "cook_early": "cooking_1",
    "cook_morning": "cooking_2",
    "cook_late": "cooking_3",
    "cook_long": "cooking_4",
    "cook_mid": "cooking_5",
}
# 既存4組み合わせの内容→名前（A=①②③ / B=④③ / C=①②③⑤ / D=④③⑤）
_COMBO_NAME_BY_CONTENT = {
    frozenset(["cooking_1", "cooking_2", "cooking_3"]): "A",
    frozenset(["cooking_4", "cooking_3"]): "B",
    frozenset(["cooking_1", "cooking_2", "cooking_3", "cooking_5"]): "C",
    frozenset(["cooking_4", "cooking_3", "cooking_5"]): "D",
}


def _sync_cooking_patterns():
    """調理マスタの整合・移行（起動ごとに冪等実行）。依頼文21対応。
    - 調理5種類(ShiftPattern cooking_1〜5)が無ければ既定で作成（既存は上書きしない＝編集を尊重）。
    - 旧コード cook_*→cooking_* を GeneratedShift・CookingComboRule に移行。
    - 組み合わせを「1組=1行（name=A/B/C/D…＋種類コードのフラットリスト）」へ正規化。
    """
    changed = False

    # 調理5種類: 無ければ作成（既存は上書きしない）
    cook_defs = {
        "cooking_1": ("① 6:00-8:00", "06:00", "08:00", 5),
        "cooking_2": ("② 8:00-13:00", "08:00", "13:00", 6),
        "cooking_3": ("③ 12:00-19:00", "12:00", "19:00", 7),
        "cooking_4": ("④ 6:00-13:00", "06:00", "13:00", 8),
        "cooking_5": ("⑤ 9:00-15:00", "09:00", "15:00", 9),
    }
    for code, (label, start, end, order) in cook_defs.items():
        if ShiftPattern.query.filter_by(code=code).first() is None:
            db.session.add(ShiftPattern(
                code=code, staff_group="cooking", label=label,
                start_time=start, end_time=end, has_break=False, break_minutes=0,
                display_order=order, period="full", covers_am=True, covers_pm=True,
            ))
            changed = True

    # 朝食あり日の1人勤務用 6:30-14:30 を保証（旧 7:00-15:00 から時刻のみ変更）。
    #   solver は「朝食あり日=6:30-14:30 / 朝食なし日=9-16」で1人編成を切り替えるため、
    #   これがマスタに無いと朝食あり日はフォールバック不成立＝その日の調理が
    #   全員off（未配置）になる。時刻で判定するので手動追加済みなら重複しない。
    #   既に 7:00-15:00 で登録済みなら、その行の時刻を 6:30-14:30 へ寄せる
    #   （職員の許可シフトのチェックを付け直さずに済ませるため、行は作り直さない）。
    for _p in ShiftPattern.query.filter_by(
        staff_group="cooking", start_time="07:00", end_time="15:00"
    ).all():
        _p.start_time, _p.end_time = "06:30", "14:30"
        if "7:00-15:00" in (_p.label or ""):
            _p.label = _p.label.replace("7:00-15:00", "6:30-14:30")
        changed = True

    if ShiftPattern.query.filter_by(
        staff_group="cooking", start_time="06:30", end_time="14:30"
    ).first() is None:
        max_n, max_order = 0, 0
        for p in ShiftPattern.query.filter_by(staff_group="cooking").all():
            m = re.match(r"^cooking_(\d+)$", p.code or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
            max_order = max(max_order, p.display_order or 0)
        db.session.add(ShiftPattern(
            code=f"cooking_{max_n + 1}", staff_group="cooking",
            label="(9) 6:30-14:30", start_time="06:30", end_time="14:30",
            has_break=False, break_minutes=0, display_order=max_order + 1,
            period="full", covers_am=True, covers_pm=True,
        ))
        changed = True

    # GeneratedShift の旧調理コードを移行
    for old, new in _COOK_CODE_MIGRATE.items():
        n = GeneratedShift.query.filter_by(assignment=old).update(
            {"assignment": new}, synchronize_session=False
        )
        if n:
            changed = True

    # 依頼文22: StaffAllowedPattern（許可シフトパターン）の旧調理コードを移行。
    #   これを変換し損ねると、調理スタッフの許可集合が現行 cooking_* と一致せず
    #   全パターン除外＝「割り当て可能パターンなし」で全日未配置になる。
    for r in StaffAllowedPattern.query.filter(
        StaffAllowedPattern.assignment_code.in_(list(_COOK_CODE_MIGRATE.keys()))
    ).all():
        new_code = _COOK_CODE_MIGRATE[r.assignment_code]
        dup = StaffAllowedPattern.query.filter_by(
            staff_id=r.staff_id, assignment_code=new_code
        ).first()
        if dup:
            db.session.delete(r)  # 既に新コードがあれば重複削除
        else:
            r.assignment_code = new_code
        changed = True

    # CookingComboRule を 1組=1行 へ正規化（旧コード移行込み）
    rules = CookingComboRule.query.order_by(CookingComboRule.id).all()
    combos = []  # 各要素 = 種類コードのフラットリスト
    for r in rules:
        try:
            pats = json.loads(r.allowed_patterns_json or "[]")
        except (ValueError, TypeError):
            pats = []
        # 旧形式(1行に複数組=リストのリスト) と 新形式(1行1組=フラットリスト) の両対応
        groups = pats if (pats and all(isinstance(p, list) for p in pats)) else ([pats] if pats else [])
        for g in groups:
            migrated = [_COOK_CODE_MIGRATE.get(c, c) for c in g if isinstance(c, str)]
            if migrated and migrated not in combos:
                combos.append(migrated)

    # 望ましい状態（1組=1行・cooking_*）と現状が一致していれば触らない（冪等）
    desired_rows = []
    used = set()
    for combo in combos:
        name = _COMBO_NAME_BY_CONTENT.get(frozenset(combo))
        if not name:
            for i in range(26):
                cand = chr(ord("A") + i)
                if cand not in used and cand not in _COMBO_NAME_BY_CONTENT.values():
                    name = cand
                    break
            name = name or f"組{len(desired_rows)+1}"
        used.add(name)
        desired_rows.append((name, combo))

    current_ok = (
        len(rules) == len(desired_rows)
        and all(
            (json.loads(r.allowed_patterns_json or "[]") == combo)
            for r, (name, combo) in zip(rules, desired_rows)
        )
    )
    if not current_ok and desired_rows:
        for r in rules:
            db.session.delete(r)
        db.session.flush()
        for name, combo in desired_rows:
            db.session.add(CookingComboRule(
                name=name, allowed_patterns_json=json.dumps(combo), is_active=True,
            ))
        changed = True

    if changed:
        db.session.commit()


# ---------------------------------------------------------------------------
# ログイン（管理者・サ責）
# ---------------------------------------------------------------------------
def _load_users():
    """ログインアカウントを構築する。
    パスワードは環境変数（Renderのシークレット等）から読み込み、
    平文では保存せずハッシュ化して保持する。
      - SHIFT_ADMIN_PASSWORD  … 管理者のパスワード
      - SHIFT_SASEKI_PASSWORD … サ責のパスワード
      - SHIFT_YAKUIN_PASSWORD … 役員のパスワード（権限は管理者と同等＝全機能）
      - SHIFT_STAFF_PASSWORD  … 閲覧専用のパスワード（職員がシフトを見るだけ）
    未設定の場合はローカル開発用に admin/saseki/yakuin/staff を仮設定
    （要・本番では必ず環境変数を設定）。
    戻り値: {username: {"hash": ..., "role": 表示名}}
    """
    admin_pw = os.environ.get("SHIFT_ADMIN_PASSWORD", "").strip()
    saseki_pw = os.environ.get("SHIFT_SASEKI_PASSWORD", "").strip()
    yakuin_pw = os.environ.get("SHIFT_YAKUIN_PASSWORD", "").strip()
    staff_pw = os.environ.get("SHIFT_STAFF_PASSWORD", "").strip()
    dev_default = not admin_pw and not saseki_pw and not yakuin_pw
    if dev_default:
        # ローカル開発フォールバック（本番では環境変数を必ず設定すること）
        admin_pw = "admin"
        saseki_pw = "saseki"
        yakuin_pw = "yakuin"
    if not staff_pw and dev_default:
        staff_pw = "staff"
    users = {}
    if admin_pw:
        users["admin"] = {"hash": generate_password_hash(admin_pw), "role": "管理者"}
    if saseki_pw:
        users["saseki"] = {"hash": generate_password_hash(saseki_pw), "role": "サ責"}
    if yakuin_pw:
        # 役員は管理者と同じ権限（ログイン済みなら全機能アクセス可）
        users["yakuin"] = {"hash": generate_password_hash(yakuin_pw), "role": "役員"}
    if staff_pw:
        # 閲覧専用（できるのはシフトを見ることだけ。生成・編集・設定は不可）
        users["staff"] = {"hash": generate_password_hash(staff_pw), "role": VIEWER_ROLE}
    return users, dev_default


# 閲覧専用ロール（このロールは下のエンドポイントだけアクセスできる）
VIEWER_ROLE = "閲覧"
VIEWER_USERNAME = "staff"
# 役員ロール（閲覧＋役員自身の予定の入力だけできる）
EXEC_VIEW_ROLE = "役員閲覧"
EXEC_USERNAME = "yakuin"
# 事務ロール（閲覧＋事務職員自身の予定の入力だけできる）
OFFICE_VIEW_ROLE = "事務閲覧"
OFFICE_USERNAME = "jimu"
# ロール → そのアカウントが予定を入れられる区分
_PLAN_EDIT_ROLES = {
    EXEC_VIEW_ROLE: ("executive",),
    OFFICE_VIEW_ROLE: ("office",),
}
_VIEWER_ENDPOINTS = {
    "view_shift",           # 閲覧専用ページ
    "api_shifts_get",       # 月のシフト取得（読み取り）
    "api_shifts_available", # シフトのある年月（読み取り）
    "logout",
    "login",
    "static",
}

# 役員・事務ロールは閲覧に加えて「自分たちの予定」の保存だけできる
_EXEC_ENDPOINTS = _VIEWER_ENDPOINTS | {"api_shift_cells_update"}


# ログイン不要でアクセスできるエンドポイント
_PUBLIC_ENDPOINTS = {"login", "static"}

# 駐車場割り当て（依頼文24）で「出勤」とみなさない assignment
_PARKING_OFF_TOKENS = {"", "off", "cook_off"}


def _save_parking_assignments(generation_id, year, month, shifts_data):
    """生成後の駐車枠割り当てを計算して DB に保存する（commit は呼び出し側）。
    シフト生成(solver)には一切干渉しない、純粋な後処理。
    """
    slot_numbers = [
        s.slot_number
        for s in ParkingSlot.query.order_by(ParkingSlot.display_order, ParkingSlot.id).all()
    ]
    car_staff_rows = Staff.query.filter_by(car_commute=True).all()
    if not car_staff_rows:
        return  # 車通勤者がいなければ何もしない
    car_ids = {s.id for s in car_staff_rows}
    car_staff = [{"id": s.id, "parking_slot": s.parking_slot or ""} for s in car_staff_rows]

    # その日に出勤する車通勤者の集合を作る
    working_by_date = {}
    for item in shifts_data:
        sid = item["staff_id"]
        if sid not in car_ids:
            continue
        if (item.get("assignment") or "") in _PARKING_OFF_TOKENS:
            continue
        d = item["date"]
        d_iso = d if isinstance(d, str) else d.isoformat()
        working_by_date.setdefault(d_iso, set()).add(sid)

    if not slot_numbers and not working_by_date:
        return

    month_dates = [
        date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)
    ]
    assignment = assign_parking(month_dates, working_by_date, car_staff, slot_numbers)

    for d_iso, per_staff in assignment.items():
        d_obj = datetime.strptime(d_iso, "%Y-%m-%d").date()
        for sid, label in per_staff.items():
            db.session.add(ParkingAssignment(
                generation_id=generation_id,
                date=d_obj,
                staff_id=sid,
                label=label,
            ))


# ---------------------------------------------------------------------------
# アプリケーションファクトリ
# ---------------------------------------------------------------------------
def create_app():
    """Flask アプリケーションを生成して返す"""
    app = Flask(__name__)
    app.config.from_object(Config)

    # テンプレートを毎リクエストでディスクから読み直す（debug=Falseでも反映）。
    # これにより、テンプレート編集後にサーバーを再起動しなくても画面に反映される。
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # 画面のJS/CSSを更新したとき、ブラウザが古いものを使い続けないように
    #   ファイルの更新時刻をURLに付ける（ユーザー依頼 2026-08:「直ってない」の原因）
    def _asset_version() -> str:
        stamp = 0
        for rel in ("js/app.js", "css/style.css"):
            try:
                stamp = max(stamp, int(os.path.getmtime(
                    os.path.join(app.static_folder, rel))))
            except OSError:
                pass
        return str(stamp)

    app.config["ASSET_VERSION"] = _asset_version()

    @app.context_processor
    def _inject_asset_version():
        return {"asset_version": app.config["ASSET_VERSION"]}

    # SQLAlchemy 初期化
    db.init_app(app)

    # CSRF保護
    csrf = CSRFProtect(app)

    # --- ログイン設定 ---
    users, _dev_default = _load_users()
    app.config["USERS"] = users
    if _dev_default:
        app.logger.warning(
            "ログインがローカル既定値(admin/saseki/yakuin)で動作中です。"
            "本番では SHIFT_ADMIN_PASSWORD / SHIFT_SASEKI_PASSWORD / SHIFT_YAKUIN_PASSWORD と "
            "SECRET_KEY を必ず設定してください。"
        )

    @app.before_request
    def _require_login():
        # ログイン画面・静的ファイルは認証不要。それ以外は未ログインなら /login へ。
        endpoint = request.endpoint or ""
        if endpoint in _PUBLIC_ENDPOINTS or endpoint.startswith("static"):
            return None
        if not session.get("user"):
            # API(JSON)呼び出しには401を返し、画面遷移はログインへ
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインが必要です", "login_required": True}), 401
            return redirect(url_for("login", next=request.path))
        # 閲覧専用ロールは閲覧ページと読み取りAPIのみ（編集・生成・設定は一切不可）
        if session.get("role") == VIEWER_ROLE:
            if endpoint not in _VIEWER_ENDPOINTS or request.method != "GET":
                if request.path.startswith("/api/"):
                    return jsonify({"error": "閲覧専用アカウントでは実行できません"}), 403
                return redirect(url_for("view_shift"))
        if session.get("role") in _PLAN_EDIT_ROLES:
            # 閲覧＋「役員の予定」の保存のみ（中身のチェックは保存時に行う）
            ok = endpoint in _EXEC_ENDPOINTS and (
                request.method == "GET" or endpoint == "api_shift_cells_update"
            )
            if not ok:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "このアカウントでは実行できません"}), 403
                return redirect(url_for("view_shift"))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user"):
            return redirect(url_for("index"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            # 環境変数のパスワードと、条件設定画面で決めたパスワードの両方を試す。
            #   （2026-08: 環境変数が優先されて、設定画面で変えたパスワードでは
            #     入れなくなっていた）
            candidates = []
            _env_user = app.config["USERS"].get(username)
            if _env_user:
                candidates.append(_env_user)
            _from_settings = {
                OFFICE_USERNAME: ("office_password_hash", OFFICE_VIEW_ROLE),
                EXEC_USERNAME: ("exec_password_hash", EXEC_VIEW_ROLE),
                VIEWER_USERNAME: ("viewer_password_hash", VIEWER_ROLE),
            }.get(username)
            if _from_settings:
                _col, _role = _from_settings
                _st = ShiftSettings.query.first()
                _hash = getattr(_st, _col, "") or ""
                if _hash:
                    candidates.append({"hash": _hash, "role": _role})
            user = next(
                (c for c in candidates if check_password_hash(c["hash"], password)),
                None,
            )
            if user:
                session["user"] = username
                session["role"] = user["role"]
                _view_only = (user["role"] == VIEWER_ROLE
                              or user["role"] in _PLAN_EDIT_ROLES)
                default_next = (
                    url_for("view_shift") if _view_only else url_for("index")
                )
                nxt = request.args.get("next") or default_next
                if _view_only:
                    nxt = default_next
                # オープンリダイレクト防止: 内部パスのみ許可
                if not nxt.startswith("/"):
                    nxt = default_next
                return redirect(nxt)
            flash("IDまたはパスワードが正しくありません。", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("login"))

    with app.app_context():
        # 既存テーブルのマイグレーション（カラム追加）
        _run_migrations(app)

        # テーブル作成（新テーブル含む）
        db.create_all()

        # 旧DBの資格マスタを正規化
        _normalize_qualifications()
        db.session.commit()

        # デフォルト設定レコード
        if ShiftSettings.query.first() is None:
            default_settings = ShiftSettings()
            db.session.add(default_settings)
            db.session.commit()

        # ShiftPattern 初期データ
        if ShiftPattern.query.count() == 0:
            for p in _INITIAL_PATTERNS:
                db.session.add(ShiftPattern(**p))
            db.session.commit()
        else:
            # 既存DBに care_3, care_4 がなければ追加
            for p in _INITIAL_PATTERNS:
                if not ShiftPattern.query.filter_by(code=p["code"]).first():
                    db.session.add(ShiftPattern(**p))
            db.session.commit()

        # Qualification 初期データ（既存DBにも新資格が追加されるよう個別チェック）
        for q in _INITIAL_QUALIFICATIONS:
            existing_qual = Qualification.query.filter_by(code=q["code"]).first()
            if existing_qual is None:
                db.session.add(Qualification(**q))
                continue

            if existing_qual.display_order != q["display_order"]:
                existing_qual.display_order = q["display_order"]
            if q["code"] == "counselor" and existing_qual.name != q["name"]:
                existing_qual.name = q["name"]
        db.session.commit()

        # PlacementRule 初期データ
        if PlacementRule.query.count() == 0:
            for rule_data in _INITIAL_PLACEMENT_RULES:
                rule_copy = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                # 資格IDの解決
                if "_qual_code" in rule_data:
                    q = Qualification.query.filter_by(code=rule_data["_qual_code"]).first()
                    if q:
                        rule_copy["target_qualification_ids_json"] = json.dumps([q.id])
                elif "_qual_codes" in rule_data:
                    ids = []
                    for qc in rule_data["_qual_codes"]:
                        q = Qualification.query.filter_by(code=qc).first()
                        if q:
                            ids.append(q.id)
                    rule_copy["target_qualification_ids_json"] = json.dumps(ids)
                db.session.add(PlacementRule(**rule_copy))
            db.session.commit()

        # 依頼文18-A / 依頼文20: 看護師の配置条件は solver 側の「合計2時間以上」判定に
        #   一本化済み。看護師を対象とする人数ベースの配置ルール（qualification_min）は
        #   重複・残骸であり、しかも nurse_short(看護9:30-13:30) を数えられず誤警告
        #   「配置ルール未達: 看護師…」を出すため、無効化する。
        #   対象: 看護師資格を含む qualification_min ルール／名前が「看護師…」で始まるルール。
        #   （相談員・男性などのルールには触れない。既に無効/不在なら冪等）。
        _nurse_qual_ids = {
            q.id for q in Qualification.query.filter(Qualification.code.in_(["nurse"])).all()
        }
        for r in PlacementRule.query.filter_by(rule_type="qualification_min").all():
            if not r.is_active:
                continue
            try:
                tq = set(json.loads(r.target_qualification_ids_json or "[]"))
            except (ValueError, TypeError):
                tq = set()
            name = r.name or ""
            if (tq & _nurse_qual_ids) or name.startswith("看護師"):
                r.is_active = False
        db.session.commit()

        # 依頼文18-B: 適用時間帯を period→time_start/time_end へ移行（時刻入力化）。
        #   既存挙動を保つ初期値: 午前=09:00-13:00 / 午後=13:00-16:00 / 終日=09:00-16:00。
        #   既に時刻が入っているルールは触らない（冪等）。
        _PERIOD_TO_TIMES = {
            "am": ("09:00", "13:00"),
            "pm": ("13:00", "16:00"),
            "all": ("09:00", "16:00"),
        }
        for r in PlacementRule.query.all():
            has_start = bool((r.time_start or "").strip())
            has_end = bool((r.time_end or "").strip())
            if not has_start and not has_end:
                ts, te = _PERIOD_TO_TIMES.get(r.period or "all", ("09:00", "16:00"))
                r.time_start = ts
                r.time_end = te
        db.session.commit()

        # CookingComboRule 初期データ
        if CookingComboRule.query.count() == 0:
            db.session.add(CookingComboRule(**_INITIAL_COOKING_COMBO))
            db.session.commit()

        # 調理パターンの整合（変更1〜3を既存DBへも反映）
        _sync_cooking_patterns()

        # 駐車枠マスタ 初期データ（依頼文24）: 未登録なら 4/7/8 を投入
        if ParkingSlot.query.count() == 0:
            for i, num in enumerate(["4", "7", "8"]):
                db.session.add(ParkingSlot(slot_number=num, display_order=i))
            db.session.commit()

    # -----------------------------------------------------------------
    # W-6: セキュリティヘッダ
    # -----------------------------------------------------------------
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    # W-8: シフト生成の排他制御
    generate_lock = threading.Lock()

    # -----------------------------------------------------------------
    # エラーハンドラー（API は JSON で返す）
    # -----------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "リソースが見つかりません"}), 404
        return render_template("base.html", error_title="ページが見つかりません", error_message="お探しのページは存在しないか、移動した可能性があります。メニューからお戻りください。"), 404

    @app.errorhandler(500)
    def internal_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "サーバー内部エラーが発生しました"}), 500
        return render_template("base.html", error_title="エラーが発生しました", error_message="申し訳ございません。しばらく待ってから再度お試しください。問題が続く場合は開発者にご連絡ください。"), 500

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        # 依頼文34: CSRFトークンが期限切れ/不一致のとき。
        # 旧来は HTML(400 "The CSRF token has expired." 等118B) が返り、
        # クライアントが「生成に時間がかかり過ぎた」と誤表示していた。
        # API には実態に合うJSONを返し、画面遷移は再読み込みを促す。
        msg = ("セキュリティトークンの有効期限が切れたか不正です。"
               "ページを再読み込み（リロード）してから、もう一度お試しください。")
        if request.path.startswith("/api/"):
            return jsonify({"error": msg, "csrf_error": True}), 400
        flash(msg, "error")
        return redirect(request.referrer or url_for("index"))

    # -----------------------------------------------------------------
    # ページルート
    # -----------------------------------------------------------------
    @app.route("/")
    def index():
        """ダッシュボード"""
        staff_count = Staff.query.count()
        fulltime_count = Staff.query.filter(Staff.employment_type.in_(["常勤", "時短正社員", "正社員", "管理者"])).count()
        parttime_count = Staff.query.filter_by(employment_type="パート").count()
        dual_count = Staff.query.filter_by(can_visit=True).count()
        care_count = Staff.query.filter_by(staff_group="care").count()
        cooking_count = Staff.query.filter_by(staff_group="cooking").count()
        return render_template(
            "index.html",
            staff_count=staff_count,
            fulltime_count=fulltime_count,
            parttime_count=parttime_count,
            dual_count=dual_count,
            care_count=care_count,
            cooking_count=cooking_count,
        )

    @app.route("/staff")
    def staff_list():
        """職員一覧（既定では在籍中のみ。休職中・退職者は切替で表示）。

        ユーザー依頼（2026-08）:「退職した人や休職中も一覧から表示されないようにして」。
        データは残したまま、普段の一覧からは隠す。
        """
        show_inactive = request.args.get("show_inactive") == "1"
        query = Staff.query
        if not show_inactive:
            query = query.filter_by(on_leave=False, retired=False)
        staffs = query.order_by(Staff.id).all()
        inactive_count = Staff.query.filter(
            db.or_(Staff.on_leave == True, Staff.retired == True)  # noqa: E712
        ).count()
        return render_template(
            "staff_list.html", staff_list=staffs,
            show_inactive=show_inactive, inactive_count=inactive_count,
        )

    @app.route("/staff/new")
    def staff_new():
        """職員登録フォーム"""
        qualifications = Qualification.query.order_by(Qualification.display_order).all()
        shift_patterns = ShiftPattern.query.order_by(ShiftPattern.display_order).all()
        return render_template("staff_form.html", staff=None, qualifications=qualifications,
                               shift_patterns=shift_patterns, allowed_pattern_codes=[],
                               pattern_assignment_map=_PATTERN_CODE_TO_ASSIGNMENT,
                               job_categories=JOB_CATEGORIES, roles=ROLES,
                               employment_types=EMPLOYMENT_TYPES, has_counselor=False)

    @app.route("/staff/<int:staff_id>/edit")
    def staff_edit(staff_id):
        """職員編集フォーム"""
        staff = Staff.query.get_or_404(staff_id)
        day_offs = DayOffRequest.query.filter_by(staff_id=staff_id).order_by(DayOffRequest.date).all()
        qualifications = Qualification.query.order_by(Qualification.display_order).all()
        staff_qual_ids = [sq.qualification_id for sq in StaffQualification.query.filter_by(staff_id=staff_id).all()]
        shift_patterns = ShiftPattern.query.order_by(ShiftPattern.display_order).all()
        allowed_pattern_codes = [
            ap.assignment_code for ap in StaffAllowedPattern.query.filter_by(staff_id=staff_id).all()
        ]
        return render_template("staff_form.html", staff=staff, day_offs=day_offs,
                               qualifications=qualifications, staff_qual_ids=staff_qual_ids,
                               shift_patterns=shift_patterns, allowed_pattern_codes=allowed_pattern_codes,
                               pattern_assignment_map=_PATTERN_CODE_TO_ASSIGNMENT,
                               job_categories=JOB_CATEGORIES, roles=ROLES,
                               employment_types=EMPLOYMENT_TYPES,
                               has_counselor=_staff_has_counselor(staff_id))

    @app.route("/settings")
    def settings():
        """条件設定ページ"""
        s = ShiftSettings.query.first()
        qualifications = Qualification.query.order_by(Qualification.display_order).all()
        placement_rules = PlacementRule.query.order_by(PlacementRule.id).all()
        cooking_combo_rules = [
            r.to_dict() for r in CookingComboRule.query.order_by(CookingComboRule.id).all()
        ]
        cooking_types = [
            r.to_dict() for r in ShiftPattern.query.filter_by(staff_group="cooking")
            .order_by(ShiftPattern.display_order).all()
        ]
        return render_template("settings.html", settings=s,
                               qualifications=qualifications,
                               placement_rules=placement_rules,
                               cooking_combo_rules=cooking_combo_rules,
                               cooking_types=cooking_types)

    @app.route("/calendar")
    def calendar_page():
        """シフトカレンダーページ"""
        return render_template("calendar.html")

    @app.route("/view")
    def view_shift():
        """閲覧専用ページ（職員がシフトを見るだけの画面）。

        ユーザー依頼（2026-08）:「出来上がったシフトを即時反映して閲覧するだけの
        別アプリを作ってほしい」。同じデータベースを直接読むので常に最新が出る。
        生成・編集・設定への導線は一切置かない。
        """
        today = date.today()
        return render_template(
            "view.html",
            default_year=today.year,
            default_month=today.month,
            is_viewer=(session.get("role") == VIEWER_ROLE),
            # この画面から予定を入れられる区分（役員／事務）
            plan_edit_categories=(
                [] if session.get("role") == VIEWER_ROLE
                else list(_PLAN_EDIT_ROLES.get(session.get("role"), PLAN_ONLY_CATEGORIES))
            ),
        )

    # -----------------------------------------------------------------
    # API ルート — 職員 CRUD
    # -----------------------------------------------------------------
    @app.route("/api/staff", methods=["POST"])
    def staff_create():
        """職員の新規作成"""
        available_days = ",".join(request.form.getlist("available_days"))
        fixed_days_off = ",".join(request.form.getlist("fixed_days_off"))

        name = request.form.get("name", "").strip()
        if not name:
            flash("氏名は必須です。", "error")
            return redirect(url_for("staff_new"))

        job_category = _normalize_job_category(request.form.get("job_category", "caregiver"))
        staff_group = _job_category_to_group(job_category)
        is_care = staff_group == "care"

        staff = Staff(
            name=name,
            employment_type=request.form.get("employment_type", "常勤"),
            job_category=job_category,
            staff_group=staff_group,
            role=_normalize_role(request.form.get("role", "")),
            can_visit="can_visit" in request.form if is_care else False,
            has_phone_duty="has_phone_duty" in request.form if is_care else False,
            can_bath_assist="can_bath_assist" in request.form if is_care else False,
            gender=request.form.get("gender", ""),
            max_consecutive_days=safe_int(request.form.get("max_consecutive_days"), 5),
            max_days_per_week=safe_int(request.form.get("max_days_per_week"), 5),
            min_days_per_week=safe_int(request.form.get("min_days_per_week"), 0),
            available_days=available_days if available_days else "0,1,2,3,4,5,6",
            available_time_slots=request.form.get("available_time_slots", "full_day") if is_care else "full_day",
            work_start_time=(request.form.get("work_start_time", "") or "").strip(),
            work_end_time=(request.form.get("work_end_time", "") or "").strip(),
            fixed_days_off=fixed_days_off,
            required_days=",".join(request.form.getlist("required_days")),
            weekend_constraint=request.form.get("weekend_constraint", ""),
            holiday_ng="holiday_ng" in request.form,
            on_leave="on_leave" in request.form,
            retired="retired" in request.form,
            retired_date=_parse_retired_month(request.form.get("retired_date", "")),
            oncall_only="oncall_only" in request.form if is_care else False,
            backup_only="backup_only" in request.form,
            oncall_when_off_ok="oncall_when_off_ok" in request.form if is_care else False,
            public_holiday_count=max(0, safe_int(request.form.get("public_holiday_count"), 0)),
            car_commute="car_commute" in request.form,
            parking_slot=(request.form.get("parking_slot", "") or "").strip(),
            # 調理スタッフのみ新人/ベテランを保持（それ以外は未設定）
            cooking_experience=(
                _normalize_cooking_experience(request.form.get("cooking_experience", ""))
                if staff_group == "cooking" else ""
            ),
            # 初出勤日（依頼文36・調理スタッフのみ入力欄あり）
            first_work_date=(
                _parse_first_work_date(request.form.get("first_work_date", ""))
                if staff_group == "cooking" else None
            ),
        )
        db.session.add(staff)
        db.session.flush()  # IDを取得

        # 資格の紐付け（相談員は「相談員可」チェックで別途管理するため除外）
        counselor_qid = _counselor_qual_id()
        qual_ids = request.form.getlist("qualifications")
        for qid in qual_ids:
            qid_int = safe_int(qid, None)
            if qid_int is not None and qid_int != counselor_qid:
                db.session.add(StaffQualification(staff_id=staff.id, qualification_id=qid_int))
        # 相談員可 → 既存の相談員資格に連動
        _set_counselor(staff.id, "can_counsel" in request.form)

        # 許可シフトパターンの保存（チェックなし＝全パターン許可）
        allowed_codes = normalize_allowed_pattern_codes(
            request.form.getlist("allowed_patterns"), staff_group
        )
        for code in allowed_codes:
            db.session.add(StaffAllowedPattern(staff_id=staff.id, assignment_code=code))

        db.session.commit()
        flash(f"{staff.name} さんを登録しました。", "success")
        return redirect(url_for("staff_list"))

    @app.route("/api/staff/<int:staff_id>", methods=["POST"])
    def staff_update(staff_id):
        """職員の更新"""
        staff = Staff.query.get_or_404(staff_id)
        available_days = ",".join(request.form.getlist("available_days"))
        fixed_days_off = ",".join(request.form.getlist("fixed_days_off"))

        name = request.form.get("name", staff.name).strip()
        if not name:
            flash("氏名は必須です。", "error")
            return redirect(url_for("staff_edit", staff_id=staff_id))

        staff.name = name
        staff.employment_type = request.form.get("employment_type", staff.employment_type)
        staff.job_category = _normalize_job_category(
            request.form.get("job_category", staff.job_category)
        )
        staff.staff_group = _job_category_to_group(staff.job_category)
        staff.role = _normalize_role(request.form.get("role", staff.role))
        staff.gender = request.form.get("gender", "")
        staff.work_start_time = (request.form.get("work_start_time", "") or "").strip()
        staff.work_end_time = (request.form.get("work_end_time", "") or "").strip()

        # 応援（人手が足りないときだけ入れる）は区分を問わず保存する
        staff.backup_only = "backup_only" in request.form

        if staff.staff_group == "cooking":
            staff.can_visit = False
            staff.has_phone_duty = False
            staff.can_bath_assist = False
            staff.oncall_only = False
            staff.oncall_when_off_ok = False
            staff.available_time_slots = "full_day"
            # 調理スタッフのみ新人/ベテランを保持
            staff.cooking_experience = _normalize_cooking_experience(
                request.form.get("cooking_experience", "")
            )
            # 初出勤日（依頼文36）
            staff.first_work_date = _parse_first_work_date(
                request.form.get("first_work_date", "")
            )
        else:
            staff.can_visit = "can_visit" in request.form
            staff.has_phone_duty = "has_phone_duty" in request.form
            staff.can_bath_assist = "can_bath_assist" in request.form
            staff.oncall_only = "oncall_only" in request.form
            staff.oncall_when_off_ok = "oncall_when_off_ok" in request.form
            staff.available_time_slots = request.form.get(
                "available_time_slots", staff.available_time_slots
            )
            # 調理以外に変更された場合は経験区分・初出勤日をクリア
            staff.cooking_experience = ""
            staff.first_work_date = None

        staff.max_consecutive_days = safe_int(
            request.form.get("max_consecutive_days"), staff.max_consecutive_days
        )
        staff.max_days_per_week = safe_int(
            request.form.get("max_days_per_week"), staff.max_days_per_week
        )
        staff.min_days_per_week = safe_int(
            request.form.get("min_days_per_week"), getattr(staff, "min_days_per_week", 0) or 0
        )
        staff.available_days = available_days if available_days else staff.available_days
        staff.fixed_days_off = fixed_days_off
        staff.required_days = ",".join(request.form.getlist("required_days"))
        staff.weekend_constraint = request.form.get("weekend_constraint", "")
        staff.holiday_ng = "holiday_ng" in request.form
        staff.on_leave = "on_leave" in request.form
        staff.retired = "retired" in request.form
        staff.retired_date = _parse_retired_month(request.form.get("retired_date", ""))
        staff.public_holiday_count = max(0, safe_int(request.form.get("public_holiday_count"), 0))
        # 駐車場（依頼文24）— 車通勤は care/cooking どちらも対象
        staff.car_commute = "car_commute" in request.form
        staff.parking_slot = (request.form.get("parking_slot", "") or "").strip()

        # 資格の更新（全削除→再追加。相談員は「相談員可」チェックで別途管理）
        counselor_qid = _counselor_qual_id()
        StaffQualification.query.filter_by(staff_id=staff_id).delete()
        qual_ids = request.form.getlist("qualifications")
        for qid in qual_ids:
            qid_int = safe_int(qid, None)
            if qid_int is not None and qid_int != counselor_qid:
                db.session.add(StaffQualification(staff_id=staff_id, qualification_id=qid_int))
        # 相談員可 → 既存の相談員資格に連動
        _set_counselor(staff_id, "can_counsel" in request.form)

        # 許可シフトパターンの更新（全削除→再追加。チェックなし＝全パターン許可）
        StaffAllowedPattern.query.filter_by(staff_id=staff_id).delete()
        allowed_codes = normalize_allowed_pattern_codes(
            request.form.getlist("allowed_patterns"), staff.staff_group
        )
        for code in allowed_codes:
            db.session.add(StaffAllowedPattern(staff_id=staff_id, assignment_code=code))

        db.session.commit()
        flash(f"{staff.name} さんの情報を更新しました。", "success")
        return redirect(url_for("staff_list"))

    @app.route("/api/staff/<int:staff_id>/delete", methods=["POST"])
    def staff_delete(staff_id):
        """職員の削除（関連する休み希望・生成シフトも削除）"""
        staff = Staff.query.get_or_404(staff_id)
        name = staff.name
        db.session.delete(staff)
        db.session.commit()
        flash(f"{name} さんを削除しました。", "success")
        return redirect(url_for("staff_list"))

    @app.route("/api/staff/bulk-bath-assist", methods=["POST"])
    def staff_bulk_bath_assist():
        """介護(care)職員の入浴介助可をまとめて設定/解除する。"""
        enable = request.form.get("enable", "1") == "1"
        care_staff = Staff.query.filter_by(staff_group="care").all()
        for st in care_staff:
            st.can_bath_assist = enable
        db.session.commit()
        verb = "付与" if enable else "解除"
        flash(f"介護職員 {len(care_staff)}名に入浴介助可を{verb}しました。", "success")
        return redirect(url_for("staff_list"))

    # -----------------------------------------------------------------
    # 職員CSV 一括エクスポート / インポート
    # -----------------------------------------------------------------
    @app.route("/api/staff/export-csv", methods=["GET"])
    def staff_export_csv():
        """現在の職員一覧をCSVで出力（見本CSV兼テンプレート）。"""
        _ids, qual_names, qual_codes = _build_staff_qualification_maps()
        # 出勤可能日（職員ごと）
        workable_map: dict[int, list[str]] = {}
        for w in StaffWorkableDate.query.order_by(StaffWorkableDate.date).all():
            workable_map.setdefault(w.staff_id, []).append(w.date.isoformat())
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(STAFF_CSV_COLUMNS)
        for s in Staff.query.order_by(Staff.id).all():
            has_counsel = "counselor" in qual_codes.get(s.id, [])
            # 資格列は相談員を除外（相談員は can_counsel 列で管理）
            names_excl = [n for n in qual_names.get(s.id, []) if n != "相談員"]
            writer.writerow([
                s.id,
                s.name,
                getattr(s, "job_category", "caregiver") or "caregiver",
                getattr(s, "role", "") or "",
                s.employment_type or "",
                s.gender or "",
                1 if s.can_visit else 0,
                1 if has_counsel else 0,
                1 if (getattr(s, "can_bath_assist", False) or False) else 0,
                1 if s.has_phone_duty else 0,
                1 if (getattr(s, "oncall_only", False) or False) else 0,
                1 if (getattr(s, "retired", False) or False) else 0,
                1 if (getattr(s, "holiday_ng", False) or False) else 0,
                getattr(s, "weekend_constraint", "") or "",
                getattr(s, "work_start_time", "") or "",
                getattr(s, "work_end_time", "") or "",
                s.available_time_slots or "full_day",
                s.available_days or "",
                s.fixed_days_off or "",
                s.max_consecutive_days if s.max_consecutive_days is not None else 5,
                s.max_days_per_week if s.max_days_per_week is not None else 5,
                getattr(s, "min_days_per_week", 0) or 0,
                ";".join(names_excl),
                ";".join(workable_map.get(s.id, [])),
            ])
        # Excelで文字化けしないよう UTF-8 BOM 付き
        data = "﻿" + output.getvalue()
        buf = BytesIO(data.encode("utf-8"))
        buf.seek(0)
        return send_file(
            buf,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="staff_template.csv",
        )

    def _apply_csv_scalars(staff, row):
        """CSV1行(dict)のスカラー項目を Staff へ反映（資格・出勤可能日は別途）。"""
        # 区分(job_category)優先。無ければ旧 staff_group 列から推定（後方互換）。
        raw_jc = row.get("job_category", "")
        if str(raw_jc).strip():
            job_category = _normalize_job_category(raw_jc)
        else:
            job_category = (
                "cooking"
                if _normalize_staff_group(row.get("staff_group", "care")) == "cooking"
                else "caregiver"
            )
        group = _job_category_to_group(job_category)
        staff.name = (row.get("name") or "").strip()
        staff.job_category = job_category
        staff.staff_group = group
        staff.role = _normalize_role(row.get("role", ""))
        # 雇用形態: 旧CSVの「管理者」は役割へ寄せる
        emp = (row.get("employment_type") or "常勤").strip() or "常勤"
        if emp == "管理者":
            if not staff.role:
                staff.role = "manager"
            emp = "常勤"
        staff.employment_type = emp
        staff.gender = (row.get("gender") or "").strip()
        staff.available_days = (row.get("available_days") or "0,1,2,3,4,5,6").strip() or "0,1,2,3,4,5,6"
        staff.fixed_days_off = (row.get("fixed_days_off") or "").strip()
        staff.max_consecutive_days = safe_int(row.get("max_consecutive_days"), 5)
        staff.max_days_per_week = safe_int(row.get("max_days_per_week"), 5)
        staff.min_days_per_week = safe_int(row.get("min_days_per_week"), 0)
        staff.weekend_constraint = (row.get("weekend_constraint") or "").strip()
        staff.holiday_ng = _csv_to_bool(row.get("holiday_ng"))
        if "retired" in row:
            staff.retired = _csv_to_bool(row.get("retired"))
        staff.work_start_time = (row.get("work_start_time") or "").strip()
        staff.work_end_time = (row.get("work_end_time") or "").strip()
        if group == "cooking":
            # 調理は職員フォームと同じく訪問・電話当番・入浴介助・時間帯を固定
            staff.can_visit = False
            staff.has_phone_duty = False
            staff.can_bath_assist = False
            staff.oncall_only = False
            staff.oncall_when_off_ok = False
            staff.available_time_slots = "full_day"
        else:
            staff.can_visit = _csv_to_bool(row.get("can_visit"))
            staff.has_phone_duty = _csv_to_bool(row.get("has_phone_duty"))
            staff.can_bath_assist = _csv_to_bool(row.get("can_bath_assist"))
            # 旧形式CSV（列なし）では既存値を維持する
            if "oncall_only" in row:
                staff.oncall_only = _csv_to_bool(row.get("oncall_only"))
            staff.available_time_slots = (row.get("available_time_slots") or "full_day").strip() or "full_day"

    def _apply_csv_qualifications(staff, row):
        """資格列を Staff.id に紐付け直す（staff は flush 済みで id を持つこと）。未知トークンを返す。
        相談員は資格列ではなく can_counsel 列で管理する。"""
        ids, unknown = _resolve_qualification_ids(_split_multi(row.get("qualifications", "")))
        counselor_qid = _counselor_qual_id()
        StaffQualification.query.filter_by(staff_id=staff.id).delete()
        for qid in ids:
            if qid == counselor_qid:
                continue  # 相談員は can_counsel 列で管理
            db.session.add(StaffQualification(staff_id=staff.id, qualification_id=qid))
        # 相談員可: can_counsel 列があればそれを、無ければ資格列に相談員が含まれていたかで判定
        if "can_counsel" in row:
            counsel = _csv_to_bool(row.get("can_counsel"))
        else:
            counsel = counselor_qid is not None and counselor_qid in ids
        _set_counselor(staff.id, counsel)
        return unknown

    def _apply_csv_workable_dates(staff, row):
        """出勤可能日列(YYYY-MM-DD をセミコロン等区切り)を反映。不正トークンのリストを返す。"""
        StaffWorkableDate.query.filter_by(staff_id=staff.id).delete()
        bad, seen = [], set()
        for tok in _split_multi(row.get("workable_dates", "")):
            try:
                d = datetime.strptime(tok, "%Y-%m-%d").date()
            except ValueError:
                bad.append(tok)
                continue
            if d in seen:
                continue
            seen.add(d)
            db.session.add(StaffWorkableDate(staff_id=staff.id, date=d))
        return bad

    def _find_merge_target(row, name, group):
        """差分取り込みの突合: ① id列が既存と一致すれば最優先。
        ② 無ければ『名前×区分(staff_group)』で突合。
        戻り値 (target_or_None, status)
          status: "id" / "name_group" / "none"（新規追加） / "ambiguous"（同名同区分が複数＝判定不能）
        ※ 池田(看護/care) と 池田(調理/cooking) は区分が異なるため別人として正しく突合される。
        """
        raw_id = (row.get("id") or "").strip()
        if raw_id:
            sid = safe_int(raw_id, None)
            if sid is not None:
                t = Staff.query.get(sid)
                if t is not None:
                    return t, "id"
        # id 未指定 or id がDBに無い → 名前×区分でフォールバック突合
        matches = Staff.query.filter_by(name=name, staff_group=group).all()
        if len(matches) == 1:
            return matches[0], "name_group"
        if len(matches) >= 2:
            return None, "ambiguous"
        return None, "none"

    @app.route("/api/debug/cooks", methods=["GET"])
    def debug_cooks():
        """調理職員の生設定を可視化（診断用・読み取り専用）。"""
        allowed = {}
        for ap in StaffAllowedPattern.query.all():
            allowed.setdefault(ap.staff_id, []).append(ap.assignment_code)
        offs = {}
        for r in DayOffRequest.query.all():
            offs.setdefault(r.staff_id, []).append(r.date.isoformat())
        out = []
        for s in Staff.query.filter_by(staff_group="cooking").order_by(Staff.id).all():
            out.append({
                "id": s.id,
                "name": s.name,
                "on_leave(休職中)": bool(s.on_leave),
                "available_days(勤務可能曜日)": s.available_days or "(空)",
                "fixed_days_off(勤務不可曜日)": s.fixed_days_off or "(なし)",
                "min_days_per_week(週下限)": getattr(s, "min_days_per_week", 0),
                "max_days_per_week(週上限)": s.max_days_per_week,
                "cooking_experience(経験)": s.cooking_experience or "(未設定)",
                "allowed_patterns(許可)": allowed.get(s.id, "(制限なし=全許可)"),
                "day_off_requests(希望休)": offs.get(s.id, []),
            })
        return jsonify(out)

    @app.route("/api/staff/import-csv", methods=["POST"])
    def staff_import_csv():
        """職員一覧をCSVから一括取り込み（全置き換え / 差分）。"""
        mode = request.form.get("mode", "merge")
        file = request.files.get("file")
        if not file or not file.filename:
            flash("CSVファイルを選択してください。", "error")
            return redirect(url_for("staff_list"))

        try:
            raw = file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                raw = file.read().decode("cp932")  # Excel(Shift_JIS)保存にも対応
            except Exception:
                flash("CSVの文字コードを読み取れませんでした（UTF-8で保存してください）。", "error")
                return redirect(url_for("staff_list"))

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames or "name" not in [h.strip() for h in reader.fieldnames]:
            flash("CSVヘッダーに『name』列がありません。見本CSVをダウンロードして列を合わせてください。", "error")
            return redirect(url_for("staff_list"))

        rows = []
        for r in reader:
            rows.append({(k or "").strip(): (v or "") for k, v in r.items()})

        added = updated = skipped = 0
        unknown_quals: set[str] = set()
        errors: list[str] = []

        try:
            if mode == "replace":
                # 全置き換え: 既存職員と、その依存データを全削除。
                # ※ Query.delete() は ORM のカスケードを発火しないため、
                #   生成シフト・休み希望・資格・許可パターン・警告を明示的に削除する。
                GeneratedShift.query.delete()
                ShiftWarning.query.delete()
                DayOffRequest.query.delete()
                StaffWorkableDate.query.delete()
                StaffQualification.query.delete()
                StaffAllowedPattern.query.delete()
                Staff.query.delete()
                db.session.flush()

            for idx, row in enumerate(rows, start=2):  # 2=ヘッダーの次の行
                name = (row.get("name") or "").strip()
                if not name:
                    skipped += 1
                    continue

                target = None
                if mode == "merge":
                    group = _normalize_staff_group(row.get("staff_group", "care"))
                    target, status = _find_merge_target(row, name, group)
                    if status == "ambiguous":
                        group_label = "調理" if group == "cooking" else "ケア"
                        errors.append(
                            f"{idx}行目: 「{name}」({group_label}) が既存に複数いるため突合できませんでした。"
                            f"スキップしました。id列で対象を指定してください。"
                        )
                        skipped += 1
                        continue

                if target is None:
                    target = Staff()
                    _apply_csv_scalars(target, row)  # name 等を先に設定してから
                    db.session.add(target)
                    db.session.flush()               # id採番（NOT NULL を満たした状態で）
                    added += 1
                else:
                    _apply_csv_scalars(target, row)
                    updated += 1
                unknown = _apply_csv_qualifications(target, row)
                unknown_quals.update(unknown)
                bad_dates = _apply_csv_workable_dates(target, row)
                if bad_dates:
                    errors.append(f"{idx}行目: 不正な日付を無視しました: {', '.join(bad_dates)}")

            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.error("CSV取り込み中にエラーが発生しました", exc_info=True)
            flash("CSV取り込み中にエラーが発生しました。内容を確認してください。", "error")
            return redirect(url_for("staff_list"))

        msg = f"CSV取り込み完了（{'全置き換え' if mode == 'replace' else '差分'}）: 追加{added}・更新{updated}・スキップ{skipped}。"
        if unknown_quals:
            msg += f" 未知の資格は無視しました: {', '.join(sorted(unknown_quals))}。"
        flash(msg, "success")
        for e in errors[:10]:
            flash(e, "error")
        return redirect(url_for("staff_list"))

    # -----------------------------------------------------------------
    # API ルート — 休み希望
    # -----------------------------------------------------------------
    @app.route("/api/staff/<int:staff_id>/dayoff", methods=["POST"])
    def api_dayoff_create(staff_id):
        """休み希望の追加"""
        Staff.query.get_or_404(staff_id)
        data = request.get_json()
        if not data or "date" not in data:
            return jsonify({"error": "date は必須です"}), 400

        try:
            req_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "日付の形式が正しくありません (YYYY-MM-DD)"}), 400

        existing = DayOffRequest.query.filter_by(staff_id=staff_id, date=req_date).first()
        if existing:
            return jsonify({"error": "この日付の休み希望は既に登録されています"}), 409

        day_off = DayOffRequest(staff_id=staff_id, date=req_date)
        db.session.add(day_off)
        db.session.commit()
        return jsonify(day_off.to_dict()), 201

    @app.route("/api/staff/<int:staff_id>/dayoff/<int:dayoff_id>", methods=["DELETE"])
    def api_dayoff_delete(staff_id, dayoff_id):
        """休み希望の削除"""
        day_off = DayOffRequest.query.filter_by(id=dayoff_id, staff_id=staff_id).first_or_404()
        db.session.delete(day_off)
        db.session.commit()
        return jsonify({"message": "削除しました"}), 200

    @app.route("/api/staff/<int:staff_id>/dayoffs", methods=["GET"])
    def api_dayoff_list(staff_id):
        """休み希望一覧"""
        Staff.query.get_or_404(staff_id)
        day_offs = (
            DayOffRequest.query.filter_by(staff_id=staff_id)
            .order_by(DayOffRequest.date)
            .all()
        )
        return jsonify([d.to_dict() for d in day_offs])

    # -----------------------------------------------------------------
    # API ルート — 出勤可能日（whitelist）
    # -----------------------------------------------------------------
    @app.route("/api/staff/<int:staff_id>/workable-date", methods=["POST"])
    def api_workable_date_create(staff_id):
        """出勤可能日の追加"""
        Staff.query.get_or_404(staff_id)
        data = request.get_json()
        if not data or "date" not in data:
            return jsonify({"error": "date は必須です"}), 400
        try:
            d = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "日付の形式が正しくありません (YYYY-MM-DD)"}), 400

        existing = StaffWorkableDate.query.filter_by(staff_id=staff_id, date=d).first()
        if existing:
            return jsonify({"error": "この日付は既に登録されています"}), 409

        wd = StaffWorkableDate(staff_id=staff_id, date=d)
        db.session.add(wd)
        db.session.commit()
        return jsonify(wd.to_dict()), 201

    @app.route("/api/staff/<int:staff_id>/workable-date/<int:wd_id>", methods=["DELETE"])
    def api_workable_date_delete(staff_id, wd_id):
        """出勤可能日の削除"""
        wd = StaffWorkableDate.query.filter_by(id=wd_id, staff_id=staff_id).first_or_404()
        db.session.delete(wd)
        db.session.commit()
        return jsonify({"message": "削除しました"}), 200

    @app.route("/api/staff/<int:staff_id>/workable-mode", methods=["POST"])
    def api_workable_mode_update(staff_id):
        """出勤可能日の扱いを切り替える（only=その日だけ / extra=通常に加えて出勤）。"""
        staff = Staff.query.get_or_404(staff_id)
        data = request.get_json(silent=True) or {}
        mode = (data.get("mode") or request.form.get("mode") or "").strip()
        if mode not in ("only", "extra"):
            return jsonify({"error": "mode は only / extra のいずれかです"}), 400
        staff.workable_dates_mode = mode
        db.session.commit()
        return jsonify({"staff_id": staff_id, "workable_dates_mode": mode}), 200

    @app.route("/api/staff/<int:staff_id>/workable-dates", methods=["GET"])
    def api_workable_date_list(staff_id):
        """出勤可能日一覧"""
        Staff.query.get_or_404(staff_id)
        dates = (
            StaffWorkableDate.query.filter_by(staff_id=staff_id)
            .order_by(StaffWorkableDate.date)
            .all()
        )
        return jsonify([w.to_dict() for w in dates])

    # -----------------------------------------------------------------
    # API ルート — シフト設定
    # -----------------------------------------------------------------
    @app.route("/api/settings", methods=["POST"])
    def settings_update():
        """シフト条件設定の更新"""
        s = ShiftSettings.query.first()
        if s is None:
            s = ShiftSettings()
            db.session.add(s)

        # デイの最低/最大配置人数は「曜日ごとの介護配置人数」に統合したためフォームから削除。
        #   既存値は曜日設定が空の曜日のフォールバックとしてそのまま残す。
        if "min_day_service" in request.form:
            s.min_day_service = safe_int(request.form.get("min_day_service"), 4)
        s.min_visit_am = safe_int(request.form.get("min_visit_am"), 1)
        s.min_visit_pm = safe_int(request.form.get("min_visit_pm"), 1)
        # 依頼文35: 兼務者最低人数(min_dual_assignment)は削除（フォーム保存しない）。
        s.min_early_staff = safe_int(request.form.get("min_early_staff"), 1)
        s.min_late_staff = safe_int(request.form.get("min_late_staff"), 1)
        s.closed_days = ",".join(request.form.getlist("closed_days"))
        # 休業日（日付指定・主に年末年始）。YYYY-MM-DD のみ受け付け、重複除去して昇順保存。
        _closed_dates = set()
        for _raw in request.form.getlist("closed_dates"):
            _raw = (_raw or "").strip()
            if not _raw:
                continue
            try:
                _closed_dates.add(datetime.strptime(_raw, "%Y-%m-%d").date().isoformat())
            except ValueError:
                continue  # 不正な日付は黙って捨てる（フォームは date 入力なので通常起きない）
        s.closed_dates = ",".join(sorted(_closed_dates))
        # --- 階別の営業曜日（これが設定の正）---
        def _form_dows(field):
            return sorted({
                int(x) for x in request.form.getlist(field)
                if x.strip().isdigit() and 0 <= int(x) <= 6
            })

        _f3_ds = _form_dows("floor3_day_service_days")
        _f3_v = _form_dows("floor3_visit_days")
        _f2_ds = _form_dows("floor2_day_service_days")
        _f2_v = _form_dows("floor2_visit_days")
        _ext_ds = _form_dows("external_day_service_days")
        s.floor3_day_service_days = ",".join(str(i) for i in _f3_ds)
        s.floor3_visit_days = ",".join(str(i) for i in _f3_v)
        s.floor2_day_service_days = ",".join(str(i) for i in _f2_ds)
        s.floor2_visit_days = ",".join(str(i) for i in _f2_v)
        s.external_day_service_days = ",".join(str(i) for i in _ext_ds)

        # --- 派生値（シフト自動作成が参照する）---
        #   デイ／訪問の営業曜日は 2階∪3階。外部デイは内部人員が不要なので含めない。
        #   no_day_service_days（デイ人員を緩和する曜日）はデイ営業日の裏返し。
        _ds_days = sorted(set(_f3_ds) | set(_f2_ds))
        _v_days = sorted(set(_f3_v) | set(_f2_v))
        s.day_service_operating_days = ",".join(str(i) for i in _ds_days)
        s.visit_operating_days = ",".join(str(i) for i in _v_days)
        s.no_day_service_days = ",".join(str(i) for i in range(7) if i not in _ds_days)
        # --- 曜日ごとの介護配置人数（その日出勤する介護職員の総数・看護師/PT除く）---
        #   care_min_wd_0..6 / care_max_wd_0..6。空欄はその曜日「指定なし」。
        def _form_wd_counts(prefix):
            vals = []
            any_set = False
            for wd in range(7):
                raw = (request.form.get(f"{prefix}_{wd}", "") or "").strip()
                if raw == "":
                    vals.append("")
                    continue
                n = safe_int(raw, None)
                if n is None or n < 0:
                    vals.append("")
                    continue
                any_set = True
                vals.append(str(n))
            return ",".join(vals) if any_set else ""

        s.care_min_by_weekday = _form_wd_counts("care_min_wd")
        s.care_max_by_weekday = _form_wd_counts("care_max_wd")
        s.min_cooking_staff = safe_int(request.form.get("min_cooking_staff"), 1)
        # 「調理 引き継ぎ時間帯の重複人数」は撤廃（ユーザー依頼 2026-08）
        s.min_cooking_overlap = 0
        s.breakfast_off_start = (request.form.get("breakfast_off_start", "") or "").strip()
        s.breakfast_off_end = (request.form.get("breakfast_off_end", "") or "").strip()
        s.am_preferred_gender = request.form.get("am_preferred_gender", "")
        s.phone_duty_enabled = "phone_duty_enabled" in request.form
        s.oncall_requires_work = "oncall_requires_work" in request.form
        # 閲覧専用ページのパスワード（空欄＝変更なし／「解除」で無効化）
        _vp = (request.form.get("viewer_password") or "").strip()
        if "viewer_password_clear" in request.form:
            s.viewer_password_hash = ""
        elif _vp:
            s.viewer_password_hash = generate_password_hash(_vp)
        # 役員用のパスワード（空欄＝変更なし／「解除」で無効化）
        _ep = (request.form.get("exec_password") or "").strip()
        if "exec_password_clear" in request.form:
            s.exec_password_hash = ""
        elif _ep:
            s.exec_password_hash = generate_password_hash(_ep)
        # 事務用のパスワード（空欄＝変更なし／「解除」で無効化）
        _op = (request.form.get("office_password") or "").strip()
        if "office_password_clear" in request.form:
            s.office_password_hash = ""
        elif _op:
            s.office_password_hash = generate_password_hash(_op)
        s.phone_duty_max_consecutive = safe_int(request.form.get("phone_duty_max_consecutive"), 1)
        s.min_staff_at_9 = safe_int(request.form.get("min_staff_at_9"), 4)
        s.min_staff_at_15 = safe_int(request.form.get("min_staff_at_15"), 4)
        s.male_am_constraint_mode = request.form.get("male_am_constraint_mode", "hard")
        if "max_day_service" in request.form:
            s.max_day_service = safe_int(request.form.get("max_day_service"), 0)
        # 依頼文35: 相談員ローテーション(counselor_desk_enabled / counselor_desk_count)は削除。
        # 調理 新人×ベテランのペア成立回数の目標値（依頼文28・0=無効）
        s.cooking_pair_target = max(0, safe_int(request.form.get("cooking_pair_target"), 0))
        # 相談員の介護業務参加モード（依頼文32・off/soft/hard、既定off）
        _ccm = (request.form.get("counselor_care_mode", "off") or "off").strip().lower()
        s.counselor_care_mode = _ccm if _ccm in ("off", "soft", "hard") else "off"
        # 依頼文40: 中介助/外介助の最低人数・連日回避モード・早遅連日回避モード
        s.min_bath_mid = max(0, safe_int(request.form.get("min_bath_mid"), 0))
        s.min_bath_out = max(0, safe_int(request.form.get("min_bath_out"), 0))
        _bram = (request.form.get("bath_role_alt_mode", "off") or "off").strip().lower()
        s.bath_role_alt_mode = _bram if _bram in ("off", "soft", "hard") else "off"
        # 「早番/遅番 連日回避」は廃止（依頼により削除）。
        # 遅番を中介助とするモード（off/soft/hard・既定hard＝従来動作）
        _lam = (request.form.get("late_as_mid_mode", "hard") or "hard").strip().lower()
        s.late_as_mid_mode = _lam if _lam in ("off", "soft", "hard") else "hard"
        # 依頼文41-(1): 遅番×オンコール禁止モード（off/soft/hard、既定off）
        _lom = (request.form.get("late_oncall_mode", "off") or "off").strip().lower()
        s.late_oncall_mode = _lom if _lom in ("off", "soft", "hard") else "off"
        # 依頼文41-(2): 訪問回数の平等化モード（off/soft/hard、既定soft）
        _vfm = (request.form.get("visit_fairness_mode", "soft") or "soft").strip().lower()
        s.visit_fairness_mode = _vfm if _vfm in ("off", "soft", "hard") else "soft"
        s.visit_fairness_max = max(0, safe_int(request.form.get("visit_fairness_max"), 1))
        # 看護師・PT を早番/遅番に入れないか（off/hard、既定hard）
        _nel = (request.form.get("nurse_early_late_mode", "hard") or "hard").strip().lower()
        s.nurse_early_late_mode = _nel if _nel in ("off", "hard") else "hard"
        # 遅番の連日回避モード（off/soft/hard、既定soft）
        _lcm = (request.form.get("late_consecutive_mode", "soft") or "soft").strip().lower()
        s.late_consecutive_mode = _lcm if _lcm in ("off", "soft", "hard") else "soft"
        # 遅番日数の平等化モード（off/soft/hard、既定soft）
        _lfm = (request.form.get("late_fairness_mode", "soft") or "soft").strip().lower()
        s.late_fairness_mode = _lfm if _lfm in ("off", "soft", "hard") else "soft"
        # 依頼文42: 早番の連日回避モード（off/soft/hard、既定soft）
        _ecm = (request.form.get("early_consecutive_mode", "soft") or "soft").strip().lower()
        s.early_consecutive_mode = _ecm if _ecm in ("off", "soft", "hard") else "soft"
        # 依頼文43: 早番日数の平等化モード（off/soft/hard、既定soft）
        _efm = (request.form.get("early_fairness_mode", "soft") or "soft").strip().lower()
        s.early_fairness_mode = _efm if _efm in ("off", "soft", "hard") else "soft"
        # 依頼文43: 早番/遅番平等化の hard 上限 spread（max−min ≤ N、1以上）
        s.early_fairness_max = max(0, safe_int(request.form.get("early_fairness_max"), 1))
        s.late_fairness_max = max(0, safe_int(request.form.get("late_fairness_max"), 1))
        # 依頼文43: オンコール回数の平等化モード（off/soft/hard、既定soft）＋hard上限
        _ofm = (request.form.get("oncall_fairness_mode", "soft") or "soft").strip().lower()
        s.oncall_fairness_mode = _ofm if _ofm in ("off", "soft", "hard") else "soft"
        s.oncall_fairness_max = max(0, safe_int(request.form.get("oncall_fairness_max"), 1))
        # 公休日数の自動算出（法定労働時間ベース）
        s.auto_public_holidays = "auto_public_holidays" in request.form
        s.auto_ph_include_holidays = "auto_ph_include_holidays" in request.form
        try:
            _dwh = float(request.form.get("daily_work_hours", 8.0) or 8.0)
        except (TypeError, ValueError):
            _dwh = 8.0
        s.daily_work_hours = min(24.0, max(0.5, _dwh))
        db.session.commit()
        flash("条件設定を保存しました。", "success")
        return redirect(url_for("settings"))

    # -----------------------------------------------------------------
    # API ルート — 資格マスタ
    # -----------------------------------------------------------------
    @app.route("/api/qualifications", methods=["GET"])
    def api_qualifications_list():
        """資格一覧"""
        quals = Qualification.query.order_by(Qualification.display_order).all()
        return jsonify([q.to_dict() for q in quals])

    @app.route("/api/qualifications", methods=["POST"])
    def api_qualification_create():
        """資格追加"""
        data = request.get_json()
        if not data or not data.get("name") or not data.get("code"):
            return jsonify({"error": "code と name は必須です"}), 400
        if Qualification.query.filter_by(code=data["code"]).first():
            return jsonify({"error": "このコードは既に使用されています"}), 409
        q = Qualification(
            code=data["code"],
            name=data["name"],
            display_order=data.get("display_order", 0),
        )
        db.session.add(q)
        db.session.commit()
        return jsonify(q.to_dict()), 201

    @app.route("/api/qualifications/<int:qual_id>", methods=["DELETE"])
    def api_qualification_delete(qual_id):
        """資格削除"""
        q = Qualification.query.get_or_404(qual_id)
        db.session.delete(q)
        db.session.commit()
        return jsonify({"message": "削除しました"}), 200

    # -----------------------------------------------------------------
    # API ルート — 駐車枠マスタ（依頼文24）
    # -----------------------------------------------------------------
    @app.route("/api/parking_slots", methods=["GET"])
    def api_parking_slots_list():
        """駐車枠一覧（表示順）"""
        slots = ParkingSlot.query.order_by(ParkingSlot.display_order, ParkingSlot.id).all()
        return jsonify([s.to_dict() for s in slots])

    @app.route("/api/parking_slots", methods=["POST"])
    def api_parking_slot_create():
        """駐車枠の追加"""
        data = request.get_json(silent=True) or {}
        num = str(data.get("slot_number", "")).strip()
        if not num:
            return jsonify({"error": "枠番号は必須です"}), 400
        if ParkingSlot.query.filter_by(slot_number=num).first():
            return jsonify({"error": "この枠番号は既に登録されています"}), 409
        max_order = db.session.query(db.func.max(ParkingSlot.display_order)).scalar()
        s = ParkingSlot(slot_number=num, display_order=(max_order or 0) + 1)
        db.session.add(s)
        db.session.commit()
        return jsonify(s.to_dict()), 201

    @app.route("/api/parking_slots/<int:slot_id>", methods=["PUT"])
    def api_parking_slot_update(slot_id):
        """駐車枠の編集（番号変更）"""
        s = ParkingSlot.query.get_or_404(slot_id)
        data = request.get_json(silent=True) or {}
        num = str(data.get("slot_number", "")).strip()
        if not num:
            return jsonify({"error": "枠番号は必須です"}), 400
        dup = ParkingSlot.query.filter_by(slot_number=num).first()
        if dup and dup.id != slot_id:
            return jsonify({"error": "この枠番号は既に登録されています"}), 409
        s.slot_number = num
        db.session.commit()
        return jsonify(s.to_dict()), 200

    @app.route("/api/parking_slots/<int:slot_id>", methods=["DELETE"])
    def api_parking_slot_delete(slot_id):
        """駐車枠の削除"""
        s = ParkingSlot.query.get_or_404(slot_id)
        db.session.delete(s)
        db.session.commit()
        return jsonify({"message": "削除しました"}), 200

    # -----------------------------------------------------------------
    # API ルート — 配置ルール
    # -----------------------------------------------------------------
    @app.route("/api/placement_rules", methods=["GET"])
    def api_placement_rules_list():
        """配置ルール一覧"""
        rules = PlacementRule.query.order_by(PlacementRule.id).all()
        return jsonify([r.to_dict() for r in rules])

    @app.route("/api/placement_rules", methods=["POST"])
    def api_placement_rule_create():
        """配置ルール追加"""
        data = request.get_json()
        if not data or not data.get("name"):
            return jsonify({"error": "name は必須です"}), 400
        # 適用時間帯は時刻入力（time_start/time_end）が主。period は時刻から分類して同期。
        time_start = (data.get("time_start", "") or "").strip()
        time_end = (data.get("time_end", "") or "").strip()
        period = _period_from_time_window(time_start, time_end) or data.get("period", "all")
        rule = PlacementRule(
            name=data["name"],
            rule_type=data.get("rule_type", "qualification_min"),
            target_qualification_ids_json=json.dumps(data.get("target_qualification_ids", [])),
            target_gender=data.get("target_gender", ""),
            period=period,
            time_start=time_start,
            time_end=time_end,
            min_count=data.get("min_count", 1),
            is_hard=data.get("is_hard", True),
            penalty_weight=data.get("penalty_weight", 100),
            apply_weekdays=data.get("apply_weekdays", "0,1,2,3,4,5,6"),
            is_active=data.get("is_active", True),
        )
        db.session.add(rule)
        db.session.commit()
        return jsonify(rule.to_dict()), 201

    @app.route("/api/placement_rules/<int:rule_id>", methods=["PUT"])
    def api_placement_rule_update(rule_id):
        """配置ルール更新"""
        rule = PlacementRule.query.get_or_404(rule_id)
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSONボディが不正です"}), 400
        if data.get("name") is not None:
            rule.name = data["name"]
        if data.get("rule_type") is not None:
            rule.rule_type = data["rule_type"]
        if data.get("target_qualification_ids") is not None:
            rule.target_qualification_ids_json = json.dumps(data["target_qualification_ids"])
        if data.get("target_gender") is not None:
            rule.target_gender = data["target_gender"]
        if data.get("period") is not None:
            rule.period = data["period"]
        # 適用時間帯（時刻入力）。指定があれば time_start/end を更新し period も同期。
        if data.get("time_start") is not None:
            rule.time_start = (data["time_start"] or "").strip()
        if data.get("time_end") is not None:
            rule.time_end = (data["time_end"] or "").strip()
        if data.get("time_start") is not None or data.get("time_end") is not None:
            derived = _period_from_time_window(rule.time_start, rule.time_end)
            if derived:
                rule.period = derived
        if data.get("min_count") is not None:
            rule.min_count = data["min_count"]
        if data.get("is_hard") is not None:
            rule.is_hard = data["is_hard"]
        if data.get("penalty_weight") is not None:
            rule.penalty_weight = data["penalty_weight"]
        if data.get("is_active") is not None:
            rule.is_active = data["is_active"]
        if data.get("apply_weekdays") is not None:
            rule.apply_weekdays = data["apply_weekdays"]
        db.session.commit()
        return jsonify(rule.to_dict())

    @app.route("/api/placement_rules/<int:rule_id>", methods=["DELETE"])
    def api_placement_rule_delete(rule_id):
        """配置ルール削除"""
        rule = PlacementRule.query.get_or_404(rule_id)
        db.session.delete(rule)
        db.session.commit()
        return jsonify({"message": "削除しました"}), 200

    # -----------------------------------------------------------------
    # API ルート — 調理組み合わせルール
    # -----------------------------------------------------------------
    @app.route("/api/cooking_combo_rules", methods=["GET"])
    def api_cooking_combo_list():
        """調理組み合わせルール一覧"""
        rules = CookingComboRule.query.order_by(CookingComboRule.id).all()
        return jsonify([r.to_dict() for r in rules])

    @app.route("/api/cooking_combo_rules/<int:rule_id>", methods=["PUT"])
    def api_cooking_combo_update(rule_id):
        """調理組み合わせルール更新"""
        rule = CookingComboRule.query.get_or_404(rule_id)
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSONボディが不正です"}), 400
        if data.get("is_active") is not None:
            rule.is_active = data["is_active"]
        if data.get("allowed_patterns") is not None:
            rule.allowed_patterns_json = json.dumps(data["allowed_patterns"])
        if data.get("name") is not None:
            rule.name = data["name"]
        db.session.commit()
        return jsonify(rule.to_dict())

    @app.route("/api/cooking_combo_rules", methods=["POST"])
    def api_cooking_combo_create():
        """調理組み合わせ（1組=1行）を追加。allowed_patterns=種類コードのフラットリスト。"""
        data = request.get_json(silent=True) or {}
        patterns = data.get("allowed_patterns") or []
        if not isinstance(patterns, list) or not patterns:
            return jsonify({"error": "含める種類を1つ以上選択してください"}), 400
        rule = CookingComboRule(
            name=(data.get("name") or "").strip() or "新しい組み合わせ",
            allowed_patterns_json=json.dumps(patterns),
            is_active=data.get("is_active", True),
        )
        db.session.add(rule)
        db.session.commit()
        return jsonify(rule.to_dict()), 201

    @app.route("/api/cooking_combo_rules/<int:rule_id>", methods=["DELETE"])
    def api_cooking_combo_delete(rule_id):
        """調理組み合わせ行を削除。"""
        rule = CookingComboRule.query.get_or_404(rule_id)
        db.session.delete(rule)
        db.session.commit()
        return jsonify({"message": "削除しました"})

    # -----------------------------------------------------------------
    # API ルート — 調理シフト種類マスタ（ShiftPattern staff_group='cooking'）
    # -----------------------------------------------------------------
    @app.route("/api/cooking_types", methods=["GET"])
    def api_cooking_types_list():
        rows = (ShiftPattern.query.filter_by(staff_group="cooking")
                .order_by(ShiftPattern.display_order).all())
        return jsonify([r.to_dict() for r in rows])

    @app.route("/api/cooking_types", methods=["POST"])
    def api_cooking_type_create():
        """調理シフト種類を追加（コードは cooking_N を自動採番）。"""
        data = request.get_json(silent=True) or {}
        label = (data.get("label") or "").strip()
        start = (data.get("start_time") or "").strip()
        end = (data.get("end_time") or "").strip()
        if not label or not start or not end:
            return jsonify({"error": "名前・出勤時刻・退勤時刻は必須です"}), 400
        # 既存 cooking_N の最大Nの次を採番
        max_n = 0
        for p in ShiftPattern.query.filter_by(staff_group="cooking").all():
            m = re.match(r"^cooking_(\d+)$", p.code or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
        code = f"cooking_{max_n + 1}"
        max_order = db.session.query(db.func.max(ShiftPattern.display_order)).scalar() or 0
        p = ShiftPattern(
            code=code, staff_group="cooking", label=label,
            start_time=start, end_time=end, has_break=False, break_minutes=0,
            display_order=max_order + 1, period="full", covers_am=True, covers_pm=True,
            counts_as_cooking=bool(data.get("counts_as_cooking", True)),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify(p.to_dict()), 201

    @app.route("/api/cooking_types/<int:type_id>", methods=["PUT"])
    def api_cooking_type_update(type_id):
        """調理シフト種類の名前・時刻を編集（コードは不変）。"""
        p = ShiftPattern.query.get_or_404(type_id)
        if p.staff_group != "cooking":
            return jsonify({"error": "調理種類ではありません"}), 400
        data = request.get_json(silent=True) or {}
        if data.get("label") is not None:
            p.label = (data["label"] or "").strip() or p.label
        if data.get("start_time") is not None:
            p.start_time = (data["start_time"] or "").strip()
        if data.get("end_time") is not None:
            p.end_time = (data["end_time"] or "").strip()
        if data.get("counts_as_cooking") is not None:
            p.counts_as_cooking = bool(data["counts_as_cooking"])
        db.session.commit()
        return jsonify(p.to_dict())

    @app.route("/api/cooking_types/<int:type_id>", methods=["DELETE"])
    def api_cooking_type_delete(type_id):
        """調理シフト種類を削除（組み合わせで使用中なら拒否）。"""
        p = ShiftPattern.query.get_or_404(type_id)
        if p.staff_group != "cooking":
            return jsonify({"error": "調理種類ではありません"}), 400
        for r in CookingComboRule.query.all():
            try:
                pats = json.loads(r.allowed_patterns_json or "[]")
            except (ValueError, TypeError):
                pats = []
            flat = []
            for g in (pats if (pats and all(isinstance(x, list) for x in pats)) else [pats]):
                flat.extend(g if isinstance(g, list) else [g])
            if p.code in flat:
                return jsonify({"error": f"組み合わせ「{r.name}」で使用中のため削除できません"}), 409
        db.session.delete(p)
        db.session.commit()
        return jsonify({"message": "削除しました"})

    # -----------------------------------------------------------------
    # API ルート — シフト生成
    # -----------------------------------------------------------------
    @app.route("/api/generate", methods=["POST"])
    def api_generate():
        """シフト生成を実行し結果を DB に保存"""
        if not generate_lock.acquire(blocking=False):
            return jsonify({"error": "シフト生成が既に実行中です。完了までお待ちください。"}), 429

        try:
            return _do_generate()
        finally:
            generate_lock.release()

    def _do_generate():
        data = request.get_json()
        if not data or "year" not in data or "month" not in data:
            return jsonify({"error": "year と month は必須です"}), 400

        try:
            year = int(data["year"])
            month = int(data["month"])
        except (TypeError, ValueError):
            return jsonify({"error": "year と month は整数で指定してください"}), 400

        if month < 1 or month > 12:
            return jsonify({"error": "month は 1〜12 で指定してください"}), 400

        if year < 2000 or year > 2100:
            return jsonify({"error": "year は 2000〜2100 の範囲で指定してください"}), 400

        # 確定済みの月は再生成（上書き）をブロック。確定解除が必要。
        if ShiftConfirmation.query.filter_by(year=year, month=month).first():
            return jsonify({
                "error": "この月は確定済みのため再生成できません。再生成するには先に「確定解除」してください。",
                "confirmed": True,
            }), 409

        # 休職中の職員は生成対象から除外（オンコールも含め一切割り当てない）。
        staffs = _active_staff_for_month(year, month)
        # 役員は自動作成の対象外（名前だけ表に出して休みを手入力する）
        staffs = [st for st in staffs
                  if not _is_plan_only_staff(st)]
        if not staffs:
            return jsonify({"error": "職員が登録されていません"}), 400

        settings_obj = ShiftSettings.query.first()
        day_off_requests = DayOffRequest.query.all()

        # 資格データの取得
        staff_qual_map, staff_qual_names, staff_qual_codes = _build_staff_qualification_maps()

        # 配置ルールの取得
        placement_rules = PlacementRule.query.filter_by(is_active=True).all()
        placement_rules_data = [r.to_dict() for r in placement_rules]

        # W-2: male_am_constraint_mode に応じて「男性 午前」ルールを動的制御
        male_am_mode = getattr(settings_obj, 'male_am_constraint_mode', 'hard') or 'hard'
        for pr in placement_rules_data:
            if pr.get("rule_type") == "gender_min" and pr.get("target_gender") == "male" and pr.get("period") == "am":
                if male_am_mode == "off":
                    pr["is_active"] = False
                elif male_am_mode == "hard":
                    pr["is_hard"] = True
                else:  # soft
                    pr["is_hard"] = False
                    pr["penalty_weight"] = max(pr.get("penalty_weight", 100), 100)

        # 調理組み合わせルールの取得
        cooking_combo_rules = CookingComboRule.query.filter_by(is_active=True).all()
        cooking_combo_data = [r.to_dict() for r in cooking_combo_rules]

        # 依頼文21: 調理シフト種類マスタ（ShiftPattern cooking）を solver へ渡す
        cooking_types_data = [
            {"code": p.code, "label": p.label,
             "start_time": p.start_time, "end_time": p.end_time,
             "counts_as_cooking": (
                 p.counts_as_cooking if p.counts_as_cooking is not None else True
             )}
            for p in ShiftPattern.query.filter_by(staff_group="cooking")
            .order_by(ShiftPattern.display_order).all()
        ]

        # 許可アサインメント制限の取得
        all_allowed = StaffAllowedPattern.query.all()
        allowed_patterns_map = {}  # {staff_id: set(assignment_codes)}
        for ap in all_allowed:
            if ap.staff_id not in allowed_patterns_map:
                allowed_patterns_map[ap.staff_id] = set()
            # 依頼文22: 旧調理コード cook_* が残っていても cooking_* に読み替える（防御）
            code = _COOK_CODE_MIGRATE.get(ap.assignment_code, ap.assignment_code)
            allowed_patterns_map[ap.staff_id].add(code)

        # 出勤可能日（whitelist）の取得: {staff_id: [YYYY-MM-DD, ...]}
        # 1日でも登録があれば、その職員は登録日のみ出勤可（solverで強制）。
        #   扱いは職員ごとの workable_dates_mode:
        #     only  … 登録日のみ出勤（従来）
        #     extra … 通常のシフトに加えて、その日は必ず出勤（振替出勤）
        _wd_mode = {
            st.id: (getattr(st, "workable_dates_mode", "only") or "only")
            for st in Staff.query.all()
        }
        workable_dates_map = {}
        forced_work_dates = []      # [(staff_id, "YYYY-MM-DD"), ...] 追加出勤日
        for w in StaffWorkableDate.query.all():
            if _wd_mode.get(w.staff_id, "only") == "extra":
                forced_work_dates.append((w.staff_id, w.date.isoformat()))
            else:
                workable_dates_map.setdefault(w.staff_id, []).append(w.date.isoformat())

        # 公休日数の自動算出。ON時は手入力より優先。
        #   ユーザー依頼（2026-08）:「正社員の公休は土日を抜いた平日日数を出勤日にする」。
        #     正社員(週5)所定労働日数 = その月の平日日数（月〜金）。祝日は労働日扱い。
        #       例) 2026年9月 = 平日22日 → 公休8日（＝土日の日数）
        #     短時間: 週5から1日減るごとに所定労働日数を -4日
        #       （週4 = -4日, 週3 = -8日, 週2 = -12日 …）
        #     所定労働日数 = max(0, 正社員所定 − (5 − 週勤務日数) × 4)
        #     公休数 = 暦日数 − 所定労働日数
        auto_ph_enabled = bool(getattr(settings_obj, "auto_public_holidays", False))
        _calendar_days = calendar.monthrange(year, month)[1]
        _daily_hours = float(getattr(settings_obj, "daily_work_hours", 8.0) or 8.0)
        if _daily_hours <= 0:
            _daily_hours = 8.0
        # 正社員(週5)基準の所定労働日数＝その月の平日日数（月〜金）。
        #   「祝日も公休に含める」がONなら平日から祝日を除く（＝その分公休が増える）。
        _ph_include_holidays = bool(
            getattr(settings_obj, "auto_ph_include_holidays", False)
        )
        _fulltime_shotei = sum(
            1 for _d in range(1, _calendar_days + 1)
            if date(year, month, _d).weekday() < 5
            and not (_ph_include_holidays and jpholiday.is_holiday(date(year, month, _d)))
        )

        # 休業曜日・休業日（この時点では settings_dict 未構築なので設定から直接読む）
        _closed_wd_for_ph = {
            int(x) for x in (settings_obj.closed_days or "").split(",") if x.strip()
        }
        _closed_iso_for_ph = {
            x.strip() for x in (getattr(settings_obj, "closed_dates", "") or "").split(",")
            if x.strip()
        }

        def _max_workable_days(s):
            """その職員が当月に物理的に出勤しうる最大日数。

            勤務可能曜日・固定休・休業日（曜日/日付）・祝日不可・週の勤務日数上限を
            すべて考慮する。ユーザー指摘（2026-08）:「池田さんは固定休が火木土なのに
            公休目標が週5相当（月22日出勤）になっていて、目標がそもそも達成不能」。
            """
            avail = {int(x) for x in (s.available_days or "").split(",") if x.strip()}
            fixed = {int(x) for x in (s.fixed_days_off or "").split(",") if x.strip()}
            hol_ng = bool(getattr(s, "holiday_ng", False))
            week_cap = s.max_days_per_week or 7
            by_week = {}
            for _d in range(1, _calendar_days + 1):
                dt = date(year, month, _d)
                iso = dt.isoformat()
                if avail and dt.weekday() not in avail:
                    continue
                if dt.weekday() in fixed:
                    continue
                if dt.weekday() in _closed_wd_for_ph or iso in _closed_iso_for_ph:
                    continue
                if hol_ng and jpholiday.is_holiday(dt):
                    continue
                by_week.setdefault(dt.isocalendar()[1], 0)
                by_week[dt.isocalendar()[1]] += 1
            return sum(min(n, week_cap) for n in by_week.values())

        def _effective_public_holidays(s):
            """auto_ph_enabled時は正社員基準＋短時間補正の公休日数を返す（手入力より優先）。

            出勤可能日(whitelist)を登録した職員は、その登録日数を出勤日数の上限とみなす
            （ユーザー指摘 2026-08:「土山さんは5日と26日しか出ない設定なのに入っていない」。
            以前は目標を0＝対象外にしていたため、ソルバーが入れなくても良いと判断していた）。
            """
            if not auto_ph_enabled:
                return getattr(s, "public_holiday_count", 0) or 0
            week_days = s.max_days_per_week or 5
            # 週5から1日減るごとに所定労働日数を -4日（週4=-4, 週3=-8 …）
            reduction = max(0, 5 - week_days) * 4
            shotei_work_days = max(0, _fulltime_shotei - reduction)
            # 固定休・勤務可能曜日・休業日で物理的に出られない分は目標から差し引く
            shotei_work_days = min(shotei_work_days, _max_workable_days(s))
            # 出勤可能日(whitelist)を登録している職員はその日数が上限
            _wl = workable_dates_map.get(s.id)
            if _wl:
                shotei_work_days = min(shotei_work_days, len(_wl))
            return max(0, _calendar_days - shotei_work_days)

        # ORM → dict 変換（部門別に分割）
        care_dicts = []
        cook_dicts = []
        for s in staffs:
            # 「オンコールのみ当番」の職員は出勤シフトを一切割り当てない
            #   （オンコールのローテーションには通常どおり参加する）。
            if getattr(s, "oncall_only", False):
                continue
            avail_days = [int(x) for x in s.available_days.split(",") if x.strip()]
            fixed_off = [int(x) for x in s.fixed_days_off.split(",") if x.strip()] if s.fixed_days_off else []
            d = {
                "id": s.id,
                "name": s.name,
                "employment_type": s.employment_type,
                "can_visit": s.can_visit,
                "max_consecutive_days": s.max_consecutive_days,
                "max_days_per_week": s.max_days_per_week,
                "min_days_per_week": getattr(s, "min_days_per_week", 0) or 0,
                "available_days": avail_days,
                "available_time_slots": s.available_time_slots,
                "fixed_days_off": fixed_off,
                "required_days": [
                    int(x) for x in (getattr(s, "required_days", "") or "").split(",")
                    if x.strip().isdigit()
                ],
                "staff_group": s.staff_group,
                "job_category": getattr(s, "job_category", "caregiver") or "caregiver",
                # 応援（人手が足りないときだけ入れる）
                "backup_only": bool(getattr(s, "backup_only", False)),
                "gender": s.gender,
                "has_phone_duty": s.has_phone_duty,
                "can_bath_assist": getattr(s, "can_bath_assist", False) or False,
                "qualification_ids": staff_qual_map.get(s.id, []),
                "qualification_names": staff_qual_names.get(s.id, []),
                "qualification_codes": staff_qual_codes.get(s.id, []),
                "weekend_constraint": getattr(s, "weekend_constraint", "") or "",
                "holiday_ng": getattr(s, "holiday_ng", False) or False,
                "workable_dates": workable_dates_map.get(s.id, []),
                "work_start_time": getattr(s, "work_start_time", "") or "",
                "work_end_time": getattr(s, "work_end_time", "") or "",
                "cooking_experience": getattr(s, "cooking_experience", "") or "",
                "first_work_date": (
                    s.first_work_date.isoformat()
                    if getattr(s, "first_work_date", None) else None
                ),
                "public_holiday_count": _effective_public_holidays(s),
            }
            if s.staff_group == "cooking":
                cook_dicts.append(d)
            else:
                care_dicts.append(d)

        dayoff_dicts = [
            {"staff_id": d.staff_id, "date": d.date}
            for d in day_off_requests
        ]

        closed_days = [int(x) for x in settings_obj.closed_days.split(",") if x.strip()] if settings_obj.closed_days else []
        closed_dates = [
            x.strip() for x in (getattr(settings_obj, "closed_dates", "") or "").split(",")
            if x.strip()
        ]
        visit_days = [int(x) for x in settings_obj.visit_operating_days.split(",") if x.strip()] if settings_obj.visit_operating_days else []
        no_ds_days = [int(x) for x in (settings_obj.no_day_service_days or "").split(",") if x.strip()] if getattr(settings_obj, "no_day_service_days", "") else []

        settings_dict = {
            "min_day_service": settings_obj.min_day_service,
            "min_visit_am": settings_obj.min_visit_am,
            "min_visit_pm": settings_obj.min_visit_pm,
            "min_early_staff": getattr(settings_obj, 'min_early_staff', 1) if getattr(settings_obj, 'min_early_staff', 1) is not None else 1,
            "min_late_staff": getattr(settings_obj, 'min_late_staff', 1) if getattr(settings_obj, 'min_late_staff', 1) is not None else 1,
            "closed_days": closed_days,
            "closed_dates": closed_dates,
            "visit_operating_days": visit_days,
            "no_day_service_days": no_ds_days,
            "forced_work_dates": forced_work_dates,
            "care_min_by_weekday": _parse_wd_counts(
                getattr(settings_obj, "care_min_by_weekday", "")
            ),
            "care_max_by_weekday": _parse_wd_counts(
                getattr(settings_obj, "care_max_by_weekday", "")
            ),
            "min_cooking_staff": settings_obj.min_cooking_staff,
            "breakfast_off_start": getattr(settings_obj, 'breakfast_off_start', '') or '',
            "breakfast_off_end": getattr(settings_obj, 'breakfast_off_end', '') or '',
            "am_preferred_gender": getattr(settings_obj, 'am_preferred_gender', '') or '',
            "phone_duty_enabled": getattr(settings_obj, 'phone_duty_enabled', False) or False,
            "phone_duty_max_consecutive": getattr(settings_obj, 'phone_duty_max_consecutive', 1) or 1,
            "min_staff_at_9": getattr(settings_obj, 'min_staff_at_9', 4) or 4,
            "min_staff_at_15": getattr(settings_obj, 'min_staff_at_15', 4) or 4,
            "male_am_constraint_mode": getattr(settings_obj, 'male_am_constraint_mode', 'hard') or 'hard',
            "max_day_service": getattr(settings_obj, 'max_day_service', 0) or 0,
            "counselor_care_mode": getattr(settings_obj, 'counselor_care_mode', 'off') or 'off',
            "min_bath_mid": getattr(settings_obj, 'min_bath_mid', 0) or 0,
            "min_bath_out": getattr(settings_obj, 'min_bath_out', 0) or 0,
            "bath_role_alt_mode": getattr(settings_obj, 'bath_role_alt_mode', 'off') or 'off',
            "late_as_mid_mode": getattr(settings_obj, 'late_as_mid_mode', 'hard') or 'hard',
            "late_oncall_mode": getattr(settings_obj, 'late_oncall_mode', 'off') or 'off',
            "nurse_early_late_mode": getattr(settings_obj, 'nurse_early_late_mode', 'hard') or 'hard',
            "visit_fairness_mode": getattr(settings_obj, 'visit_fairness_mode', 'soft') or 'soft',
            "visit_fairness_max": getattr(settings_obj, 'visit_fairness_max', 1) if getattr(settings_obj, 'visit_fairness_max', 1) is not None else 1,
            "late_consecutive_mode": getattr(settings_obj, 'late_consecutive_mode', 'soft') or 'soft',
            "late_fairness_mode": getattr(settings_obj, 'late_fairness_mode', 'soft') or 'soft',
            "late_fairness_max": getattr(settings_obj, 'late_fairness_max', 1) if getattr(settings_obj, 'late_fairness_max', 1) is not None else 1,
            "early_consecutive_mode": getattr(settings_obj, 'early_consecutive_mode', 'soft') or 'soft',
            "early_fairness_mode": getattr(settings_obj, 'early_fairness_mode', 'soft') or 'soft',
            "early_fairness_max": getattr(settings_obj, 'early_fairness_max', 1) if getattr(settings_obj, 'early_fairness_max', 1) is not None else 1,
            "oncall_fairness_mode": getattr(settings_obj, 'oncall_fairness_mode', 'soft') or 'soft',
            "oncall_fairness_max": getattr(settings_obj, 'oncall_fairness_max', 1) if getattr(settings_obj, 'oncall_fairness_max', 1) is not None else 1,
            "placement_rules": placement_rules_data,
            "cooking_combo_rules": cooking_combo_data,
            "cooking_types": cooking_types_data,
            "cooking_pair_target": getattr(settings_obj, 'cooking_pair_target', 0) or 0,
        }

        # --- オンコール（電話当番）を生成前に確定 ---
        #   オンコール当番の翌日休（forced off）をソルバーへ渡すため、解く前にローテーションを決める。
        month_dates = [
            date(year, month, d)
            for d in range(1, calendar.monthrange(year, month)[1] + 1)
        ]
        oncall_items, oncall_warnings = [], []
        if settings_dict.get("phone_duty_enabled"):
            # 休み希望: {staff_id: set(isoformat)}
            dayoff_by_staff = {}
            for r in day_off_requests:
                dayoff_by_staff.setdefault(r.staff_id, set()).add(r.date.isoformat())

            oncall_eligible = []
            # 「オンコールのみ当番」の職員は電話当番チェックの有無に関わらず対象に含める
            oncall_candidates = _active_staff_for_month(
                year, month,
                Staff.query.filter(
                    db.or_(Staff.has_phone_duty == True, Staff.oncall_only == True),  # noqa: E712
                ),
            )
            # オンコールは「その日出勤している職員」に割り当てる（電話を持ち帰るため）。
            #   ユーザー依頼（2026-08）:「オンコールは出勤してる職員しか電話を
            #   持って帰れない」「日曜日は例外。オンコール担当は土曜出勤が持って帰る」。
            #   → 休業日（日曜など）の当番は、その直前の営業日に出勤する職員が持ち帰る。
            #   例外（oncall_when_off_ok / オンコールのみ当番）は休みの日でも持てる。
            _oncall_requires_work = bool(
                getattr(settings_obj, "oncall_requires_work", True)
            )
            _closed_wd = set(closed_days)
            _closed_iso = set(closed_dates)

            def _is_closed_day(dt):
                return dt.weekday() in _closed_wd or dt.isoformat() in _closed_iso

            def _prev_open_day(dt):
                """休業日の当番を持ち帰る「直前の営業日」（当月内）。無ければ None。"""
                d = dt - timedelta(days=1)
                while d >= month_dates[0]:
                    if not _is_closed_day(d):
                        return d
                    d -= timedelta(days=1)
                return None

            must_work_ids = set()
            # 休業日の当番 → 実際に出勤させる日（前営業日）。{staff_id: {当番日: 出勤日}}
            oncall_carry_day = {}
            for st in oncall_candidates:
                avail_wd = set(int(x) for x in st.available_days.split(",") if x.strip())
                fixed_wd = (set(int(x) for x in st.fixed_days_off.split(",") if x.strip())
                            if st.fixed_days_off else set())
                wk = set(workable_dates_map.get(st.id, []))
                offs = dayoff_by_staff.get(st.id, set())
                hol_ng = bool(getattr(st, "holiday_ng", False))
                exempt = (
                    bool(getattr(st, "oncall_when_off_ok", False))
                    or bool(getattr(st, "oncall_only", False))
                    # 役員は勤務枠を持たないので「出勤者限定」の対象外
                    or _is_plan_only_staff(st)
                    or not _oncall_requires_work
                )
                if not exempt:
                    must_work_ids.add(st.id)

                def _can_work(dt):
                    """その日に出勤できるか（休み希望・勤務不可曜日・出勤可能日外・祝日不可）。"""
                    iso = dt.isoformat()
                    return not (
                        dt.weekday() not in avail_wd
                        or dt.weekday() in fixed_wd
                        or (wk and iso not in wk)
                        or iso in offs
                        or (hol_ng and jpholiday.is_holiday(dt))
                    )

                # オンコールに入れない日を集約
                unavailable = set()
                for dt in month_dates:
                    iso = dt.isoformat()
                    if exempt or not _is_closed_day(dt):
                        # 例外者＝従来どおり（出勤の有無と無関係／勤務不可日のみ除外）
                        # 出勤者限定＝その日に出勤できる職員だけ
                        if not _can_work(dt):
                            unavailable.add(iso)
                        continue
                    # 休業日（日曜など）は出勤者がいない。
                    #   → 直前の営業日（土曜など）に出勤する職員が電話を持ち帰る。
                    if iso in offs or (hol_ng and jpholiday.is_holiday(dt)):
                        unavailable.add(iso)
                        continue
                    prev = _prev_open_day(dt)
                    if prev is None or not _can_work(prev):
                        unavailable.add(iso)
                    else:
                        oncall_carry_day.setdefault(st.id, {})[iso] = prev.isoformat()
                oncall_eligible.append(
                    {"id": st.id, "name": st.name, "unavailable": unavailable}
                )
            oncall_items, oncall_warnings = assign_oncall(
                oncall_eligible,
                month_dates,
                max_consecutive=settings_dict.get("phone_duty_max_consecutive", 1),
                fairness_mode=settings_dict.get("oncall_fairness_mode", "soft"),
                fairness_max=settings_dict.get("oncall_fairness_max", 1),
            )
            # オンコール当番の翌日を強制休み（翌日が当月内のもののみ）
            last_dom = calendar.monthrange(year, month)[1]
            forced_off = []
            for it in oncall_items:
                d = it["date"]
                if d.day < last_dom:
                    forced_off.append((it["staff_id"], (d + timedelta(days=1)).isoformat()))
            settings_dict["oncall_forced_off"] = forced_off
            # オンコール当番日そのものを「勤務日」としてソルバーへ渡し、
            # 連勤上限のカウントに含める（オンコール込みで連勤上限を超えないように）。
            settings_dict["oncall_work_days"] = [
                (it["staff_id"], it["date"].isoformat()) for it in oncall_items
            ]
            # 出勤者限定の担当者は、その当番日に必ず出勤させる（電話を持ち帰るため）。
            #   休業日（日曜など）の当番は、直前の営業日（土曜など）に出勤させる。
            _must_work = []
            for it in oncall_items:
                if it["staff_id"] not in must_work_ids:
                    continue
                _iso = it["date"].isoformat()
                _target = oncall_carry_day.get(it["staff_id"], {}).get(_iso, _iso)
                _must_work.append((it["staff_id"], _target))
            settings_dict["oncall_must_work"] = _must_work

        # --- 固定職員（依頼文28）の既存シフトを読み込む ---
        #   固定職員は再生成の対象外。既存シフトをそのまま温存し、ソルバーには
        #   「埋まっている枠」としてピン留めで渡す（人数・在籍・重複の整合を保つ）。
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        fixed_ids = {
            f.staff_id for f in ShiftFix.query.filter_by(year=year, month=month).all()
        }
        fixed_rows = []
        locked_shifts = []
        if fixed_ids:
            fixed_rows = GeneratedShift.query.filter(
                GeneratedShift.date >= first_day,
                GeneratedShift.date <= last_day,
                GeneratedShift.staff_id.in_(fixed_ids),
            ).all()
            locked_shifts = [r.to_dict() for r in fixed_rows]

        # ソルバー実行（ケアと調理を独立して解く）
        try:
            shifts_data, warnings_data = generate_shift(
                year, month, care_dicts, cook_dicts, dayoff_dicts, settings_dict,
                allowed_patterns=allowed_patterns_map,
                locked_shifts=locked_shifts,
            )
        except Exception as e:
            app.logger.error("シフト生成中にエラーが発生しました", exc_info=True)
            return jsonify({"error": "シフト生成中にエラーが発生しました。設定や職員データを確認してください。"}), 500

        # 生成 ID を付与
        generation_id = str(uuid.uuid4())

        try:
            # 既存の同月シフトを削除（最新結果のみ保持）
            #   固定職員（依頼文28）の行は削除しない（既存シフトを温存）。
            gs_del = GeneratedShift.query.filter(
                GeneratedShift.date >= first_day,
                GeneratedShift.date <= last_day,
            )
            # 役員の予定は手入力なので、再生成でも消さない
            exec_ids = {
                st.id for st in Staff.query.all()
                if _is_plan_only_staff(st)
            }
            keep_ids = set(fixed_ids) | exec_ids
            if keep_ids:
                gs_del = gs_del.filter(~GeneratedShift.staff_id.in_(keep_ids))
            gs_del.delete(synchronize_session=False)
            ShiftWarning.query.filter(
                ShiftWarning.date >= first_day,
                ShiftWarning.date <= last_day,
            ).delete()
            OncallAssignment.query.filter(
                OncallAssignment.date >= first_day,
                OncallAssignment.date <= last_day,
            ).delete()
            ParkingAssignment.query.filter(
                ParkingAssignment.date >= first_day,
                ParkingAssignment.date <= last_day,
            ).delete()

            # シフト結果を DB に保存
            saved_shifts = []
            for item in shifts_data:
                shift_date = item["date"]
                if isinstance(shift_date, str):
                    shift_date = datetime.strptime(shift_date, "%Y-%m-%d").date()
                # ③ 相談員事務スロットをJSON文字列として保存
                desk_slots = item.get("counselor_desk_slots")
                desk_slots_json = json.dumps(desk_slots) if desk_slots else None
                shift = GeneratedShift(
                    generation_id=generation_id,
                    date=shift_date,
                    staff_id=item["staff_id"],
                    assignment=item["assignment"],
                    shift_pattern_code=item.get("shift_pattern_code"),
                    is_phone_duty=item.get("is_phone_duty", False),
                    break_start=item.get("break_start"),
                    counselor_desk_slots=desk_slots_json,
                    bath_role=item.get("bath_role"),
                    meal_assist=item.get("meal_assist"),
                )
                db.session.add(shift)
                saved_shifts.append(shift)

            # 警告を DB に保存
            saved_warnings = []
            for item in warnings_data:
                warn_date = item["date"]
                if isinstance(warn_date, str):
                    warn_date = datetime.strptime(warn_date, "%Y-%m-%d").date()
                warning = ShiftWarning(
                    generation_id=generation_id,
                    date=warn_date,
                    warning_type=item.get("warning_type", ""),
                    message=item.get("message", ""),
                )
                db.session.add(warning)
                saved_warnings.append(warning)

            # --- オンコール（電話当番）の保存：生成前に確定した結果を保存 ---
            #   当番者は翌日休（ソルバーへ forced_off として渡し済み）。
            for item in oncall_items:
                db.session.add(OncallAssignment(
                    generation_id=generation_id,
                    date=item["date"],
                    staff_id=item["staff_id"],
                ))
            for w in oncall_warnings:
                wd = w["date"]
                if isinstance(wd, str):
                    wd = datetime.strptime(wd, "%Y-%m-%d").date()
                warn = ShiftWarning(
                    generation_id=generation_id,
                    date=wd,
                    warning_type=w.get("warning_type", ""),
                    message=w.get("message", ""),
                )
                db.session.add(warn)
                saved_warnings.append(warn)

            # --- 固定職員（依頼文28）の既存行を新しい generation_id に付け替え ---
            #   シフト内容（assignment・入浴・食事・相談・休憩・電話当番）は一切変えず、
            #   月内の generation_id を統一するためだけに更新する。
            for r in fixed_rows:
                r.generation_id = generation_id

            # --- 駐車場の割り当て（依頼文24）：solver非干渉の後処理 ---
            #   各営業日に出勤する車通勤者へ枠を割り当て、溢れは「コイン」。
            #   固定職員も「出勤している枠」として駐車計算に含める。
            _save_parking_assignments(
                generation_id, year, month, shifts_data + locked_shifts
            )

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error("シフトデータの保存中にエラーが発生しました", exc_info=True)
            return jsonify({"error": "シフトデータの保存中にエラーが発生しました。"}), 500

        return jsonify(
            {
                "generation_id": generation_id,
                "status": "success",
                "year": year,
                "month": month,
                "shift_count": len(saved_shifts),
                "warning_count": len(saved_warnings),
            }
        )

    # -----------------------------------------------------------------
    # API ルート — シフト参照
    # -----------------------------------------------------------------
    @app.route("/api/shifts/<int:year>/<int:month>", methods=["GET"])
    def api_shifts_get(year, month):
        """指定月のシフトデータを JSON で返す"""
        if month < 1 or month > 12:
            return jsonify({"error": "month は 1〜12 で指定してください"}), 400
        if year < 2000 or year > 2100:
            return jsonify({"error": "year は 2000〜2100 の範囲で指定してください"}), 400

        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])

        shifts = (
            GeneratedShift.query.filter(
                GeneratedShift.date >= first_day,
                GeneratedShift.date <= last_day,
            )
            .order_by(GeneratedShift.date, GeneratedShift.staff_id)
            .all()
        )

        warnings = (
            ShiftWarning.query.filter(
                ShiftWarning.date >= first_day,
                ShiftWarning.date <= last_day,
            )
            .order_by(ShiftWarning.date)
            .all()
        )

        generation_id = shifts[0].generation_id if shifts else None

        # 職員一覧（department情報付き）。休職中はカレンダー表示・印刷の対象外。
        all_staff = _active_staff_for_month(year, month)

        # ⑧ 祝日リスト
        holidays = {}
        num_days = calendar.monthrange(year, month)[1]
        for d in range(1, num_days + 1):
            dt = date(year, month, d)
            hname = jpholiday.is_holiday_name(dt)
            if hname:
                holidays[dt.isoformat()] = hname

        # ④ 資格データ
        _staff_qual_ids, staff_qual_names, staff_qual_codes = _build_staff_qualification_maps()

        # オンコール（電話当番）: 日付→氏名
        oncall_rows = OncallAssignment.query.filter(
            OncallAssignment.date >= first_day,
            OncallAssignment.date <= last_day,
        ).all()
        oncall_map = {r.date.isoformat(): (r.staff.name if r.staff else "") for r in oncall_rows}

        # 駐車場（依頼文24）: 日付→{staff_id: ラベル("4"/"7"/"8"/"コイン")}
        parking_rows = ParkingAssignment.query.filter(
            ParkingAssignment.date >= first_day,
            ParkingAssignment.date <= last_day,
        ).all()
        parking_map = {}
        for r in parking_rows:
            parking_map.setdefault(r.date.isoformat(), {})[str(r.staff_id)] = r.label

        # 休み希望（この月）: 日付→[staff_id, ...]（シフト表に「希望休」と表示する）
        dayoff_map = {}
        for r in DayOffRequest.query.filter(
            DayOffRequest.date >= first_day, DayOffRequest.date <= last_day
        ).all():
            dayoff_map.setdefault(r.date.isoformat(), []).append(r.staff_id)

        # 確定情報（この月）
        conf = ShiftConfirmation.query.filter_by(year=year, month=month).first()

        # 固定職員（依頼文28）: この月に固定されている staff_id のリスト
        fixed_staff_ids = [
            f.staff_id for f in ShiftFix.query.filter_by(year=year, month=month).all()
        ]

        # カレンダー日付ヘッダの「デイ／訪問」表示用（0=月〜6=日）。設定画面の値を正とする。
        _st = ShiftSettings.query.first()

        def _dow_list(raw, fallback):
            # 未設定(None)なら既定値。空文字は「その曜日は無し」という明示的な指定として尊重する。
            if raw is None:
                return fallback
            vals = [x.strip() for x in raw.split(",") if x.strip().isdigit()]
            return sorted({int(v) for v in vals if 0 <= int(v) <= 6})

        operating_days = {
            # 階別（日付ヘッダの「デイ3階」「訪2階」などの表示に使う）
            "floor3_day_service": _dow_list(
                getattr(_st, "floor3_day_service_days", None), [1, 4, 6]
            ),
            "floor3_visit": _dow_list(getattr(_st, "floor3_visit_days", None), [0, 3]),
            "floor2_day_service": _dow_list(
                getattr(_st, "floor2_day_service_days", None), [0, 3, 5]
            ),
            "floor2_visit": _dow_list(getattr(_st, "floor2_visit_days", None), [1, 4]),
            "external_day_service": _dow_list(
                getattr(_st, "external_day_service_days", None), [2]
            ),
        }

        return jsonify(
            {
                "year": year,
                "month": month,
                "generation_id": generation_id,
                "shifts": [s.to_dict() for s in shifts],
                "warnings": [w.to_dict() for w in warnings],
                "holidays": holidays,
                "oncall": oncall_map,
                "day_off_requests": dayoff_map,
                "parking": parking_map,
                "confirmation": conf.to_dict() if conf else None,
                "fixed_staff_ids": fixed_staff_ids,
                "operating_days": operating_days,
                "staff_list": [
                    {
                        "id": st.id,
                        "name": st.name,
                        "department": st.staff_group,
                        "qualifications": staff_qual_names.get(st.id, []),
                        "qualification_codes": staff_qual_codes.get(st.id, []),
                        "job_category": getattr(st, "job_category", "caregiver") or "caregiver",
                        "car_commute": st.car_commute or False,
                        # 訪問に出られる職員か（手直し画面で訪問NGの人を弾くため）
                        "can_visit": bool(st.can_visit),
                        # 画面で直接編集したときの公休チェック用
                        "public_holiday_target": _public_holiday_target(st, year, month),
                    }
                    for st in all_staff
                ],
                # 画面編集用の凡例（ここからドラッグしてシフトを追加する）
                "palette": {
                    # 早番と訪問（午前）を分けて動かせるよう、訪問の枠は分かりやすい名前にする
                    "care": [
                        {"code": code, "label": label}
                        for code, label in (
                            ("day_pattern1", ASSIGNMENT_LABELS["day_pattern1"]),
                            ("day_pattern2", ASSIGNMENT_LABELS["day_pattern2"]),
                            ("day_pattern3", ASSIGNMENT_LABELS["day_pattern3"]),
                            ("day_pattern4", ASSIGNMENT_LABELS["day_pattern4"]),
                            ("early", ASSIGNMENT_LABELS["early"]),
                            ("late", ASSIGNMENT_LABELS["late"]),
                            ("nurse_short", ASSIGNMENT_LABELS["nurse_short"]),
                            ("visit_am", "訪問(午前)のみ"),
                            ("visit_pm", "訪問(午後)のみ"),
                            ("visit_am_day_p4", "訪問(午前)＋デイ(午後)"),
                            ("day_p3_visit_pm", "デイ(午前)＋訪問(午後)"),
                        )
                    ],
                    "cooking": [
                        {"code": p.code, "label": p.label}
                        for p in ShiftPattern.query.filter_by(staff_group="cooking")
                        .order_by(ShiftPattern.display_order).all()
                    ],
                },
                # 調理シフト種類マスタのラベル（画面表示で新種類を正しく表示する用）
                "cook_labels": {
                    p.code: p.label
                    for p in ShiftPattern.query.filter_by(staff_group="cooking").all()
                },
            }
        )

    @app.route("/api/shift-fix/<int:year>/<int:month>", methods=["GET"])
    def api_shift_fix_get(year, month):
        """その月に固定されている職員ID一覧を返す（依頼文28）。"""
        if month < 1 or month > 12 or year < 2000 or year > 2100:
            return jsonify({"error": "year/month が不正です"}), 400
        ids = [
            f.staff_id for f in ShiftFix.query.filter_by(year=year, month=month).all()
        ]
        return jsonify({"year": year, "month": month, "fixed_staff_ids": ids})

    @app.route("/api/shift-fix", methods=["POST"])
    def api_shift_fix_set():
        """職員のシフト固定 ON/OFF を切り替える（依頼文28）。
        body: {staff_id, year, month, fixed: true/false}
        fixed=true でエントリ追加（固定）、false で削除（解除）。
        """
        data = request.get_json(silent=True) or {}
        try:
            staff_id = int(data["staff_id"])
            year = int(data["year"])
            month = int(data["month"])
            fixed = bool(data["fixed"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "staff_id / year / month / fixed は必須です"}), 400

        if month < 1 or month > 12 or year < 2000 or year > 2100:
            return jsonify({"error": "year/month が不正です"}), 400

        if Staff.query.get(staff_id) is None:
            return jsonify({"error": "職員が見つかりません"}), 404

        existing = ShiftFix.query.filter_by(
            staff_id=staff_id, year=year, month=month
        ).first()

        if fixed:
            if existing is None:
                db.session.add(ShiftFix(staff_id=staff_id, year=year, month=month))
        else:
            if existing is not None:
                db.session.delete(existing)
        db.session.commit()

        return jsonify({"status": "success", "staff_id": staff_id,
                        "year": year, "month": month, "fixed": fixed})

    @app.route("/api/shifts/available", methods=["GET"])
    def api_shifts_available():
        """シフトがある年月の一覧と、最初に開くべき年月を返す。

        ユーザー指摘（2026-08）:「閲覧アプリが、いつのメンバーか分からない古い月を
        読み込んでいる」。閲覧ページが今日の月を決め打ちしていたため、まだ作成して
        いない月や古い月を開いていた。実際にシフトがある月から選ぶようにする。
        """
        rows = db.session.query(GeneratedShift.date).distinct().all()
        months = sorted({(r[0].year, r[0].month) for r in rows})
        confirmed = {
            (c.year, c.month) for c in ShiftConfirmation.query.all()
        }
        # 一番新しく作った月を開く。今日の月に古いシフトが残っていても、
        #   最新の（＝いま運用している）シフトが最初に出るようにする。
        default = months[-1] if months else None
        return jsonify({
            "months": [
                {"year": y, "month": m, "confirmed": (y, m) in confirmed}
                for (y, m) in months
            ],
            "default": ({"year": default[0], "month": default[1]} if default else None),
        })

    @app.route("/api/shift/cells", methods=["POST"])
    def api_shift_cells_update():
        """画面上で直接編集したシフトを保存する（1セル単位の一括反映）。

        ユーザー依頼（2026-08）:「この画面で直接1つ1つシフト変更できるようにする」。
        changes: [{"date": "YYYY-MM-DD", "staff_id": 1, "assignment": "early"}, ...]
                 assignment が "off"/"" のセルは休み（行を削除）。
        """
        data = request.get_json(silent=True) or {}
        year = safe_int(data.get("year"), 0)
        month = safe_int(data.get("month"), 0)
        changes = data.get("changes") or []
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            return jsonify({"error": "年月が正しくありません"}), 400
        if not isinstance(changes, list):
            return jsonify({"error": "changes の形式が正しくありません"}), 400

        _all_staff = Staff.query.all()
        exec_staff_ids = {st.id for st in _all_staff if _is_plan_only_staff(st)}
        _my_cats = _PLAN_EDIT_ROLES.get(session.get("role"))
        if _my_cats:
            # 役員／事務アカウントは自分の区分の予定だけ。ほかの職員のシフトは触れない
            allowed_ids = {
                st.id for st in _all_staff
                if (getattr(st, "job_category", "") or "") in _my_cats
            }
            for ch in changes:
                if safe_int(ch.get("staff_id"), None) not in allowed_ids:
                    return jsonify({
                        "error": "このアカウントで変えられるのは自分の予定だけです",
                    }), 403
                _c = (ch.get("assignment") or "").strip()
                if _c not in ("", EXEC_OFF_CODE) and not _c.startswith(EXEC_PLAN_PREFIX):
                    return jsonify({
                        "error": "このアカウントで入れられるのは予定だけです",
                    }), 403

        # 役員の予定だけの変更は、確定済みの月でも入れられる（勤務表は変わらないため）
        _only_exec_changes = all(
            safe_int(ch.get("staff_id"), None) in exec_staff_ids for ch in changes
        ) if changes else False
        if (not _only_exec_changes
                and ShiftConfirmation.query.filter_by(year=year, month=month).first()):
            return jsonify({
                "error": "この月は確定済みのため変更できません。先に「確定解除」してください。",
                "confirmed": True,
            }), 409

        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        existing = GeneratedShift.query.filter(
            GeneratedShift.date >= first_day, GeneratedShift.date <= last_day
        ).all()
        if not existing:
            return jsonify({"error": "この月のシフトがありません。先に生成してください。"}), 400
        generation_id = existing[0].generation_id
        by_key = {(r.staff_id, r.date): r for r in existing}

        valid_codes = set(CARE_ASSIGNMENTS) | set(COOK_ASSIGNMENTS) | {
            p.code for p in ShiftPattern.query.filter_by(staff_group="cooking").all()
        } | {EXEC_OFF_CODE}
        staff_by_id = {s.id: s for s in Staff.query.all()}

        applied = 0
        for ch in changes:
            try:
                d = datetime.strptime(str(ch.get("date")), "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if not (first_day <= d <= last_day):
                continue
            sid = safe_int(ch.get("staff_id"), None)
            st = staff_by_id.get(sid)
            if st is None:
                continue
            code = (ch.get("assignment") or "").strip()
            row = by_key.get((sid, d))

            # 訪問へ出る時間帯だけの変更（シフト本体は変えない）
            #   ※ assignment が入っている変更は本体の変更として扱う。
            #     画面からは assignment="" (=休み) と visit_slot を一緒に送るため、
            #     ここで取り違えると「休みにしたのに元に戻る」ことになる（2026-08 の不具合）。
            if "visit_slot" in ch and "assignment" not in ch:
                slot = ch.get("visit_slot")
                # "none" = この日は訪問に行かない（早番の既定の訪問を外す）
                slot = slot if slot in ("am", "pm", "none") else None
                if row is None:
                    if slot in (None, "none"):
                        continue
                    # 休みの人に訪問だけを割り当てる場合は訪問のシフトを作る
                    row = GeneratedShift(
                        generation_id=generation_id, date=d, staff_id=sid,
                        assignment=("visit_am" if slot == "am" else "visit_pm"),
                        visit_slot=slot,
                    )
                    db.session.add(row)
                    by_key[(sid, d)] = row
                else:
                    row.visit_slot = slot
                applied += 1
                continue

            if code in ("", "off", "cook_off"):
                if row is not None:
                    db.session.delete(row)
                    by_key.pop((sid, d), None)
                    applied += 1
                continue
            is_exec_code = (code == EXEC_OFF_CODE or code.startswith(EXEC_PLAN_PREFIX))
            if code not in valid_codes and not is_exec_code:
                continue
            is_exec = _is_plan_only_staff(st)
            if is_exec != is_exec_code:
                # 役員は役員の予定だけ、役員以外に役員の予定は入れられない
                continue
            if is_exec_code:
                # 手入力なので長さと改行を整える
                code = " ".join(code.split()).strip()[:30]
            # 区分違いの割り当て（調理職員に介護シフト等）は受け付けない
            is_cook_code = code.startswith("cooking_") or code in COOK_ASSIGNMENTS
            if not is_exec and is_cook_code != (st.staff_group == "cooking"):
                continue
            slot = ch.get("visit_slot")
            slot = slot if slot in ("am", "pm", "none") else None
            if row is None:
                row = GeneratedShift(
                    generation_id=generation_id, date=d, staff_id=sid, assignment=code,
                    visit_slot=slot,
                )
                db.session.add(row)
                by_key[(sid, d)] = row
            else:
                if row.assignment == code and row.visit_slot == slot:
                    continue
                row.assignment = code
                row.visit_slot = slot
                # 手動変更時は自動で付いた役割・休憩をいったん外す（実態と食い違わせない）
                row.bath_role = None
                row.break_start = None
                row.counselor_desk_slots = None
                row.meal_assist = None
            applied += 1

        db.session.flush()

        # 変更後の内容で警告を再計算（人数不足・公休など）
        rows = GeneratedShift.query.filter(
            GeneratedShift.date >= first_day, GeneratedShift.date <= last_day
        ).all()
        shifts_data = [r.to_dict() for r in rows]
        new_warnings = recompute_warnings_from_shifts(
            shifts_data, _staff_list_for_validation(year, month),
            _settings_for_validation(), year, month,
        )
        ShiftWarning.query.filter_by(generation_id=generation_id).delete()
        for w in new_warnings:
            db.session.add(ShiftWarning(
                generation_id=generation_id, date=date.fromisoformat(w["date"]),
                warning_type=w["warning_type"], message=w["message"],
            ))
        db.session.commit()
        return jsonify({
            "applied": applied,
            "warning_count": len(new_warnings),
            "warnings": new_warnings,
        })

    @app.route("/api/oncall", methods=["POST"])
    def api_oncall_set():
        """オンコール担当を手で入れ替える（date, staff_id）。staff_id 未指定で解除。"""
        data = request.get_json(silent=True) or {}
        try:
            d = datetime.strptime(str(data.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({"error": "日付が正しくありません"}), 400
        if ShiftConfirmation.query.filter_by(year=d.year, month=d.month).first():
            return jsonify({"error": "この月は確定済みのため変更できません。", "confirmed": True}), 409

        first_day = date(d.year, d.month, 1)
        last_day = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        gen_row = GeneratedShift.query.filter(
            GeneratedShift.date >= first_day, GeneratedShift.date <= last_day
        ).first()
        if gen_row is None:
            return jsonify({"error": "この月のシフトがありません。"}), 400
        generation_id = gen_row.generation_id

        sid = safe_int(data.get("staff_id"), None)
        existing = OncallAssignment.query.filter_by(
            generation_id=generation_id, date=d
        ).first()
        if sid is None:
            if existing is not None:
                db.session.delete(existing)
            db.session.commit()
            return jsonify({"date": d.isoformat(), "staff_id": None, "name": ""})
        st = Staff.query.get(sid)
        if st is None:
            return jsonify({"error": "職員が見つかりません"}), 400
        if existing is None:
            db.session.add(OncallAssignment(
                generation_id=generation_id, date=d, staff_id=sid,
            ))
        else:
            existing.staff_id = sid
        db.session.commit()
        return jsonify({"date": d.isoformat(), "staff_id": sid, "name": st.name})

    @app.route("/api/shifts/<int:year>/<int:month>/confirm", methods=["POST"])
    def api_shift_confirm(year, month):
        """その月のシフトを「確定」する（確定者・確定日時JSTを記録、月ごとに上書き）。"""
        if month < 1 or month > 12:
            return jsonify({"error": "month は 1〜12 で指定してください"}), 400
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        has_shifts = GeneratedShift.query.filter(
            GeneratedShift.date >= first_day, GeneratedShift.date <= last_day
        ).first() is not None
        if not has_shifts:
            return jsonify({"error": "この月のシフトがありません。先に生成してください。"}), 400

        conf = ShiftConfirmation.query.filter_by(year=year, month=month).first()
        if conf is None:
            conf = ShiftConfirmation(year=year, month=month)
            db.session.add(conf)
        conf.confirmed_by = session.get("user", "")
        conf.confirmed_role = session.get("role", "")
        conf.confirmed_at = _now_jst()
        db.session.commit()
        return jsonify(conf.to_dict())

    @app.route("/api/shifts/<int:year>/<int:month>/confirm", methods=["DELETE"])
    def api_shift_unconfirm(year, month):
        """その月のシフト確定を解除する（再生成できるようにする）。"""
        conf = ShiftConfirmation.query.filter_by(year=year, month=month).first()
        if conf:
            db.session.delete(conf)
            db.session.commit()
        return jsonify({"message": "確定を解除しました", "confirmation": None})

    # -----------------------------------------------------------------
    # API ルート — エクスポート
    # -----------------------------------------------------------------
    def _build_export_payload(generation_id):
        """Excel/CSV/PDF 共通: 生成IDからエクスポート用データ一式を組み立てる。
        戻り値: (shifts_data, warnings_data, staff_list_data, year, month, oncall_map)
        該当シフトが無ければ None。
        """
        shifts = GeneratedShift.query.filter_by(generation_id=generation_id).all()
        if not shifts:
            return None

        # 曜日行の「デイ／訪問」注記を設定画面の営業曜日に合わせる（画面カレンダーと同じ）
        _st = ShiftSettings.query.first()

        def _dow_list(raw):
            # None（列なし／設定未作成）は既定値据え置き、空文字は「無し」の明示指定。
            if raw is None:
                return None
            return [
                int(x) for x in raw.split(",")
                if x.strip().isdigit() and 0 <= int(x) <= 6
            ]

        configure_operating_days(
            floor3_day_service_days=_dow_list(getattr(_st, "floor3_day_service_days", None)),
            floor3_visit_days=_dow_list(getattr(_st, "floor3_visit_days", None)),
            floor2_day_service_days=_dow_list(getattr(_st, "floor2_day_service_days", None)),
            floor2_visit_days=_dow_list(getattr(_st, "floor2_visit_days", None)),
            external_day_service_days=_dow_list(
                getattr(_st, "external_day_service_days", None)
            ),
        )

        warnings = ShiftWarning.query.filter_by(generation_id=generation_id).all()

        first_date = shifts[0].date
        year = first_date.year
        month = first_date.month

        # 休職中・（その月より前に）退職した職員は印刷一覧に載せない。
        staffs = _active_staff_for_month(year, month)
        _active_ids = {st.id for st in staffs}
        on_leave_ids = {
            st.id for st in Staff.query.all() if st.id not in _active_ids
        }

        # 休み希望（この月）: 出力の休みセルを「希望休」と表示するため登録する
        _first_day = date(year, month, 1)
        _last_day = date(year, month, calendar.monthrange(year, month)[1])
        _dayoff_map = {}
        for _r in DayOffRequest.query.filter(
            DayOffRequest.date >= _first_day, DayOffRequest.date <= _last_day
        ).all():
            _dayoff_map.setdefault(_r.date.isoformat(), []).append(_r.staff_id)
        register_day_off_requests(_dayoff_map)

        # 駐車場（依頼文24）: (date, staff_id) → ラベル。各シフト項目に付与して
        #   Excel/PDF のセルに表示できるようにする（生成ロジックには非干渉）。
        parking_rows = ParkingAssignment.query.filter_by(generation_id=generation_id).all()
        parking_lookup = {(r.date.isoformat(), r.staff_id): r.label for r in parking_rows}

        shifts_data = []
        for s in shifts:
            # 休職中の職員のシフト行は出力に含めない（念のための防御。通常は生成時点で除外済み）
            if s.staff_id in on_leave_ids:
                continue
            d = {
                "date": s.date.isoformat(),
                "staff_id": s.staff_id,
                "staff_name": s.staff.name if s.staff else "",
                "assignment": s.assignment,
                "is_phone_duty": s.is_phone_duty,
                "break_start": s.break_start,
                "bath_role": s.bath_role,
                "visit_slot": s.visit_slot,
                "meal_assist": s.meal_assist,
                "parking_label": parking_lookup.get((s.date.isoformat(), s.staff_id)),
            }
            # ③ 相談員事務スロット
            if s.counselor_desk_slots:
                try:
                    d["counselor_desk_slots"] = json.loads(s.counselor_desk_slots)
                except (ValueError, TypeError):
                    pass
            shifts_data.append(d)

        warnings_data = [
            {
                "date": w.date.isoformat(),
                "warning_type": w.warning_type or "",
                "message": w.message or "",
            }
            for w in warnings
        ]
        # ④ 資格データ
        _staff_qual_ids, staff_qual_names, staff_qual_codes = _build_staff_qualification_maps()

        staff_list_data = [
            {
                "id": st.id,
                "name": st.name,
                "department": st.staff_group,
                "qualifications": staff_qual_names.get(st.id, []),
                "qualification_codes": staff_qual_codes.get(st.id, []),
                "job_category": getattr(st, "job_category", "caregiver") or "caregiver",
            }
            for st in staffs
        ]

        oncall_rows = OncallAssignment.query.filter_by(generation_id=generation_id).all()
        oncall_map = {r.date.isoformat(): (r.staff.name if r.staff else "") for r in oncall_rows}

        # 調理シフト種類マスタのラベル（新種類の Excel/CSV/PDF 表示用）
        cook_labels = {
            p.code: p.label
            for p in ShiftPattern.query.filter_by(staff_group="cooking").all()
        }

        return shifts_data, warnings_data, staff_list_data, year, month, oncall_map, cook_labels

    def _jp_export_filename(year, month, group, half, ext):
        """依頼文26: 分かりやすい日本語のダウンロード名を生成する。
        例: シフト表_2026年7月_介護看護_前半.pdf
        非ASCII名はsend_file(Werkzeug)がfilename*=UTF-8''で自動エンコードする。
        """
        group_jp = "調理" if group == "cooking" else "介護看護"
        half_jp = "後半" if half == "second" else "前半"
        return f"シフト表_{year}年{month}月_{group_jp}_{half_jp}.{ext}"

    @app.route("/api/export/<generation_id>/excel", methods=["GET"])
    def api_export_excel(generation_id):
        """Excel ダウンロード。
        group/half クエリ指定時は PDFと同じ4分割（1シート）、無指定なら従来の全月2シート。
        """
        payload = _build_export_payload(generation_id)
        if payload is None:
            return jsonify({"error": "該当するシフトデータがありません"}), 404
        shifts_data, warnings_data, staff_list_data, year, month, oncall_map, cook_labels = payload

        group = request.args.get("group")
        half = request.args.get("half")
        role = session.get("role", "")
        if group in ("care", "cooking") and half in ("first", "second"):
            # 依頼文23-A: 4分割Excel（介護看護/調理 × 前半/後半）
            buf = export_excel_group_half(
                shifts_data, warnings_data, staff_list_data, year, month,
                group=group, half=half, oncall_map=oncall_map, cook_labels=cook_labels,
            )
            filename = _jp_export_filename(year, month, group, half, "xlsx")
        else:
            buf = export_excel(
                shifts_data, warnings_data, staff_list_data, year, month,
                oncall_map=oncall_map, cook_labels=cook_labels,
            )
            filename = f"シフト{role}{_now_jst().strftime('%y%m%d')}.xlsx"

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/export/<generation_id>/csv", methods=["GET"])
    def api_export_csv(generation_id):
        """CSV ファイルとしてダウンロード"""
        payload = _build_export_payload(generation_id)
        if payload is None:
            return jsonify({"error": "該当するシフトデータがありません"}), 404
        shifts_data, warnings_data, staff_list_data, year, month, oncall_map, cook_labels = payload

        csv_string = export_csv(shifts_data, warnings_data, staff_list_data, year, month, oncall_map=oncall_map, cook_labels=cook_labels)
        role = session.get("role", "")
        filename = f"シフト{role}{_now_jst().strftime('%y%m%d')}.csv"

        buf = BytesIO(csv_string.encode("utf-8"))
        buf.seek(0)

        return send_file(
            buf,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/export/<generation_id>/pdf", methods=["GET"])
    def api_export_pdf(generation_id):
        """PDF ファイルとしてダウンロード（4種: 介護看護/調理 × 前半/後半）。

        クエリ: ?group=care|cooking & half=first|second
        """
        group = request.args.get("group", "care")
        if group not in ("care", "cooking"):
            group = "care"
        half = request.args.get("half", "first")
        if half not in ("first", "second"):
            half = "first"

        payload = _build_export_payload(generation_id)
        if payload is None:
            return jsonify({"error": "該当するシフトデータがありません"}), 404
        shifts_data, warnings_data, staff_list_data, year, month, oncall_map, cook_labels = payload

        buf = export_pdf(
            shifts_data, warnings_data, staff_list_data, year, month,
            group=group, half=half, oncall_map=oncall_map, cook_labels=cook_labels,
        )
        # ファイル名（依頼文26）: シフト表_{年}年{月}月_{介護看護|調理}_{前半|後半}.pdf
        filename = _jp_export_filename(year, month, group, half, "pdf")

        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/export/<generation_id>/pdf-individual", methods=["GET"])
    def api_export_pdf_individual(generation_id):
        """職員1人ずつの月間カレンダーPDF（縦A4）。

        クエリ: ?staff_id=N を付けるとその職員1名のPDF。
        無指定なら在籍職員全員を「1人1ファイルのPDF」にしてZIPでまとめて返す。
        休職者は _build_export_payload の時点で除外されるため出力に含まれない。
        """
        payload = _build_export_payload(generation_id)
        if payload is None:
            return jsonify({"error": "該当するシフトデータがありません"}), 404
        shifts_data, warnings_data, staff_list_data, year, month, oncall_map, cook_labels = payload

        if not staff_list_data:
            return jsonify({"error": "対象の職員がいません"}), 404

        def _safe_name(nm):
            return re.sub(r'[\\/:*?"<>|]', "_", nm).replace(" ", "")

        staff_id_arg = request.args.get("staff_id")
        if staff_id_arg:
            # --- 個別1名: 単一PDF ---
            try:
                sid = int(staff_id_arg)
            except (TypeError, ValueError):
                return jsonify({"error": "staff_id が不正です"}), 400
            match = next((s for s in staff_list_data if s["id"] == sid), None)
            if match is None:
                return jsonify({"error": "対象の職員が見つかりません（休職中または存在しません）"}), 404
            buf = export_pdf_individual(
                shifts_data, staff_list_data, year, month,
                staff_ids=[sid], oncall_map=oncall_map, cook_labels=cook_labels,
            )
            filename = f"シフト表_{year}年{month:02d}月_{_safe_name(match['name'])}.pdf"
            return send_file(
                buf, mimetype="application/pdf",
                as_attachment=True, download_name=filename,
            )

        # --- 全員: 1人1ファイルのPDFを作りZIPでまとめる ---
        zip_buf = BytesIO()
        used_names = set()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for s in staff_list_data:
                pdf_buf = export_pdf_individual(
                    shifts_data, staff_list_data, year, month,
                    staff_ids=[s["id"]], oncall_map=oncall_map, cook_labels=cook_labels,
                )
                base = f"シフト表_{year}年{month:02d}月_{_safe_name(s['name'])}"
                entry = f"{base}.pdf"
                idx = 2
                while entry in used_names:  # 同名職員がいても衝突しないよう連番
                    entry = f"{base}_{idx}.pdf"
                    idx += 1
                used_names.add(entry)
                zf.writestr(entry, pdf_buf.getvalue())
        zip_buf.seek(0)
        return send_file(
            zip_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"シフト表_個別_{year}年{month:02d}月.zip",
        )

    @app.route("/api/export/excel-to-pdf", methods=["POST"])
    def api_export_excel_to_pdf():
        """依頼文23-B: アップロードされた（手直し済み）Excelの内容から該当PDFを作る。
        アプリの保存データは一切使わない・変更しない。フォーム: file, group, half。
        """
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "Excelファイルを選択してください"}), 400
        group = request.form.get("group", "care")
        if group not in ("care", "cooking"):
            group = "care"
        half = request.form.get("half", "first")
        if half not in ("first", "second"):
            half = "first"
        try:
            file_bytes = f.read()
            buf, year, month = export_pdf_from_excel(file_bytes, group=group, half=half)
        except Exception as e:  # 形式不一致など
            return jsonify({"error": f"Excelの読み取りに失敗しました: {e}"}), 400

        # 依頼文26: アップロード由来でも同じ日本語命名（対象月はExcelから取得）
        filename = _jp_export_filename(year, month, group, half, "pdf")
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    # -----------------------------------------------------------------
    # 依頼文41: 手修正Excel → 保存シフトへの反映（確認→範囲限定上書き・バックアップ）
    #   依頼文23（アップロード→PDF・データ不変）は別ルートで温存。こちらは「反映する」専用。
    # -----------------------------------------------------------------
    def _db_file_path():
        return app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")

    def _upload_tmp_dir():
        d = os.path.join(os.path.dirname(_db_file_path()), "shift_upload_tmp")
        os.makedirs(d, exist_ok=True)
        return d

    def _group_staff_rows(group, year=None, month=None):
        # 休職中は出力（Excel/PDF/CSV）に出さないため、反映の「期待職員セット」からも除外する。
        # これを含めると休職者が出力に無いことを「ファイルに不足」と誤検知し構造不一致になる（依頼文44-c）。
        # 退職者も同様（退職月までは在籍扱いなので、対象月で判定する）。
        if year and month:
            staffs = _active_staff_for_month(year, month)
        else:
            staffs = Staff.query.filter_by(on_leave=False, retired=False).order_by(Staff.id).all()
        if group == "cooking":
            return [s for s in staffs if s.staff_group == "cooking"]
        return [s for s in staffs if s.staff_group != "cooking"]

    def _validate_upload_structure(parsed, group):
        """ファイルの職員行が DB の当該グループ職員と一致するか検査（構造崩れ＝反映拒否）。"""
        db_staff = _group_staff_rows(group, parsed.get("year"), parsed.get("month"))
        db_names = [s.name for s in db_staff]
        file_names = parsed["staff_names"]
        seen = {}
        for n in file_names:
            seen[n] = seen.get(n, 0) + 1
        errors = []
        dups = sorted([n for n, c in seen.items() if c > 1])
        unknown = [n for n in file_names if n not in set(db_names)]
        missing = [n for n in db_names if n not in set(file_names)]
        if dups:
            errors.append("職員名の重複: " + "、".join(dups))
        if unknown:
            errors.append("アプリに存在しない職員名（氏名改変の可能性）: " + "、".join(unknown))
        if missing:
            errors.append("ファイルに不足している職員（行削除の可能性）: " + "、".join(missing))
        return errors, {s.name: s.id for s in db_staff}

    def _norm_slots(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        if not value:
            return None
        return sorted(set(int(x) for x in value))

    def _norm_state(st):
        return (
            (st.get("assignment") or "off"),
            (st.get("bath_role") or None),
            tuple(_norm_slots(st.get("desk_slots")) or ()),
        )

    def _derive_break(stt):
        """役割から休憩開始を再導出（依頼文41・変更セルのみ。非表示項目）。"""
        if stt.get("bath_role") == "中":
            return "11:30"
        if stt.get("assignment") == "nurse_short":
            return "13:00"
        if stt.get("assignment") in ("visit_am", "visit_pm"):
            return None
        if stt.get("assignment", "").startswith("cooking_"):
            return None
        return "12:30"

    def _derive_meal(stt):
        if stt.get("bath_role") == "中":
            return "12:30-13:00"
        return None

    def _backup_db_file(year, month):
        src = _db_file_path()
        bdir = os.path.join(os.path.dirname(src), "shift_backups")
        os.makedirs(bdir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(bdir, f"shift_backup_{year}{month:02d}_{ts}.db")
        shutil.copy2(src, dst)
        return dst

    def _settings_for_validation():
        so = ShiftSettings.query.first()
        closed = [int(x) for x in (so.closed_days or "").split(",") if x.strip()] if so and so.closed_days else []
        vdays = [int(x) for x in (so.visit_operating_days or "").split(",") if x.strip()] if so and so.visit_operating_days else []
        cdates = [x.strip() for x in (getattr(so, "closed_dates", "") or "").split(",") if x.strip()]
        placement = [r.to_dict() for r in PlacementRule.query.filter_by(is_active=True).all()]
        nods = [
            int(x) for x in (getattr(so, "no_day_service_days", "") or "").split(",")
            if x.strip()
        ]
        return {
            "closed_days": closed, "closed_dates": cdates, "visit_operating_days": vdays,
            "no_day_service_days": nods,
            "min_day_service": getattr(so, "min_day_service", 0) or 0,
            "min_visit_am": getattr(so, "min_visit_am", 0) or 0,
            "min_visit_pm": getattr(so, "min_visit_pm", 0) or 0,
            "min_bath_mid": getattr(so, "min_bath_mid", 0) or 0,
            "min_bath_out": getattr(so, "min_bath_out", 0) or 0,
            "min_early_staff": getattr(so, "min_early_staff", 1) or 0,
            "min_late_staff": getattr(so, "min_late_staff", 1) or 0,
            # 曜日ごとの介護配置人数（警告の判定にも使う。2026-08 の依頼）
            "care_min_by_weekday": _parse_wd_counts(
                getattr(so, "care_min_by_weekday", "")),
            "care_max_by_weekday": _parse_wd_counts(
                getattr(so, "care_max_by_weekday", "")),
            "placement_rules": placement,
        }

    def _staff_list_for_validation(year=None, month=None):
        qm, qn, qc = _build_staff_qualification_maps()
        rows = (
            _active_staff_for_month(year, month) if (year and month)
            else Staff.query.filter_by(on_leave=False, retired=False).all()
        )
        return [
            {"id": s.id, "name": s.name,
             "qualification_codes": qc.get(s.id, []),
             "qualifications": qn.get(s.id, []),
             "job_category": getattr(s, "job_category", "caregiver") or "caregiver",
             "public_holiday_target": _public_holiday_target(s, year, month)}
            for s in rows
        ]

    @app.route("/api/shift/upload-preview", methods=["POST"])
    def api_shift_upload_preview():
        """依頼文41-A: 手修正Excelを読み、現在の保存シフトとの差分・解釈不能セルを返す（DB不変）。"""
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "Excelファイルを選択してください"}), 400
        group = request.form.get("group", "care")
        if group not in ("care", "cooking"):
            group = "care"
        half = request.form.get("half", "first")
        if half not in ("first", "second"):
            half = "first"
        file_bytes = f.read()
        try:
            parsed = parse_uploaded_shift_excel(file_bytes, group=group, half=half)
        except Exception as e:
            return jsonify({"error": f"Excelの読み取りに失敗しました: {e}"}), 400
        year, month = parsed["year"], parsed["month"]
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        if not GeneratedShift.query.filter(GeneratedShift.date >= first, GeneratedShift.date <= last).first():
            return jsonify({"error": f"{year}年{month}月の保存シフトがありません。先に生成してください。"}), 400
        errors, name_to_id = _validate_upload_structure(parsed, group)
        if errors:
            return jsonify({"error": "ファイル構造が一致しないため反映できません。", "errors": errors}), 400

        staff_ids = list(name_to_id.values())
        rows = GeneratedShift.query.filter(
            GeneratedShift.date >= first, GeneratedShift.date <= last,
            GeneratedShift.staff_id.in_(staff_ids),
        ).all()
        cur = {}
        for r in rows:
            cur[(r.staff_id, r.date.isoformat())] = {
                "assignment": r.assignment, "bath_role": r.bath_role,
                "desk_slots": _norm_slots(r.counselor_desk_slots),
            }
        iso_set = set(parsed["date_isos"])
        diffs = []
        for name, row_states in parsed["cells"].items():
            sid = name_to_id[name]
            for iso, st in row_states.items():
                if iso not in iso_set or st is None:
                    continue
                cur_state = cur.get((sid, iso), {"assignment": "off", "bath_role": None, "desk_slots": None})
                if _norm_state(cur_state) != _norm_state(st):
                    diffs.append({
                        "name": name, "date": iso,
                        "from": state_to_cell_text(cur_state["assignment"], cur_state["bath_role"], cur_state["desk_slots"]),
                        "to": state_to_cell_text(st["assignment"], st["bath_role"], st["desk_slots"]),
                    })
        token = str(uuid.uuid4())
        tmp = _upload_tmp_dir()
        with open(os.path.join(tmp, token + ".xlsx"), "wb") as fp:
            fp.write(file_bytes)
        with open(os.path.join(tmp, token + ".json"), "w", encoding="utf-8") as fp:
            json.dump({"group": group, "half": half, "year": year, "month": month}, fp)
        diffs.sort(key=lambda d: (d["date"], d["name"]))
        return jsonify({
            "token": token, "year": year, "month": month,
            "group_label": ("調理" if group == "cooking" else "介護看護"),
            "range_label": ("前半(1〜15日)" if half != "second" else "後半(16日〜月末)"),
            "diffs": diffs, "diff_count": len(diffs),
            "unparseable": parsed["unparseable"],
        })

    @app.route("/api/shift/upload-apply", methods=["POST"])
    def api_shift_upload_apply():
        """依頼文41-B/C/D: バックアップ→範囲限定で上書き→警告再計算。手修正優先（違反でも反映）。"""
        data = request.get_json(silent=True) or {}
        token = data.get("token") or request.form.get("token", "")
        if not re.match(r"^[0-9a-fA-F-]{36}$", str(token)):
            return jsonify({"error": "不正なトークンです。もう一度アップロードしてください。"}), 400
        tmp = _upload_tmp_dir()
        xlsx_path = os.path.join(tmp, token + ".xlsx")
        meta_path = os.path.join(tmp, token + ".json")
        if not (os.path.exists(xlsx_path) and os.path.exists(meta_path)):
            return jsonify({"error": "プレビュー情報が見つかりません。もう一度アップロードしてください。"}), 400
        with open(meta_path, encoding="utf-8") as fp:
            meta = json.load(fp)
        group, half = meta["group"], meta["half"]
        with open(xlsx_path, "rb") as fp:
            file_bytes = fp.read()
        try:
            parsed = parse_uploaded_shift_excel(file_bytes, group=group, half=half)
        except Exception as e:
            return jsonify({"error": f"Excelの読み取りに失敗しました: {e}"}), 400
        year, month = parsed["year"], parsed["month"]
        errors, name_to_id = _validate_upload_structure(parsed, group)
        if errors:
            return jsonify({"error": "ファイル構造が一致しないため反映できません。", "errors": errors}), 400
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        any_row = GeneratedShift.query.filter(GeneratedShift.date >= first, GeneratedShift.date <= last).first()
        if not any_row:
            return jsonify({"error": f"{year}年{month}月の保存シフトがありません。"}), 400
        gen_id = any_row.generation_id

        # --- C: 反映直前にDBファイルをバックアップ ---
        backup_path = _backup_db_file(year, month)

        # --- B: 範囲限定の書き戻し（当該グループ職員 × 当該半期の日付のみ）---
        staff_ids = list(name_to_id.values())
        rows = GeneratedShift.query.filter(
            GeneratedShift.date >= first, GeneratedShift.date <= last,
            GeneratedShift.staff_id.in_(staff_ids),
        ).all()
        row_map = {(r.staff_id, r.date.isoformat()): r for r in rows}
        iso_set = set(parsed["date_isos"])
        applied = 0
        skipped = 0
        for name, row_states in parsed["cells"].items():
            sid = name_to_id[name]
            for iso, stt in row_states.items():
                if iso not in iso_set:
                    continue
                if stt is None:
                    skipped += 1
                    continue  # 解釈不能セルは上書きしない（原値保持）
                d = date.fromisoformat(iso)
                existing = row_map.get((sid, iso))
                if stt["assignment"] == "off":
                    if existing is not None:
                        db.session.delete(existing)
                        applied += 1
                    continue
                desk_json = json.dumps(stt["desk_slots"]) if stt["desk_slots"] else None
                if existing is None:
                    db.session.add(GeneratedShift(
                        generation_id=gen_id, date=d, staff_id=sid,
                        assignment=stt["assignment"], shift_pattern_code=None,
                        is_phone_duty=False, break_start=_derive_break(stt),
                        counselor_desk_slots=desk_json, bath_role=stt["bath_role"],
                        meal_assist=_derive_meal(stt),
                    ))
                    applied += 1
                else:
                    changed = (
                        existing.assignment != stt["assignment"]
                        or (existing.bath_role or None) != (stt["bath_role"] or None)
                        or _norm_slots(existing.counselor_desk_slots) != (stt["desk_slots"] or None)
                    )
                    if changed:
                        existing.assignment = stt["assignment"]
                        existing.bath_role = stt["bath_role"]
                        existing.counselor_desk_slots = desk_json
                        existing.break_start = _derive_break(stt)
                        existing.meal_assist = _derive_meal(stt)
                        applied += 1

        db.session.flush()
        # --- D: 手修正後の全データで警告を再計算（違反でも拒否しない）---
        all_rows = GeneratedShift.query.filter(
            GeneratedShift.date >= first, GeneratedShift.date <= last
        ).all()
        shifts_data = [r.to_dict() for r in all_rows]
        new_warnings = recompute_warnings_from_shifts(
            shifts_data, _staff_list_for_validation(year, month),
            _settings_for_validation(), year, month
        )
        ShiftWarning.query.filter_by(generation_id=gen_id).delete()
        for w in new_warnings:
            db.session.add(ShiftWarning(
                generation_id=gen_id, date=date.fromisoformat(w["date"]),
                warning_type=w["warning_type"], message=w["message"],
            ))
        db.session.commit()
        try:
            os.remove(xlsx_path)
            os.remove(meta_path)
        except OSError:
            pass
        return jsonify({
            "applied": applied, "skipped_unparseable": skipped,
            "backup": backup_path, "warning_count": len(new_warnings),
            "warnings": new_warnings,
            "message": f"{applied}件のセルを反映しました。バックアップ: {backup_path}",
        })

    return app


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import webbrowser

    app = create_app()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5050))
    if not debug:
        threading.Timer(1.5, webbrowser.open, args=[f"http://localhost:{port}"]).start()
    app.run(debug=debug, host="0.0.0.0", port=port)
