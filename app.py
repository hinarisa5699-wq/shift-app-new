"""
app.py — Flask アプリケーション本体
介護シフト自動作成アプリ
"""

import csv
import io
import json
import logging
import os
import threading
import uuid
import sqlite3
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

from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

import jpholiday
from config import Config
from models import (
    db, Staff, DayOffRequest, ShiftSettings, GeneratedShift, ShiftWarning,
    ShiftPattern, Qualification, StaffQualification, PlacementRule, CookingComboRule,
    StaffAllowedPattern, StaffWorkableDate, OncallAssignment, ShiftConfirmation,
)
from solver import generate_shift, assign_oncall, CARE_ASSIGNMENTS, COOK_ASSIGNMENTS
from export import export_excel, export_csv


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
    "cooking_1": "cook_early",
    "cooking_2": "cook_morning",
    "cooking_3": "cook_late",
    "cooking_4": "cook_long",
    "cooking_5": "cook_mid",
}

_VALID_ALLOWED_BY_GROUP = {
    "care": set(CARE_ASSIGNMENTS) - {"off"},
    "cooking": set(COOK_ASSIGNMENTS) - {"cook_off"},
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
    "holiday_ng", "weekend_constraint",
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


# --- 区分(job_category) / 役割(role) の選択肢とラベル ---
JOB_CATEGORIES = [
    ("caregiver", "介護"),
    ("nurse_rehab", "看護"),
    ("cooking", "調理"),
]
JOB_CATEGORY_LABELS = dict(JOB_CATEGORIES)

ROLES = [
    ("", "なし"),
    ("manager", "管理者"),
    ("sekinin", "サ責"),
    ("sekinin_assist", "サ責補佐"),
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
    }
    return table.get(v, table.get(v.lower(), ""))


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


def normalize_allowed_pattern_codes(raw_codes, staff_group):
    """フォーム入力の allowed_patterns を solver が扱うコードへ正規化する。"""
    valid_codes = _VALID_ALLOWED_BY_GROUP.get(staff_group, set())
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

    # ShiftSettings テーブル
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(shift_settings)").fetchall()]
    if "visit_operating_days" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN visit_operating_days VARCHAR(50) DEFAULT '0,1,3,4'")
    if "min_cooking_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_cooking_staff INTEGER DEFAULT 1")
    if "min_early_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_early_staff INTEGER DEFAULT 1")
    if "min_late_staff" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_late_staff INTEGER DEFAULT 1")
    if "min_cooking_overlap" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN min_cooking_overlap INTEGER DEFAULT 2")
    if "am_preferred_gender" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN am_preferred_gender VARCHAR(10) DEFAULT ''")
    if "phone_duty_enabled" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN phone_duty_enabled BOOLEAN DEFAULT 0")
    if "phone_duty_max_consecutive" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN phone_duty_max_consecutive INTEGER DEFAULT 1")
    if "male_am_constraint_mode" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN male_am_constraint_mode VARCHAR(10) DEFAULT 'hard'")
    if "counselor_desk_enabled" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN counselor_desk_enabled BOOLEAN DEFAULT 0")
    if "counselor_desk_count" not in columns:
        cursor.execute("ALTER TABLE shift_settings ADD COLUMN counselor_desk_count INTEGER DEFAULT 1")

    # GeneratedShift テーブル
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(generated_shift)").fetchall()]
    if "shift_pattern_code" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN shift_pattern_code VARCHAR(30)")
    if "is_phone_duty" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN is_phone_duty BOOLEAN DEFAULT 0")
    if "counselor_desk_slots" not in columns:
        cursor.execute("ALTER TABLE generated_shift ADD COLUMN counselor_desk_slots TEXT")
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
    {
        "name": "看護師/PT 9-16時 1名以上",
        "rule_type": "qualification_min",
        "target_qualification_ids_json": "[]",
        "target_gender": "",
        "period": "all",
        "min_count": 1,
        "is_hard": False,
        "penalty_weight": 200,
        "_qual_codes": ["nurse", "pt"],
    },
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
    ["cook_early", "cook_morning", "cook_late", "cook_mid"],  # ①②③⑤
    ["cook_early", "cook_morning", "cook_late"],              # ①②③
    ["cook_long", "cook_late", "cook_mid"],                   # ④③⑤
    ["cook_long", "cook_late"],                               # ④③
]

_INITIAL_COOKING_COMBO = {
    "name": "調理の日単位組み合わせ",
    "allowed_patterns_json": json.dumps(_COOKING_COMBOS),
    "is_active": True,
}


