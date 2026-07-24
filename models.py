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
    on_leave = db.Column(db.Boolean, default=False, nullable=False)
    # True = 休職中（シフト生成・オンコールの対象外。一覧には残しバッジ表示）
    public_holiday_count = db.Column(db.Integer, default=0, nullable=False)
    # 月の公休日数（=勤務以外の日数の目標。0=指定なし＝制約なし）。ソフトで==Nを目指す。

    # --- 追加カラム (v3) ---
    job_category = db.Column(db.String(20), default="caregiver", nullable=False)
    # 区分: "cooking" = 調理, "nurse_rehab" = 看護師・リハ, "caregiver" = 介護職員
    # ※ staff_group(care/cooking) は job_category から自動連動する（調理→cooking, それ以外→care）
    role = db.Column(db.String(20), default="", nullable=False)
    # 役割: "" = なし, "manager" = 管理者, "sekinin" = サ責, "sekinin_assist" = サ責補佐
    can_bath_assist = db.Column(db.Boolean, default=False)
    # True = 入浴介助可
    work_start_time = db.Column(db.String(5), default="", nullable=False)  # "HH:MM" 空欄可
    work_end_time = db.Column(db.String(5), default="", nullable=False)    # "HH:MM" 空欄可

    # --- 駐車場（依頼文24）---
    car_commute = db.Column(db.Boolean, default=False)  # True = 車通勤（駐車枠が必要）
    parking_slot = db.Column(db.String(10), default="", nullable=False)
    # 固定枠番号（例 "4"/"7"/"8"）。空欄ならローテーション扱い。

    # --- 調理スタッフの経験区分（依頼文28・新人/ベテラン）---
    cooking_experience = db.Column(db.String(10), default="", nullable=False)
    # "" = 未設定, "new" = 新人, "veteran" = ベテラン
    # 調理スタッフのみ意味を持つ。新人×ベテランのペア成立回数（ソフト目標）に使用。

    # --- 初出勤日（依頼文36・任意）---
    first_work_date = db.Column(db.Date, nullable=True)
    # 調理「新人」の教育期間（初出勤から連続3出勤日はベテラン同伴）の起点。
    # 未設定(None)の場合は生成対象月で最初に出勤する日を起点とみなす。

    # リレーション
    day_off_requests = db.relationship(
        "DayOffRequest", backref="staff", lazy=True, cascade="all, delete-orphan"
    )
    workable_dates = db.relationship(
        "StaffWorkableDate", backref="staff", lazy=True, cascade="all, delete-orphan"
    )
    oncall_assignments = db.relationship(
        "OncallAssignment", backref="staff", lazy=True, cascade="all, delete-orphan"
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
            "on_leave": self.on_leave or False,
            "public_holiday_count": self.public_holiday_count or 0,
            "job_category": self.job_category or "caregiver",
            "role": self.role or "",
            "can_bath_assist": self.can_bath_assist or False,
            "work_start_time": self.work_start_time or "",
            "work_end_time": self.work_end_time or "",
            "car_commute": self.car_commute or False,
            "parking_slot": self.parking_slot or "",
            "cooking_experience": self.cooking_experience or "",
            "first_work_date": self.first_work_date.isoformat() if self.first_work_date else None,
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


class StaffWorkableDate(db.Model):
    """出勤可能日（whitelist）
    エントリがある職員 → 指定された日付のみ出勤可（それ以外は休み）。
    エントリが無い職員 → 制約なし（従来通り全日出勤可能）。
    ※ 実際のシフト生成での適用は次段階で対応。今は入力・保存・表示のみ。
    """
    __tablename__ = "staff_workable_date"

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(
        db.Integer, db.ForeignKey("staff.id"), nullable=False
    )
    date = db.Column(db.Date, nullable=False)  # 出勤可能日

    __table_args__ = (
        db.UniqueConstraint("staff_id", "date", name="uq_staff_workable_date"),
    )

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "date": self.date.isoformat(),
        }


class OncallAssignment(db.Model):
    """オンコール（電話当番）割り当て
    1日1名。出勤の有無と独立して割り当てる（休みの担当者も対象）。
    生成IDごとに保持し、同月再生成で置き換える。
    """
    __tablename__ = "oncall_assignment"

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(db.String(36), nullable=False)
    date = db.Column(db.Date, nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("generation_id", "date", name="uq_oncall_gen_date"),
    )

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "generation_id": self.generation_id,
            "date": self.date.isoformat(),
            "staff_id": self.staff_id,
            "staff_name": self.staff.name if self.staff else None,
        }


class ParkingSlot(db.Model):
    """駐車枠マスタ（依頼文24）
    車通勤者に割り当てる枠番号の一覧（初期値 4/7/8）。後から編集できる。
    """
    __tablename__ = "parking_slot"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slot_number = db.Column(db.String(10), unique=True, nullable=False)  # "4"/"7"/"8"
    display_order = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_number": self.slot_number,
            "display_order": self.display_order or 0,
        }


