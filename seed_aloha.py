"""
seed_aloha.py — アロハ施設の実運用データをDBに投入するスクリプト
シフト条件 0223.xlsx のデータを正確に反映する。

使い方:
    python seed_aloha.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from models import (
    db, Staff, Qualification, StaffQualification,
    PlacementRule, StaffAllowedPattern, ShiftSettings,
    CookingComboRule, GeneratedShift, ShiftWarning,
    DayOffRequest,
)
import json


def seed():
    app = create_app()
    with app.app_context():
        print("=== アロハ施設 実運用データ投入開始 ===")

        # --------------------------------------------------
        # 1. 既存データを全削除（外部キー順に）
        # --------------------------------------------------
        print("[1/8] 既存データ削除...")
        DayOffRequest.query.delete()
        ShiftWarning.query.delete()
        GeneratedShift.query.delete()
        StaffAllowedPattern.query.delete()
        StaffQualification.query.delete()
        Staff.query.delete()
        db.session.commit()

        # --------------------------------------------------
        # 2. 資格マスタ（不足分を追加）
        # --------------------------------------------------
        print("[2/8] 資格マスタ更新...")
        existing_quals = {q.code: q for q in Qualification.query.all()}
        quals_to_add = [
            ("counselor", "相談員", 1),
            ("nurse", "看護師", 2),
            ("pt", "PT", 3),
            ("care_worker", "介護福祉士", 4),
            ("beginner", "初任者研修", 5),
            ("chef", "調理師", 6),
        ]
        qual_map = {}  # code -> id
        for code, name, order in quals_to_add:
            if code in existing_quals:
                q = existing_quals[code]
                q.name = name
                q.display_order = order
                qual_map[code] = q.id
            else:
                q = Qualification(code=code, name=name, display_order=order)
                db.session.add(q)
                db.session.flush()
                qual_map[code] = q.id
        db.session.commit()
        print(f"  資格: {qual_map}")

        # --------------------------------------------------
        # 3. 介護職員14名を登録
        # --------------------------------------------------
        print("[3/8] 介護職員登録 (14名)...")
        care_staff_data = [
            # (name, gender, employment_type, can_visit, max_pw, min_pw,
            #  available_days, time_slots, fixed_off, weekend_constraint,
            #  holiday_ng, has_phone_duty, qualifications, allowed_patterns)
            (
                "上村", "male", "常勤", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "",
                False, True,
                ["counselor", "beginner"],
                [],  # all patterns OK
            ),
            (
                "大野", "female", "時短正社員", True, 4, 4,
                "0,1,2,3,4,5,6", "full_day", "", "one_off",
                False, True,
                ["counselor", "care_worker"],
                [],
            ),
            (
                "岡本", "female", "常勤", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "",
                False, True,
                ["counselor", "care_worker"],
                [],
            ),
            (
                "俣野", "female", "パート", True, 3, 3,
                "0,1,2,3,4", "full_day", "", "",
                True, False,  # 土日祝NG
                ["beginner"],
                [],
            ),
            (
                "手倉森", "female", "パート", False, 4, 4,
                "0,1,4,5", "full_day", "", "",
                False, False,
                ["beginner"],
                [],  # can_visit=False で訪問不可
            ),
            (
                "藤本", "female", "常勤", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "",
                False, True,
                ["counselor", "care_worker"],
                [],
            ),
            (
                "植坂", "female", "パート", False, 3, 3,
                "0,1,2,3,4", "full_day", "", "",
                True, False,  # 土日祝NG
                ["care_worker"],
                ["day_pattern2"],  # 9:00-16:00のみ
            ),
            (
                "成井", "female", "時短正社員", True, 4, 4,
                "0,1,2,3,4", "full_day", "", "",
                True, True,  # 土日祝NG
                ["care_worker"],
                [],
            ),
            (
                "川島", "female", "パート", True, 3, 3,
                "0,1,2,3", "full_day", "", "",
                False, False,
                [],  # 資格なし
                [],
            ),
            (
                "大山", "female", "パート", False, 2, 2,
                "1,3", "full_day", "", "",
                True, False,  # 土日祝NG、火木のみ
                ["nurse"],
                ["day_pattern2"],  # 9:00-16:00のみ
            ),
            (
                "菊池", "female", "常勤", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "",
                False, True,
                ["counselor", "care_worker"],
                [],
            ),
            (
                "長谷川", "female", "管理者", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "one_off",
                False, True,
                ["counselor", "care_worker"],
                [],
            ),
            (
                "宮寺", "female", "パート", True, 3, 3,
                "0,1,2,3,4,5,6", "full_day", "", "one_off",
                False, False,
                ["care_worker"],
                [],
            ),
            (
                "A", "male", "パート", True, 5, 5,
                "0,1,2,3,4,5,6", "full_day", "", "",
                False, False,
                ["care_worker"],
                [],
            ),
        ]

        staff_ids = {}  # name -> id
        for (name, gender, emp_type, can_visit, max_pw, min_pw,
             avail_days, time_slots, fixed_off, wkend,
             hol_ng, phone, quals, allowed) in care_staff_data:
            s = Staff(
                name=name,
                staff_group="care",
                gender=gender,
                employment_type=emp_type,
                can_visit=can_visit,
                max_consecutive_days=5,
                max_days_per_week=max_pw,
                min_days_per_week=min_pw,
                available_days=avail_days,
                available_time_slots=time_slots,
                fixed_days_off=fixed_off,
                weekend_constraint=wkend,
                holiday_ng=hol_ng,
                has_phone_duty=phone,
            )
            db.session.add(s)
            db.session.flush()
            staff_ids[name] = s.id

            # 資格
            for qcode in quals:
                sq = StaffQualification(
                    staff_id=s.id,
                    qualification_id=qual_map[qcode],
                )
                db.session.add(sq)

            # 許可パターン
            for pat in allowed:
                ap = StaffAllowedPattern(
                    staff_id=s.id,
                    assignment_code=pat,
                )
                db.session.add(ap)

        db.session.commit()
        print(f"  介護職員: {staff_ids}")

        # --------------------------------------------------
        # 4. 調理職員6名を登録
        # --------------------------------------------------
        print("[4/8] 調理職員登録 (6名)...")
        cooking_staff_data = [
            # (name, gender, emp_type, max_pw, min_pw,
            #  available_days, holiday_ng, qualifications, allowed_patterns)
            (
                "竹原", "male", "パート", 3, 3,
                "0,2,5",  # 月水土
                False,
                ["chef"],
                ["cook_long"],  # ④ 6:00-13:00 のみ
            ),
            (
                "飯岡", "female", "常勤", 5, 5,
                "0,1,2,3,4,5,6",
                False,
                [],
                ["cook_late"],  # ③ 12:00-19:00 のみ
            ),
            (
                "中込", "female", "常勤", 4, 4,
                "0,1,2,3,4",  # 月-金
                True,  # 土日祝NG
                [],
                ["cook_morning"],  # ② 8:00-13:00 のみ
            ),
            (
                "B", "female", "常勤", 5, 5,
                "0,1,2,3,4,5,6",
                False,
                [],
                [],  # 全パターン可
            ),
            (
                "C", "female", "常勤", 5, 5,
                "0,1,2,3,4,5,6",
                False,
                [],
                [],  # 全パターン可
            ),
            (
                "D", "female", "常勤", 5, 5,
                "0,1,2,3,4,5,6",
                False,
                [],
                [],  # 全パターン可
            ),
        ]

        for (name, gender, emp_type, max_pw, min_pw,
             avail_days, hol_ng, quals, allowed) in cooking_staff_data:
            s = Staff(
                name=name,
                staff_group="cooking",
                gender=gender,
                employment_type=emp_type,
                can_visit=False,
                max_consecutive_days=5,
                max_days_per_week=max_pw,
                min_days_per_week=min_pw,
                available_days=avail_days,
                available_time_slots="full_day",
                fixed_days_off="",
                weekend_constraint="",
                holiday_ng=hol_ng,
                has_phone_duty=False,
            )
            db.session.add(s)
            db.session.flush()
            staff_ids[name] = s.id

            for qcode in quals:
                sq = StaffQualification(
                    staff_id=s.id,
                    qualification_id=qual_map[qcode],
                )
                db.session.add(sq)

            for pat in allowed:
                ap = StaffAllowedPattern(
                    staff_id=s.id,
                    assignment_code=pat,
                )
                db.session.add(ap)

        db.session.commit()
        print(f"  調理職員: { {k:v for k,v in staff_ids.items() if k in ['竹原','飯岡','中込','B','C','D']} }")

        # --------------------------------------------------
        # 5. 配置ルール修正
        # --------------------------------------------------
        print("[5/8] 配置ルール修正...")
        counselor_id = qual_map["counselor"]
        nurse_id = qual_map["nurse"]
        pt_id = qual_map["pt"]

        for rule in PlacementRule.query.all():
            if "相談員" in rule.name and "午前" in rule.name:
                rule.target_qualification_ids_json = json.dumps([counselor_id])
                print(f"  修正: {rule.name} → target_qualification_ids=[{counselor_id}]")
            elif "相談員" in rule.name and "午後" in rule.name:
                rule.target_qualification_ids_json = json.dumps([counselor_id])
                print(f"  修正: {rule.name} → target_qualification_ids=[{counselor_id}]")
            elif "看護" in rule.name:
                rule.target_qualification_ids_json = json.dumps([nurse_id, pt_id])
                print(f"  確認: {rule.name} → target_qualification_ids=[{nurse_id},{pt_id}]")
        db.session.commit()

        # --------------------------------------------------
        # 6. シフト条件設定
        # --------------------------------------------------
        print("[6/8] シフト条件設定...")
        settings = ShiftSettings.query.first()
        if not settings:
            settings = ShiftSettings()
            db.session.add(settings)

        settings.min_day_service = 4       # デイ最低4名（Excel: 毎日出勤人数4名）
        settings.max_day_service = 0       # 0=min_day_serviceと同じ
        settings.min_visit_am = 1          # 訪問午前1名
        settings.min_visit_pm = 1          # 訪問午後1名
        settings.min_dual_assignment = 0   # 兼務者最低人数（0=制約なし）
        settings.closed_days = ""          # 休業日なし（毎日営業）
        settings.visit_operating_days = "0,1,3,4"  # 月火木金
        settings.min_cooking_staff = 1     # 調理各スロット1名
        settings.min_cooking_overlap = 2   # 引き継ぎ重複2名
        settings.am_preferred_gender = ""
        settings.phone_duty_enabled = True
        settings.phone_duty_max_consecutive = 1
        settings.min_staff_at_9 = 4        # 9時最低4名
        settings.min_staff_at_15 = 4       # 15時最低4名
        settings.male_am_constraint_mode = "hard"
        settings.counselor_desk_enabled = True   # ③ 相談員事務ローテ有効
        settings.counselor_desk_count = 1        # 同時事務1名

        db.session.commit()
        print(f"  min_day=4, min_9=4, min_15=4, closed=なし, visit=月火木金, 事務ローテ=ON(1名)")

        # --------------------------------------------------
        # 7. 調理組み合わせルール修正
        # --------------------------------------------------
        print("[7/8] 調理組み合わせルール修正...")
        for combo in CookingComboRule.query.all():
            old = combo.allowed_patterns_json
            # cook_day → cook_morning に統一
            new = old.replace("cook_day", "cook_morning")
            if old != new:
                combo.allowed_patterns_json = new
                print(f"  修正: cook_day → cook_morning")
            # 正しいパターン確認
            patterns = json.loads(combo.allowed_patterns_json)
            print(f"  組み合わせ: {patterns}")
        db.session.commit()

        # --------------------------------------------------
        # 8. 最終確認
        # --------------------------------------------------
        print("\n[8/8] 最終確認...")
        care_count = Staff.query.filter_by(staff_group="care").count()
        cook_count = Staff.query.filter_by(staff_group="cooking").count()
        qual_count = Qualification.query.count()
        sq_count = StaffQualification.query.count()
        ap_count = StaffAllowedPattern.query.count()
        pr_count = PlacementRule.query.count()

        print(f"  介護職員: {care_count}名")
        print(f"  調理職員: {cook_count}名")
        print(f"  資格マスタ: {qual_count}件")
        print(f"  職員×資格: {sq_count}件")
        print(f"  許可パターン: {ap_count}件")
        print(f"  配置ルール: {pr_count}件")

        # 相談員の確認
        counselor_staff = (
            db.session.query(Staff.name)
            .join(StaffQualification)
            .filter(StaffQualification.qualification_id == counselor_id)
            .all()
        )
        print(f"  相談員: {[s.name for s in counselor_staff]}")

        # 看護師の確認
        nurse_staff = (
            db.session.query(Staff.name)
            .join(StaffQualification)
            .filter(StaffQualification.qualification_id == nurse_id)
            .all()
        )
        print(f"  看護師: {[s.name for s in nurse_staff]}")

        print("\n=== 投入完了 ===")


if __name__ == "__main__":
    seed()