# ---------------------------------------------------------------------------
# 調理シフトパターンの整合（既存DBにも変更1〜3を反映・冪等）
# ---------------------------------------------------------------------------
def _sync_cooking_patterns():
    """既存DBの調理パターン定義を最新仕様へ揃える（起動ごとに冪等実行）。
    調理5パターン: ①6-8 / ②8-13 / ③12-19 / ④6-13 / ⑤9-15。
    組み合わせは _COOKING_COMBOS の4通りに統一。
    ※ ShiftPattern/CookingComboRule のシードは既存行を更新しないため、ここで補正する。
    """
    changed = False

    # 調理パターンの正しい時間（コード→(label, start, end)）
    cook_defs = {
        "cooking_1": ("(1) 6:00-8:00", "06:00", "08:00", 5),
        "cooking_2": ("(2) 8:00-13:00", "08:00", "13:00", 6),
        "cooking_3": ("(3) 12:00-19:00", "12:00", "19:00", 7),
        "cooking_4": ("(4) 6:00-13:00", "06:00", "13:00", 8),
        "cooking_5": ("(5) 9:00-15:00", "09:00", "15:00", 9),
    }
    for code, (label, start, end, order) in cook_defs.items():
        p = ShiftPattern.query.filter_by(code=code).first()
        if p is None:
            db.session.add(ShiftPattern(
                code=code, staff_group="cooking", label=label,
                start_time=start, end_time=end, has_break=False, break_minutes=0,
                display_order=order, period="full", covers_am=True, covers_pm=True,
            ))
            changed = True
        elif p.start_time != start or p.end_time != end or p.label != label:
            p.label = label
            p.start_time = start
            p.end_time = end
            p.covers_am = True
            p.covers_pm = True
            changed = True

    # 組み合わせルールを4通り(_COOKING_COMBOS)に統一（旧①〜⑧/セットルールは取り消し）
    desired = json.dumps(_COOKING_COMBOS)
    rules = CookingComboRule.query.all()
    if not rules:
        db.session.add(CookingComboRule(**_INITIAL_COOKING_COMBO))
        changed = True
    else:
        for rule in rules:
            if rule.allowed_patterns_json != desired:
                rule.allowed_patterns_json = desired
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
    未設定の場合はローカル開発用に admin/saseki/yakuin を仮設定（要・本番では必ず環境変数を設定）。
    戻り値: {username: {"hash": ..., "role": 表示名}}
    """
    admin_pw = os.environ.get("SHIFT_ADMIN_PASSWORD", "").strip()
    saseki_pw = os.environ.get("SHIFT_SASEKI_PASSWORD", "").strip()
    yakuin_pw = os.environ.get("SHIFT_YAKUIN_PASSWORD", "").strip()
    dev_default = not admin_pw and not saseki_pw and not yakuin_pw
    if dev_default:
        # ローカル開発フォールバック（本番では環境変数を必ず設定すること）
        admin_pw = "admin"
        saseki_pw = "saseki"
        yakuin_pw = "yakuin"
    users = {}
    if admin_pw:
        users["admin"] = {"hash": generate_password_hash(admin_pw), "role": "管理者"}
    if saseki_pw:
        users["saseki"] = {"hash": generate_password_hash(saseki_pw), "role": "サ責"}
    if yakuin_pw:
        # 役員は管理者と同じ権限（ログイン済みなら全機能アクセス可）
        users["yakuin"] = {"hash": generate_password_hash(yakuin_pw), "role": "役員"}
    return users, dev_default


# ログイン不要でアクセスできるエンドポイント
_PUBLIC_ENDPOINTS = {"login", "static"}


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
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user"):
            return redirect(url_for("index"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = app.config["USERS"].get(username)
            if user and check_password_hash(user["hash"], password):
                session["user"] = username
                session["role"] = user["role"]
                nxt = request.args.get("next") or url_for("index")
                # オープンリダイレクト防止: 内部パスのみ許可
                if not nxt.startswith("/"):
                    nxt = url_for("index")
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

        # CookingComboRule 初期データ
        if CookingComboRule.query.count() == 0:
            db.session.add(CookingComboRule(**_INITIAL_COOKING_COMBO))
            db.session.commit()

        # 調理パターンの整合（変更1〜3を既存DBへも反映）
        _sync_cooking_patterns()

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
        """職員一覧"""
        staffs = Staff.query.order_by(Staff.id).all()
        return render_template("staff_list.html", staff_list=staffs)

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
        cooking_combo_rules = CookingComboRule.query.order_by(CookingComboRule.id).all()
        return render_template("settings.html", settings=s,
                               qualifications=qualifications,
                               placement_rules=placement_rules,
                               cooking_combo_rules=cooking_combo_rules)

    @app.route("/calendar")
    def calendar_page():
        """シフトカレンダーページ"""
        return render_template("calendar.html")

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
            weekend_constraint=request.form.get("weekend_constraint", ""),
            holiday_ng="holiday_ng" in request.form,
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

        if staff.staff_group == "cooking":
            staff.can_visit = False
            staff.has_phone_duty = False
            staff.can_bath_assist = False
            staff.available_time_slots = "full_day"
        else:
            staff.can_visit = "can_visit" in request.form
            staff.has_phone_duty = "has_phone_duty" in request.form
            staff.can_bath_assist = "can_bath_assist" in request.form
            staff.available_time_slots = request.form.get(
                "available_time_slots", staff.available_time_slots
            )

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
        staff.weekend_constraint = request.form.get("weekend_constraint", "")
        staff.holiday_ng = "holiday_ng" in request.form

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
        staff.work_start_time = (row.get("work_start_time") or "").strip()
        staff.work_end_time = (row.get("work_end_time") or "").strip()
        if group == "cooking":
            # 調理は職員フォームと同じく訪問・電話当番・入浴介助・時間帯を固定
            staff.can_visit = False
            staff.has_phone_duty = False
            staff.can_bath_assist = False
            staff.available_time_slots = "full_day"
        else:
            staff.can_visit = _csv_to_bool(row.get("can_visit"))
            staff.has_phone_duty = _csv_to_bool(row.get("has_phone_duty"))
            staff.can_bath_assist = _csv_to_bool(row.get("can_bath_assist"))
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

        s.min_day_service = safe_int(request.form.get("min_day_service"), 4)
        s.min_visit_am = safe_int(request.form.get("min_visit_am"), 1)
        s.min_visit_pm = safe_int(request.form.get("min_visit_pm"), 1)
        s.min_dual_assignment = safe_int(request.form.get("min_dual_assignment"), 2)
        s.min_early_staff = safe_int(request.form.get("min_early_staff"), 1)
        s.min_late_staff = safe_int(request.form.get("min_late_staff"), 1)
        s.closed_days = ",".join(request.form.getlist("closed_days"))
        s.visit_operating_days = ",".join(request.form.getlist("visit_operating_days"))
        s.min_cooking_staff = safe_int(request.form.get("min_cooking_staff"), 1)
        s.min_cooking_overlap = safe_int(request.form.get("min_cooking_overlap"), 2)
        s.am_preferred_gender = request.form.get("am_preferred_gender", "")
        s.phone_duty_enabled = "phone_duty_enabled" in request.form
        s.phone_duty_max_consecutive = safe_int(request.form.get("phone_duty_max_consecutive"), 1)
        s.min_staff_at_9 = safe_int(request.form.get("min_staff_at_9"), 4)
        s.min_staff_at_15 = safe_int(request.form.get("min_staff_at_15"), 4)
        s.male_am_constraint_mode = request.form.get("male_am_constraint_mode", "hard")
        s.max_day_service = safe_int(request.form.get("max_day_service"), 0)
        s.counselor_desk_enabled = "counselor_desk_enabled" in request.form
        s.counselor_desk_count = safe_int(request.form.get("counselor_desk_count"), 1)
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
        rule = PlacementRule(
            name=data["name"],
            rule_type=data.get("rule_type", "qualification_min"),
            target_qualification_ids_json=json.dumps(data.get("target_qualification_ids", [])),
            target_gender=data.get("target_gender", ""),
            period=data.get("period", "all"),
            time_start=data.get("time_start", ""),
            time_end=data.get("time_end", ""),
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

        staffs = Staff.query.all()
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

        # 許可アサインメント制限の取得
        all_allowed = StaffAllowedPattern.query.all()
        allowed_patterns_map = {}  # {staff_id: set(assignment_codes)}
        for ap in all_allowed:
            if ap.staff_id not in allowed_patterns_map:
                allowed_patterns_map[ap.staff_id] = set()
            allowed_patterns_map[ap.staff_id].add(ap.assignment_code)

        # 出勤可能日（whitelist）の取得: {staff_id: [YYYY-MM-DD, ...]}
        # 1日でも登録があれば、その職員は登録日のみ出勤可（solverで強制）。
        workable_dates_map = {}
        for w in StaffWorkableDate.query.all():
            workable_dates_map.setdefault(w.staff_id, []).append(w.date.isoformat())

        # ORM → dict 変換（部門別に分割）
        care_dicts = []
        cook_dicts = []
        for s in staffs:
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
                "staff_group": s.staff_group,
                "gender": s.gender,
                "has_phone_duty": s.has_phone_duty,
                "can_bath_assist": getattr(s, "can_bath_assist", False) or False,
                "qualification_ids": staff_qual_map.get(s.id, []),
                "qualification_names": staff_qual_names.get(s.id, []),
                "qualification_codes": staff_qual_codes.get(s.id, []),
                "weekend_constraint": getattr(s, "weekend_constraint", "") or "",
                "holiday_ng": getattr(s, "holiday_ng", False) or False,
                "workable_dates": workable_dates_map.get(s.id, []),
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
        visit_days = [int(x) for x in settings_obj.visit_operating_days.split(",") if x.strip()] if settings_obj.visit_operating_days else []

        settings_dict = {
            "min_day_service": settings_obj.min_day_service,
            "min_visit_am": settings_obj.min_visit_am,
            "min_visit_pm": settings_obj.min_visit_pm,
            "min_dual_assignment": settings_obj.min_dual_assignment,
            "min_early_staff": getattr(settings_obj, 'min_early_staff', 1) if getattr(settings_obj, 'min_early_staff', 1) is not None else 1,
            "min_late_staff": getattr(settings_obj, 'min_late_staff', 1) if getattr(settings_obj, 'min_late_staff', 1) is not None else 1,
            "closed_days": closed_days,
            "visit_operating_days": visit_days,
            "min_cooking_staff": settings_obj.min_cooking_staff,
            "min_cooking_overlap": settings_obj.min_cooking_overlap,
            "am_preferred_gender": getattr(settings_obj, 'am_preferred_gender', '') or '',
            "phone_duty_enabled": getattr(settings_obj, 'phone_duty_enabled', False) or False,
            "phone_duty_max_consecutive": getattr(settings_obj, 'phone_duty_max_consecutive', 1) or 1,
            "min_staff_at_9": getattr(settings_obj, 'min_staff_at_9', 4) or 4,
            "min_staff_at_15": getattr(settings_obj, 'min_staff_at_15', 4) or 4,
            "male_am_constraint_mode": getattr(settings_obj, 'male_am_constraint_mode', 'hard') or 'hard',
            "max_day_service": getattr(settings_obj, 'max_day_service', 0) or 0,
            "counselor_desk_enabled": getattr(settings_obj, 'counselor_desk_enabled', False) or False,
            "counselor_desk_count": getattr(settings_obj, 'counselor_desk_count', 1) or 1,
            "placement_rules": placement_rules_data,
            "cooking_combo_rules": cooking_combo_data,
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
            for st in Staff.query.filter_by(has_phone_duty=True).order_by(Staff.id).all():
                avail_wd = set(int(x) for x in st.available_days.split(",") if x.strip())
                fixed_wd = (set(int(x) for x in st.fixed_days_off.split(",") if x.strip())
                            if st.fixed_days_off else set())
                wk = set(workable_dates_map.get(st.id, []))
                offs = dayoff_by_staff.get(st.id, set())
                hol_ng = bool(getattr(st, "holiday_ng", False))
                # オンコールに入れない日を集約（休み希望・勤務不可曜日・出勤可能日外・祝日不可）
                unavailable = set()
                for dt in month_dates:
                    iso = dt.isoformat()
                    if (dt.weekday() not in avail_wd
                            or dt.weekday() in fixed_wd
                            or (wk and iso not in wk)
                            or iso in offs
                            or (hol_ng and jpholiday.is_holiday(dt))):
                        unavailable.add(iso)
                oncall_eligible.append(
                    {"id": st.id, "name": st.name, "unavailable": unavailable}
                )
            oncall_items, oncall_warnings = assign_oncall(
                oncall_eligible,
                month_dates,
                max_consecutive=settings_dict.get("phone_duty_max_consecutive", 1),
            )
            # オンコール当番の翌日を強制休み（翌日が当月内のもののみ）
            last_dom = calendar.monthrange(year, month)[1]
            forced_off = []
            for it in oncall_items:
                d = it["date"]
                if d.day < last_dom:
                    forced_off.append((it["staff_id"], (d + timedelta(days=1)).isoformat()))
            settings_dict["oncall_forced_off"] = forced_off

        # ソルバー実行（ケアと調理を独立して解く）
        try:
            shifts_data, warnings_data = generate_shift(
                year, month, care_dicts, cook_dicts, dayoff_dicts, settings_dict,
                allowed_patterns=allowed_patterns_map,
            )
        except Exception as e:
            app.logger.error("シフト生成中にエラーが発生しました", exc_info=True)
            return jsonify({"error": "シフト生成中にエラーが発生しました。設定や職員データを確認してください。"}), 500

        # 生成 ID を付与
        generation_id = str(uuid.uuid4())

        try:
            # 既存の同月シフトを削除（最新結果のみ保持）
            first_day = date(year, month, 1)
            last_day = date(year, month, calendar.monthrange(year, month)[1])
            GeneratedShift.query.filter(
                GeneratedShift.date >= first_day,
                GeneratedShift.date <= last_day,
            ).delete()
            ShiftWarning.query.filter(
                ShiftWarning.date >= first_day,
                ShiftWarning.date <= last_day,
            ).delete()
            OncallAssignment.query.filter(
                OncallAssignment.date >= first_day,
                OncallAssignment.date <= last_day,
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

        # 職員一覧（department情報付き）
        all_staff = Staff.query.order_by(Staff.id).all()

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

        # 確定情報（この月）
        conf = ShiftConfirmation.query.filter_by(year=year, month=month).first()

        return jsonify(
            {
                "year": year,
                "month": month,
                "generation_id": generation_id,
                "shifts": [s.to_dict() for s in shifts],
                "warnings": [w.to_dict() for w in warnings],
                "holidays": holidays,
                "oncall": oncall_map,
                "confirmation": conf.to_dict() if conf else None,
                "staff_list": [
                    {
                        "id": st.id,
                        "name": st.name,
                        "department": st.staff_group,
                        "qualifications": staff_qual_names.get(st.id, []),
                        "qualification_codes": staff_qual_codes.get(st.id, []),
                    }
                    for st in all_staff
                ],
            }
        )

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
    @app.route("/api/export/<generation_id>/excel", methods=["GET"])
    def api_export_excel(generation_id):
        """Excel ファイルとしてダウンロード"""
        shifts = GeneratedShift.query.filter_by(generation_id=generation_id).all()
        if not shifts:
            return jsonify({"error": "該当するシフトデータがありません"}), 404

        staffs = Staff.query.order_by(Staff.id).all()
        warnings = ShiftWarning.query.filter_by(generation_id=generation_id).all()

        first_date = shifts[0].date
        year = first_date.year
        month = first_date.month

        shifts_data = []
        for s in shifts:
            d = {
                "date": s.date.isoformat(),
                "staff_id": s.staff_id,
                "staff_name": s.staff.name if s.staff else "",
                "assignment": s.assignment,
                "is_phone_duty": s.is_phone_duty,
                "break_start": s.break_start,
                "bath_role": s.bath_role,
                "meal_assist": s.meal_assist,
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
            }
            for st in staffs
        ]

        oncall_rows = OncallAssignment.query.filter_by(generation_id=generation_id).all()
        oncall_map = {r.date.isoformat(): (r.staff.name if r.staff else "") for r in oncall_rows}

        buf = export_excel(shifts_data, warnings_data, staff_list_data, year, month, oncall_map=oncall_map)
        # ファイル名: シフト{役割}{YYMMDD}.xlsx（役割=ログイン中ユーザー、日付=JST）
        role = session.get("role", "")
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
        shifts = GeneratedShift.query.filter_by(generation_id=generation_id).all()
        if not shifts:
            return jsonify({"error": "該当するシフトデータがありません"}), 404

        staffs = Staff.query.order_by(Staff.id).all()
        warnings = ShiftWarning.query.filter_by(generation_id=generation_id).all()

        first_date = shifts[0].date
        year = first_date.year
        month = first_date.month

        shifts_data = []
        for s in shifts:
            d = {
                "date": s.date.isoformat(),
                "staff_id": s.staff_id,
                "staff_name": s.staff.name if s.staff else "",
                "assignment": s.assignment,
                "is_phone_duty": s.is_phone_duty,
                "break_start": s.break_start,
                "bath_role": s.bath_role,
                "meal_assist": s.meal_assist,
            }
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
        _staff_qual_ids, staff_qual_names_csv, staff_qual_codes_csv = _build_staff_qualification_maps()

        staff_list_data = [
            {
                "id": st.id,
                "name": st.name,
                "department": st.staff_group,
                "qualifications": staff_qual_names_csv.get(st.id, []),
                "qualification_codes": staff_qual_codes_csv.get(st.id, []),
            }
            for st in staffs
        ]

        oncall_rows = OncallAssignment.query.filter_by(generation_id=generation_id).all()
        oncall_map = {r.date.isoformat(): (r.staff.name if r.staff else "") for r in oncall_rows}

        csv_string = export_csv(shifts_data, warnings_data, staff_list_data, year, month, oncall_map=oncall_map)
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
