"""職員一覧・登録・編集（/staff）。モジュール直下のビューで endpoint 名を確実に登録する。"""
from flask import render_template

from models import (
    Staff,
    DayOffRequest,
    ShiftPattern,
    Qualification,
    StaffQualification,
    StaffAllowedPattern,
)


def staff_list():
    staffs = Staff.query.order_by(Staff.id).all()
    return render_template("staff_list.html", staff_list=staffs)


def staff_new():
    from app import _PATTERN_CODE_TO_ASSIGNMENT

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


def staff_edit(staff_id):
    from app import _PATTERN_CODE_TO_ASSIGNMENT

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


def register_staff_routes(app):
    """endpoint 名を url_for('staff_list') 等と一致させる"""
    app.add_url_rule("/staff", "staff_list", staff_list, methods=["GET"])
    app.add_url_rule("/staff/new", "staff_new", staff_new, methods=["GET"])
    app.add_url_rule(
        "/staff/<int:staff_id>/edit", "staff_edit", staff_edit, methods=["GET"]
    )
