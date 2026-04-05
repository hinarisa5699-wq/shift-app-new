"""職員一覧・登録・編集（/staff）。create_app 内の深いインデントでのミスを避けるため分離。"""
from flask import render_template

from models import (
    Staff,
    DayOffRequest,
    ShiftPattern,
    Qualification,
    StaffQualification,
    StaffAllowedPattern,
)


def register_staff_routes(app):
    from app import _PATTERN_CODE_TO_ASSIGNMENT

    @app.route("/staff")
    def staff_list():
        staffs = Staff.query.order_by(Staff.id).all()
        return render_template("staff_list.html", staff_list=staffs)

    @app.route("/staff/new")
    def staff_new():
        """職員登録フォーム"""
        qualifications = Qualification.query.order_by(Qualification.display_order).all()
        shift_patterns = ShiftPattern.query.order_by(ShiftPattern.display_order).all()
        return render_template(
            "staff_form.html",
            staff=None,
            qualifications=qualifications,
            shift_patterns=shift_patterns,
            allowed_pattern_codes=[],
            pattern_assignment_map=_PATTERN_CODE_TO_ASSIGNMENT,
        )

    @app.route("/staff/<int:staff_id>/edit")
    def staff_edit(staff_id):
        """職員編集フォーム"""
        staff = Staff.query.get_or_404(staff_id)
        day_offs = DayOffRequest.query.filter_by(staff_id=staff_id).order_by(DayOffRequest.date).all()
        qualifications = Qualification.query.order_by(Qualification.display_order).all()
        staff_qual_ids = [
            sq.qualification_id for sq in StaffQualification.query.filter_by(staff_id=staff_id).all()
        ]
        shift_patterns = ShiftPattern.query.order_by(ShiftPattern.display_order).all()
        allowed_pattern_codes = [
            ap.assignment_code for ap in StaffAllowedPattern.query.filter_by(staff_id=staff_id).all()
        ]
        return render_template(
            "staff_form.html",
            staff=staff,
            day_offs=day_offs,
            qualifications=qualifications,
            staff_qual_ids=staff_qual_ids,
            shift_patterns=shift_patterns,
            allowed_pattern_codes=allowed_pattern_codes,
            pattern_assignment_map=_PATTERN_CODE_TO_ASSIGNMENT,
        )
