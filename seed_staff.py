"""
seed_staff.py -- Excel職員データをDBに登録するスクリプト
全既存スタッフを削除し、20名（ケア14名+調理6名）を正確に登録する。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from models import (
    db, Staff, DayOffRequest, GeneratedShift, ShiftWarning,
    StaffQualification, StaffAllowedPattern, Qualification,
)


def seed():
    app = create_app()
    with app.app_context():
        # ============================================================
        # 既存データクリア
        # ============================================================
        StaffAllowedPattern.query.delete()
        StaffQualification.query.delete()
        DayOffRequest.query.delete()
        GeneratedShift.query.delete()
        ShiftWarning.query.delete()
        Staff.query.delete()
        db.session.commit()
        print("既存データをクリアしました。")

        # ============================================================
        # 資格マスタ取得
        # ============================================================
        qual_social = Qualification.query.filter_by(code="social_worker").first()
        qual_nurse = Qualification.query.filter_by(code="nurse").first()
        qual_care_worker = Qualification.query.filter_by(code="care_worker").first()
        qual_beginner = Qualification.query.filter_by(code="beginner").first()

        if not all([qual_social, qual_nurse, qual_care_worker, qual_beginner]):
            print("ERROR: 資格マスタが未登録です。先にapp.pyを一度起動してください。")
            return

        # ============================================================
        # ケアスタッフ 14名
        # ============================================================
        care_staff_data = [
            {
                "name": "上村",
                "gender": "male",
                "employment_type": "常勤",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_beginner.id, qual_social.id],
            },
            {
                "name": "大野",
                "gender": "female",
                "employment_type": "時短正社員",
                "can_visit": True,
                "max_days_per_week": 4,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id, qual_social.id],
            },
            {
                "name": "岡本",
                "gender": "female",
                "employment_type": "常勤",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id, qual_social.id],
            },
            {
                "name": "俣野",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": True,
                "max_days_per_week": 3,
                "available_days": "0,1,2,3,4",  # 月火水木金
                "fixed_days_off": "5,6",  # 土日
                "has_phone_duty": False,
                "qualifications": [qual_beginner.id],
            },
            {
                "name": "手倉森",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": False,
                "max_days_per_week": 4,
                "available_days": "0,1,4,5",  # 月火金土
                "fixed_days_off": "",
                "has_phone_duty": False,
                "qualifications": [qual_beginner.id],
            },
            {
                "name": "藤本",
                "gender": "female",
                "employment_type": "常勤",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id, qual_social.id],
            },
            {
                "name": "植坂",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": False,
                "max_days_per_week": 3,
                "available_days": "0,1,2,3,4",  # 月火水木金
                "fixed_days_off": "5,6",  # 土日
                "has_phone_duty": False,
                "qualifications": [qual_care_worker.id],
                "allowed_patterns": ["day_pattern2"],  # 9:00-16:00のみ
            },
            {
                "name": "成井",
                "gender": "female",
                "employment_type": "時短正社員",
                "can_visit": True,
                "max_days_per_week": 4,
                "available_days": "0,1,2,3,4",  # 月火水木金
                "fixed_days_off": "5,6",  # 土日
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id],
            },
            {
                "name": "川島",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": True,
                "max_days_per_week": 3,
                "available_days": "0,1,2,3",  # 月火水木
                "fixed_days_off": "4,6",  # 金日
                "has_phone_duty": False,
                "qualifications": [],  # 資格なし
            },
            {
                "name": "大山",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": False,
                "max_days_per_week": 2,
                "available_days": "1,3",  # 火木
                "fixed_days_off": "",
                "has_phone_duty": False,
                "qualifications": [qual_nurse.id],
                "allowed_patterns": ["day_pattern2"],  # 9:00-16:00のみ
            },
            {
                "name": "菊池",
                "gender": "female",
                "employment_type": "常勤",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id, qual_social.id],
            },
            {
                "name": "長谷川",
                "gender": "female",
                "employment_type": "管理者",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": True,
                "qualifications": [qual_care_worker.id, qual_social.id],
            },
            {
                "name": "宮寺",
                "gender": "female",
                "employment_type": "パート",
                "can_visit": True,
                "max_days_per_week": 3,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": False,
                "qualifications": [qual_care_worker.id],
            },
            {
                "name": "A（仮）",
                "gender": "male",
                "employment_type": "パート",
                "can_visit": True,
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "has_phone_duty": False,
                "qualifications": [qual_care_worker.id],
            },
        ]

        # ============================================================
        # 調理スタッフ 6名
        # ============================================================
        cook_staff_data = [
            {
                "name": "竹原",
                "gender": "male",
                "employment_type": "パート",
                "max_days_per_week": 3,
                "available_days": "0,2,5",  # 月水土
                "fixed_days_off": "",
                "allowed_patterns": ["cook_early", "cook_morning", "cook_long"],  # 6:00-13:00
            },
            {
                "name": "飯岡",
                "gender": "female",
                "employment_type": "パート",
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "allowed_patterns": ["cook_late"],  # 12:00-19:00
            },
            {
                "name": "中込",
                "gender": "female",
                "employment_type": "パート",
                "max_days_per_week": 4,
                "available_days": "0,1,2,3,4",  # 月火水木金
                "fixed_days_off": "5,6",  # 土日
                "allowed_patterns": ["cook_morning"],  # 8:00-13:00
            },
            {
                "name": "B（仮）",
                "gender": "female",
                "employment_type": "パート",
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "allowed_patterns": ["cook_early", "cook_morning", "cook_long"],  # 6:00-13:00
            },
            {
                "name": "C（仮）",
                "gender": "female",
                "employment_type": "パート",
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "allowed_patterns": ["cook_late"],  # 12:00-19:00
            },
            {
                "name": "D（仮）",
                "gender": "female",
                "employment_type": "パート",
                "max_days_per_week": 5,
                "available_days": "0,1,2,3,4,5,6",
                "fixed_days_off": "",
                "allowed_patterns": ["cook_morning"],  # 8:00-13:00
            },
        ]

        # ============================================================
        # ケアスタッフ登録
        # ============================================================
        for data in care_staff_data:
            staff = Staff(
                name=data["name"],
                gender=data["gender"],
                employment_type=data["employment_type"],
                staff_group="care",
                can_visit=data["can_visit"],
                max_consecutive_days=5,
                max_days_per_week=data["max_days_per_week"],
                available_days=data["available_days"],
                available_time_slots="full_day",
                fixed_days_off=data["fixed_days_off"],
                has_phone_duty=data["has_phone_duty"],
            )
            db.session.add(staff)
            db.session.flush()

            # 資格紐付け
            for qid in data["qualifications"]:
                db.session.add(StaffQualification(staff_id=staff.id, qualification_id=qid))

            # 許可パターン
            if "allowed_patterns" in data:
                for code in data["allowed_patterns"]:
                    db.session.add(StaffAllowedPattern(staff_id=staff.id, assignment_code=code))

            print(f"  ケア: {staff.name} (ID={staff.id})")

        # ============================================================
        # 調理スタッフ登録
        # ============================================================
        for data in cook_staff_data:
            staff = Staff(
                name=data["name"],
                gender=data["gender"],
                employment_type=data["employment_type"],
                staff_group="cooking",
                can_visit=False,
                max_consecutive_days=5,
                max_days_per_week=data["max_days_per_week"],
                available_days=data["available_days"],
                available_time_slots="full_day",
                fixed_days_off=data["fixed_days_off"],
                has_phone_duty=False,
            )
            db.session.add(staff)
            db.session.flush()

            # 許可パターン
            if "allowed_patterns" in data:
                for code in data["allowed_patterns"]:
                    db.session.add(StaffAllowedPattern(staff_id=staff.id, assignment_code=code))

            print(f"  調理: {staff.name} (ID={staff.id})")

        db.session.commit()

        # ============================================================
        # 検証
        # ============================================================
        care_count = Staff.query.filter_by(staff_group="care").count()
        cook_count = Staff.query.filter_by(staff_group="cooking").count()
        qual_count = StaffQualification.query.count()
        pattern_count = StaffAllowedPattern.query.count()

        print(f"\n登録完了:")
        print(f"  ケアスタッフ: {care_count}名")
        print(f"  調理スタッフ: {cook_count}名")
        print(f"  資格紐付け: {qual_count}件")
        print(f"  パターン制限: {pattern_count}件")

        # 詳細確認
        print(f"\n--- 資格紐付け詳細 ---")
        for sq in StaffQualification.query.all():
            staff = Staff.query.get(sq.staff_id)
            qual = Qualification.query.get(sq.qualification_id)
            print(f"  {staff.name} -> {qual.name}")

        print(f"\n--- パターン制限詳細 ---")
        for ap in StaffAllowedPattern.query.all():
            staff = Staff.query.get(ap.staff_id)
            print(f"  {staff.name} -> {ap.assignment_code}")


if __name__ == "__main__":
    seed()