class ParkingAssignment(db.Model):
    """駐車枠の割り当て（依頼文24）
    生成後に営業日ごと自動で振る。シフト生成(solver)とは独立。
    label は枠番号 or "コイン"（溢れ先）。生成IDごとに保持し同月再生成で置き換える。
    """
    __tablename__ = "parking_assignment"

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(db.String(36), nullable=False)
    date = db.Column(db.Date, nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    label = db.Column(db.String(20), nullable=False)  # "4"/"7"/"8" または "コイン"

    __table_args__ = (
        db.UniqueConstraint("generation_id", "date", "staff_id", name="uq_parking_gen_date_staff"),
    )

    staff = db.relationship("Staff")

    def to_dict(self):
        return {
            "id": self.id,
            "generation_id": self.generation_id,
            "date": self.date.isoformat(),
            "staff_id": self.staff_id,
            "label": self.label,
        }


class ShiftConfirmation(db.Model):
    """シフトの確定記録（月ごと）。同じ月で再確定したら上書き。"""
    __tablename__ = "shift_confirmation"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    confirmed_by = db.Column(db.String(50), default="")    # ログインユーザー名
    confirmed_role = db.Column(db.String(20), default="")  # 役割（管理者/サ責/役員）
    confirmed_at = db.Column(db.DateTime, nullable=False)  # 確定日時（JST wall-clock）

    __table_args__ = (
        db.UniqueConstraint("year", "month", name="uq_shift_confirmation_ym"),
    )

    def to_dict(self):
        return {
            "year": self.year,
            "month": self.month,
            "confirmed_by": self.confirmed_by or "",
            "confirmed_role": self.confirmed_role or "",
            "confirmed_at": self.confirmed_at.strftime("%Y/%m/%d %H:%M") if self.confirmed_at else None,
        }


class ShiftFix(db.Model):
    """職員ごとのシフト固定（依頼文28）
    エントリがある (staff_id, year, month) は「固定」＝再生成の対象外。
    既存シフトをそのまま残し、ソルバーの生成対象から除外する。
    解除はエントリ削除で行う。
    """
    __tablename__ = "shift_fix"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(
        db.Integer, db.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False
    )
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("staff_id", "year", "month", name="uq_shift_fix_staff_ym"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "staff_id": self.staff_id,
            "year": self.year,
            "month": self.month,
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
    min_early_staff = db.Column(db.Integer, default=1)  # 早番(7:30-16:30)の配置人数/日
    min_late_staff = db.Column(db.Integer, default=1)   # 遅番(9:30-18:30)の配置人数/日
    closed_days = db.Column(
        db.String(50), default=""
    )  # 休業曜日 (0=月〜6=日, カンマ区切り)
    closed_dates = db.Column(
        db.Text, default="", nullable=False
    )  # 休業日（日付指定・YYYY-MM-DD のカンマ区切り）。主に年末年始用。
    # closed_days（曜日）と同じ扱いで、その日は介護・調理とも全員 off になる。

    # --- 階別の営業曜日 (0=月〜6=日, カンマ区切り) ---
    #   これが設定の「正」。下の day_service_operating_days / visit_operating_days /
    #   no_day_service_days は保存時にここから自動算出される派生値。
    floor3_day_service_days = db.Column(
        db.String(50), default="1,4,6", nullable=False
    )  # 3階のデイ曜日（既定: 火・金・日）
    floor3_visit_days = db.Column(
        db.String(50), default="0,3", nullable=False
    )  # 3階の訪問曜日（既定: 月・木）
    floor2_day_service_days = db.Column(
        db.String(50), default="0,3,5", nullable=False
    )  # 2階のデイ曜日（既定: 月・木・土）
    floor2_visit_days = db.Column(
        db.String(50), default="1,4", nullable=False
    )  # 2階の訪問曜日（既定: 火・金）
    external_day_service_days = db.Column(
        db.String(50), default="2", nullable=False
    )  # 外部デイの曜日（既定: 水）。内部人員は不要。

    visit_operating_days = db.Column(
        db.String(50), default="0,1,3,4"
    )  # 訪問介護の営業曜日（派生値: 2階∪3階の訪問曜日）
    day_service_operating_days = db.Column(
        db.String(50), default="0,1,3,4,5,6", nullable=False
    )  # デイの営業曜日（派生値: 2階∪3階のデイ曜日）
    no_day_service_days = db.Column(
        db.String(50), default="2", nullable=False
    )  # デイ利用者がいない曜日。day_service_operating_days の裏返しとして自動保存される
    # この曜日はデイ人員を1名に緩和。外部デイのみの日もここに入る。
    min_cooking_staff = db.Column(
        db.Integer, default=1
    )  # 調理スタッフ最低配置人数/日
    min_cooking_overlap = db.Column(
        db.Integer, default=2
    )  # 引き継ぎ時間帯の重複人数 (12:00-13:00)
    # 朝食なし期間（この期間は朝[6-8]の調理を要求せず ④6-13 を ②8-13 表示に）。"YYYY-MM-DD" 空=無効
    breakfast_off_start = db.Column(db.String(10), default="", nullable=False)
    breakfast_off_end = db.Column(db.String(10), default="", nullable=False)
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

    # 依頼文35: 相談員ローテーション(counselor_desk_enabled / counselor_desk_count)は
    #   機能ごと削除。相談員の制御は counselor_care_mode（依頼文32）に一本化。

    # 相談員の介護業務参加モード（依頼文32）: "off"/"soft"/"hard"
    counselor_care_mode = db.Column(db.String(10), default="off", nullable=False)
    # "off"=制限なし(依頼文30の挙動) / "soft"=不足日のみ介護参加(強ペナルティ最小化) /
    # "hard"=相談員は介護業務に一切入らない（相談員業務のみ）。既定は off。

    # 調理：新人×ベテランのペア成立回数の目標値（依頼文28・ソフト目標）
    cooking_pair_target = db.Column(db.Integer, default=0)
    # 当月に「片方=新人・もう片方=ベテラン」の調理日を何回作りたいか。
    # 0 = 無効（ペア目標を課さない）。期間ではなく回数で指定する。

    # 依頼文40: お風呂当番 中介助/外介助の最低人数（0=制約なし＝その役割を割り当てない）
    min_bath_mid = db.Column(db.Integer, default=0)   # 中介助 最低人数/日
    min_bath_out = db.Column(db.Integer, default=0)   # 外介助 最低人数/日
    # 依頼文40: 中介助/外介助 連日回避（交互）モード "off"/"soft"/"hard"（既定 off）
    bath_role_alt_mode = db.Column(db.String(10), default="off", nullable=False)
    # 依頼文40: 早番/遅番 連日回避モード（廃止・列はダミーで残置）
    early_late_alt_mode = db.Column(db.String(10), default="off", nullable=False)
    # 遅番を中介助とするモード "off"/"soft"/"hard"（既定 hard＝従来動作）
    late_as_mid_mode = db.Column(db.String(10), default="hard", nullable=False)
    # 依頼文41-(1): 遅番×オンコール禁止モード "off"/"soft"/"hard"（既定 off）
    late_oncall_mode = db.Column(db.String(10), default="off", nullable=False)
    # 看護師・PT を早番/遅番に入れないか "off"/"hard"（既定 hard＝入れない＝介護職ローテ）
    nurse_early_late_mode = db.Column(db.String(10), default="hard", nullable=False)
    # 依頼文41-(2): 訪問回数の平等化モード "off"/"soft"/"hard"（既定 soft）
    visit_fairness_mode = db.Column(db.String(10), default="soft", nullable=False)
    # 訪問回数の平等化を hard にしたときの spread 上限（max−min ≤ この値）。既定 1。
    visit_fairness_max = db.Column(db.Integer, default=1, nullable=False)
    # 遅番の連日回避モード "off"/"soft"/"hard"（既定 soft＝同一職員が遅番を連日入らない）
    late_consecutive_mode = db.Column(db.String(10), default="soft", nullable=False)
    # 遅番日数の介護スタッフ間平等化モード "off"/"soft"/"hard"（既定 soft）
    late_fairness_mode = db.Column(db.String(10), default="soft", nullable=False)
    # 依頼文42: 早番の連日回避モード "off"/"soft"/"hard"（既定 soft＝同一職員が早番を連日入らない）
    early_consecutive_mode = db.Column(db.String(10), default="soft", nullable=False)
    # 依頼文43: 早番日数の介護スタッフ間平等化モード "off"/"soft"/"hard"（既定 soft）
    early_fairness_mode = db.Column(db.String(10), default="soft", nullable=False)
    # 依頼文43: 早番/遅番平等化を hard にしたときの spread 上限（max−min ≤ この値）。既定 1。
    early_fairness_max = db.Column(db.Integer, default=1, nullable=False)
    late_fairness_max = db.Column(db.Integer, default=1, nullable=False)
    # 依頼文43: オンコール回数の平等化モード "off"/"soft"/"hard"（既定 soft）＋hard時のspread上限。
    oncall_fairness_mode = db.Column(db.String(10), default="soft", nullable=False)
    oncall_fairness_max = db.Column(db.Integer, default=1, nullable=False)
    # 公休日数を法定労働時間ベースで自動算出するか（既定 off）。
    #   ON時、生成する月について各職員の公休日数を次式で自動反映（手入力より優先）:
    #     週所定労働時間 = min(週の勤務日数上限 × 1日の所定労働時間, 40)
    #     所定労働日数(上限) = floor(週所定労働時間 × 暦日数 ÷ 7 ÷ 1日の所定労働時間)
    #     公休数 = 暦日数 − 所定労働日数
    #   ※祝日は労働日扱い（公休に数えない）。
    auto_public_holidays = db.Column(db.Boolean, default=False, nullable=False)
    # 1日の所定労働時間（時間）。公休自動算出に使用。既定 8.0。
    daily_work_hours = db.Column(db.Float, default=8.0, nullable=False)

    def to_dict(self):
        """辞書形式に変換"""
        return {
            "id": self.id,
            "min_day_service": self.min_day_service,
            "min_visit_am": self.min_visit_am,
            "min_visit_pm": self.min_visit_pm,
            "min_dual_assignment": self.min_dual_assignment,
            "min_early_staff": self.min_early_staff if self.min_early_staff is not None else 1,
            "min_late_staff": self.min_late_staff if self.min_late_staff is not None else 1,
            "closed_days": self.closed_days,
            "closed_dates": self.closed_dates or "",
            "floor3_day_service_days": self.floor3_day_service_days or "",
            "floor3_visit_days": self.floor3_visit_days or "",
            "floor2_day_service_days": self.floor2_day_service_days or "",
            "floor2_visit_days": self.floor2_visit_days or "",
            "external_day_service_days": self.external_day_service_days or "",
            "visit_operating_days": self.visit_operating_days,
            "day_service_operating_days": self.day_service_operating_days or "",
            "no_day_service_days": self.no_day_service_days or "",
            "min_cooking_staff": self.min_cooking_staff,
            "min_cooking_overlap": self.min_cooking_overlap,
            "breakfast_off_start": self.breakfast_off_start or "",
            "breakfast_off_end": self.breakfast_off_end or "",
            "am_preferred_gender": self.am_preferred_gender,
            "phone_duty_enabled": self.phone_duty_enabled,
            "phone_duty_max_consecutive": self.phone_duty_max_consecutive,
            "min_staff_at_9": self.min_staff_at_9 if self.min_staff_at_9 is not None else 4,
            "min_staff_at_15": self.min_staff_at_15 if self.min_staff_at_15 is not None else 4,
            "male_am_constraint_mode": self.male_am_constraint_mode or "hard",
            "max_day_service": self.max_day_service or 0,
            "counselor_care_mode": self.counselor_care_mode or "off",
            "cooking_pair_target": self.cooking_pair_target or 0,
            "min_bath_mid": self.min_bath_mid if self.min_bath_mid is not None else 0,
            "min_bath_out": self.min_bath_out if self.min_bath_out is not None else 0,
            "bath_role_alt_mode": self.bath_role_alt_mode or "off",
            "late_as_mid_mode": self.late_as_mid_mode or "hard",
            "late_oncall_mode": self.late_oncall_mode or "off",
            "nurse_early_late_mode": self.nurse_early_late_mode or "hard",
            "visit_fairness_mode": self.visit_fairness_mode or "soft",
            "visit_fairness_max": self.visit_fairness_max if self.visit_fairness_max is not None else 1,
            "late_consecutive_mode": self.late_consecutive_mode or "soft",
            "late_fairness_mode": self.late_fairness_mode or "soft",
            "early_consecutive_mode": self.early_consecutive_mode or "soft",
            "early_fairness_mode": self.early_fairness_mode or "soft",
            "early_fairness_max": self.early_fairness_max if self.early_fairness_max is not None else 1,
            "late_fairness_max": self.late_fairness_max if self.late_fairness_max is not None else 1,
            "oncall_fairness_mode": self.oncall_fairness_mode or "soft",
            "oncall_fairness_max": self.oncall_fairness_max if self.oncall_fairness_max is not None else 1,
            "auto_public_holidays": self.auto_public_holidays or False,
            "daily_work_hours": self.daily_work_hours if self.daily_work_hours is not None else 8.0,
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
    counselor_desk_slots = db.Column(db.Text, nullable=True)  # ③ JSON: [0,2] = 事務スロットインデックス（[0,1,2,3]=終日相談）
    bath_role = db.Column(db.String(5), nullable=True)  # お風呂当番: "中" / "外" / None
    meal_assist = db.Column(db.String(20), nullable=True)  # 食事介助の担当時間帯 e.g. "12:00-13:00" / None

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
            "bath_role": self.bath_role,
            "meal_assist": self.meal_assist,
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
    # 調理種類のみ: 調理の充足人数（時間帯カバレッジ）に数えるか。
    #   事務など「調理シフト表には載るが調理はしない」種類は False にする。
    counts_as_cooking = db.Column(db.Boolean, default=True)

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
            "counts_as_cooking": (
                self.counts_as_cooking if self.counts_as_cooking is not None else True
            ),
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
