"""
models.py — SQLAlchemy データベースモデル定義
介護シフト自動作成アプリ
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Staff(db.Model):
    """職員マスタ"""
    __tablename__ = "staff"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)  # 氏名
    employment_type = db.Column(
        db.String(20), default="常勤"
    )  # "常勤", "時短正社員", "パート", "管理者"
    can_visit = db.Column(
        db.Boolean, default=False
    )  # True=デイ+訪問兼務可, False=デイのみ
    max_consecutive_days = db.Column(db.Integer, default=5)  # 連勤上限
    max_days_per_week = db.Column(db.Integer, default=5)  # 週勤務日数上限
    min_days_per_week = db.Column(db.Integer, default=0)  # 週勤務日数下限（0=制約なし）
    available_days = db.Column(
        db.String(50), default="0,1,2,3,4,5,6"
    )  # 勤務可能曜日 (0=月〜6=日, カンマ区切り)
    available_time_slots = db.Column(
        db.String(20), default="full_day"
    )  # "full_day", "am_only", "pm_only"
    fixed_days_off = db.Column(
        db.String(50), default=""
    )  # 固定休曜日 (カンマ区切り)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    staff_group = db.Column(
        db.String(20), default="care", nullable=False
    )  # "care" = ケアスタッフ, "cooking" = 調理スタッフ
    has_phone_duty = db.Column(
        db.Boolean, default=False
    )  # True = 電話当番対象者（★マーク表示）
    gender = db.Column(db.String(10), default="", nullable=False)
    # "" = 未設定, "male" = 男性, "female" = 女性
    weekend_constraint = db.Column(db.String(20), default="", nullable=False)
    # "" = 制約なし, "one_off" = 土日どちらかは休み（毎週）
    holiday_ng = db.Column(db.Boolean, default=False)
    # True = 祝日は出勤不可

    # リレーション
    day_off_requests = db.relationship(
        "DayOffRequest", backref="staff", lazy=True, cascade="all, delete-orphan"
    )
    generated_shifts = db.relationship(
        "GeneratedShift", backref="staff", lazy=True, cascade="all, delete-orphan"
    )
    qualifications = db.relationship(
        "StaffQualification", backref="staff", lazy=True, cascade="all, delete-orphan"
    )
    allowed_patterns = db.relationship(
        "StaffAllowedPattern", backref="staff", lazy=True, cascade="all, delete-orphan"
    )

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "name": self.name,
            "employment_type": self.employment_type,
            "can_visit": self.can_visit,
            "max_consecutive_days": self.max_consecutive_days,
            "max_days_per_week": self.max_days_per_week,
            "min_days_per_week": self.min_days_per_week or 0,
            "available_days": self.available_days,
            "available_time_slots": self.available_time_slots,
            "fixed_days_off": self.fixed_days_off,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "staff_group": self.staff_group,
            "has_phone_duty": self.has_phone_duty,
            "gender": self.gender,
            "weekend_constraint": self.weekend_constraint or "",
            "holiday_ng": self.holiday_ng or False,
        }


class DayOffRequest(db.Model):
    """休み希望"""
    __tablename__ = "day_off_request"

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(
        db.Integer, db.ForeignKey("staff.id"), nullable=False
    )
    date = db.Column(db.Date, nullable=False)  # 休み希望日

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "date": self.date.isoformat(),
        }


class ShiftSettings(db.Model):
    """シフト条件設定"""
    __tablename__ = "shift_settings"

    id = db.Column(db.Integer, primary_key=True)
    min_day_service = db.Column(db.Integer, default=4)  # デイサービス最低人数
    max_day_service = db.Column(db.Integer, default=0)  # デイサービス最大人数（0=min_day_serviceと同じ）
    min_visit_am = db.Column(db.Integer, default=1)  # 訪問午前最低人数
    min_visit_pm = db.Column(db.Integer, default=1)  # 訪問午後最低人数
    min_dual_assignment = db.Column(
        db.Integer, default=0
    )  # 兼務者最低人数/日
    closed_days = db.Column(
        db.String(50), default=""
    )  # 休業曜日 (0=月〜6=日, カンマ区切り)

    visit_operating_days = db.Column(
        db.String(50), default="0,1,3,4"
    )  # 訪問介護の営業曜日
    min_cooking_staff = db.Column(
        db.Integer, default=1
    )  # 調理スタッフ最低配置人数/日
    min_cooking_overlap = db.Column(
        db.Integer, default=2
    )  # 引き継ぎ時間帯の重複人数 (12:00-13:00)
    am_preferred_gender = db.Column(db.String(10), default="")
    phone_duty_enabled = db.Column(db.Boolean, default=True)
    phone_duty_max_consecutive = db.Column(db.Integer, default=1)

    # 9時・15時の事業所最低在籍人数
    min_staff_at_9 = db.Column(db.Integer, default=4)
    min_staff_at_15 = db.Column(db.Integer, default=4)

    # --- 新規カラム (v2) ---
    male_am_constraint_mode = db.Column(
        db.String(10), default="hard"
    )  # "hard" / "soft" / "off"

    # ③ 相談員事務ローテーション
    counselor_desk_enabled = db.Column(db.Boolean, default=False)
    counselor_desk_count = db.Column(db.Integer, default=1)  # 同時事務人数

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "min_day_service": self.min_day_service,
            "min_visit_am": self.min_visit_am,
            "min_visit_pm": self.min_visit_pm,
            "min_dual_assignment": self.min_dual_assignment,
            "closed_days": self.closed_days,
            "visit_operating_days": self.visit_operating_days,
            "min_cooking_staff": self.min_cooking_staff,
            "min_cooking_overlap": self.min_cooking_overlap,
            "am_preferred_gender": self.am_preferred_gender,
            "phone_duty_enabled": self.phone_duty_enabled,
            "phone_duty_max_consecutive": self.phone_duty_max_consecutive,
            "min_staff_at_9": self.min_staff_at_9 if self.min_staff_at_9 is not None else 4,
            "min_staff_at_15": self.min_staff_at_15 if self.min_staff_at_15 is not None else 4,
            "male_am_constraint_mode": self.male_am_constraint_mode or "hard",
            "max_day_service": self.max_day_service or 0,
            "counselor_desk_enabled": self.counselor_desk_enabled or False,
            "counselor_desk_count": self.counselor_desk_count if self.counselor_desk_count is not None else 1,
        }


class GeneratedShift(db.Model):
    """生成シフト結果"""
    __tablename__ = "generated_shift"

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(
        db.String(36), nullable=False
    )  # UUID — 1回の生成で共通のID
    date = db.Column(db.Date, nullable=False)
    staff_id = db.Column(
        db.Integer, db.ForeignKey("staff.id"), nullable=False
    )
    assignment = db.Column(
        db.String(30), nullable=False
    )  # "day_pattern1", "day_pattern3", "cook_early" 等
    shift_pattern_code = db.Column(
        db.String(30), nullable=True
    )  # シフトパターンコード
    is_phone_duty = db.Column(db.Boolean, default=False)
    break_start = db.Column(db.String(5), nullable=True)  # ① 休憩開始時刻 e.g. "12:00"
    counselor_desk_slots = db.Column(db.Text, nullable=True)  # ③ JSON: [0,2] = 事務スロットインデックス

    def to_dict(self):
        """辞書形式に変換"""
        import json as _json
        desk_slots = None
        if self.counselor_desk_slots:
            try:
                desk_slots = _json.loads(self.counselor_desk_slots)
            except (ValueError, TypeError):
                pass
        return {
            "id": self.id,
            "generation_id": self.generation_id,
            "date": self.date.isoformat(),
            "staff_id": self.staff_id,
            "assignment": self.assignment,
            "shift_pattern_code": self.shift_pattern_code,
            "is_phone_duty": self.is_phone_duty,
            "break_start": self.break_start,
            "staff_name": self.staff.name if self.staff else None,
            "counselor_desk_slots": desk_slots,
        }


class ShiftPattern(db.Model):
    """シフトパターン定義"""
    __tablename__ = "shift_pattern"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    staff_group = db.Column(db.String(20), nullable=False)  # "care" or "cooking"
    label = db.Column(db.String(50), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    has_break = db.Column(db.Boolean, default=False)
    break_minutes = db.Column(db.Integer, default=0)
    display_order = db.Column(db.Integer, default=0)
    period = db.Column(db.String(10), default="full")  # "full" / "am" / "pm"
    covers_am = db.Column(db.Boolean, default=True)
    covers_pm = db.Column(db.Boolean, default=True)

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "code": self.code,
            "staff_group": self.staff_group,
            "label": self.label,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "has_break": self.has_break,
            "break_minutes": self.break_minutes,
            "display_order": self.display_order,
            "period": self.period or "full",
            "covers_am": self.covers_am if self.covers_am is not None else True,
            "covers_pm": self.covers_pm if self.covers_pm is not None else True,
        }


class ShiftWarning(db.Model):
    """警告"""
    __tablename__ = "shift_warning"

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(db.String(36), nullable=False)
    date = db.Column(db.Date, nullable=False)
    warning_type = db.Column(
        db.String(50)
    )  # "understaffed", "no_solution" 等
    message = db.Column(db.String(500))

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "generation_id": self.generation_id,
            "date": self.date.isoformat(),
            "warning_type": self.warning_type,
            "message": self.message,
        }


# ===========================================================================
# 新規テーブル (v2)
# ===========================================================================

class Qualification(db.Model):
    """資格マスタ"""
    __tablename__ = "qualification"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(50), nullable=False)
    display_order = db.Column(db.Integer, default=0)

    staff_qualifications = db.relationship(
        "StaffQualification", backref="qualification", lazy=True, cascade="all, delete-orphan"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "display_order": self.display_order,
        }


class StaffQualification(db.Model):
    """職員×資格 多対多"""
    __tablename__ = "staff_qualification"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    qualification_id = db.Column(db.Integer, db.ForeignKey("qualification.id"), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("staff_id", "qualification_id", name="uq_staff_qual"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "qualification_id": self.qualification_id,
        }


class PlacementRule(db.Model):
    """配置ルール（汎用）"""
    __tablename__ = "placement_rule"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    rule_type = db.Column(
        db.String(30), nullable=False
    )  # "qualification_min" / "gender_min" / "headcount_min"
    target_qualification_ids_json = db.Column(
        db.Text, default="[]"
    )  # JSON: [1, 2] — いずれかの資格を持つ職員が対象
    target_gender = db.Column(
        db.String(10), default=""
    )  # "male" / "female" / ""
    period = db.Column(
        db.String(10), default="all"
    )  # "am" / "pm" / "all"
    time_start = db.Column(db.String(5), default="")  # "09:00" など（将来用）
    time_end = db.Column(db.String(5), default="")  # "16:00" など（将来用）
    min_count = db.Column(db.Integer, default=1)
    is_hard = db.Column(db.Boolean, default=True)
    penalty_weight = db.Column(db.Integer, default=100)
    apply_weekdays = db.Column(
        db.String(50), default="0,1,2,3,4,5,6"
    )  # 適用曜日
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "rule_type": self.rule_type,
            "target_qualification_ids": json.loads(self.target_qualification_ids_json or "[]"),
            "target_gender": self.target_gender or "",
            "period": self.period or "all",
            "time_start": self.time_start or "",
            "time_end": self.time_end or "",
            "min_count": self.min_count,
            "is_hard": self.is_hard,
            "penalty_weight": self.penalty_weight,
            "apply_weekdays": self.apply_weekdays or "0,1,2,3,4,5,6",
            "is_active": self.is_active,
        }


class StaffAllowedPattern(db.Model):
    """職員ごとの許可アサインメント制限
    エントリがある職員 → そのアサインメントのみ許可（off/cook_off は常に許可）
    エントリがない職員 → 全アサインメント許可（後方互換）
    """
    __tablename__ = "staff_allowed_pattern"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False)
    assignment_code = db.Column(db.String(30), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("staff_id", "assignment_code", name="uq_staff_allowed_pattern"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "assignment_code": self.assignment_code,
        }


class CookingComboRule(db.Model):
    """調理の日単位組み合わせルール"""
    __tablename__ = "cooking_combo_rule"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    allowed_patterns_json = db.Column(
        db.Text, nullable=False
    )  # JSON: [["cook_early","cook_morning","cook_late"],["cook_late","cook_long"]]
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "allowed_patterns": json.loads(self.allowed_patterns_json or "[]"),
            "is_active": self.is_active,
        }
