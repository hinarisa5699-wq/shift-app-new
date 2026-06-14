import importlib
import json
from pathlib import Path


def test_resolve_database_path_prefers_parent_directory(tmp_path, monkeypatch):
    app_dir = tmp_path / "shift-app"
    app_dir.mkdir()
    legacy_db = app_dir / "shift.db"
    legacy_db.write_text("legacy-db", encoding="utf-8")

    monkeypatch.delenv("SHIFT_APP_DB_PATH", raising=False)

    import config as config_module

    resolved = Path(config_module.resolve_database_path(app_dir))

    assert resolved == tmp_path / "shift.db"
    assert resolved.read_text(encoding="utf-8") == "legacy-db"


def test_normalize_qualifications_merges_legacy_social_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "test.db"))

    import config as config_module
    import app as app_module

    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    flask_app = app_module.create_app()

    from models import db, Qualification, PlacementRule, Staff, StaffQualification

    with flask_app.app_context():
        StaffQualification.query.delete()
        PlacementRule.query.delete()
        Qualification.query.delete()
        Staff.query.delete()
        db.session.commit()

        staff = Staff(
            name="相談員A",
            employment_type="常勤",
            staff_group="care",
            can_visit=False,
            has_phone_duty=False,
            gender="female",
            max_consecutive_days=5,
            max_days_per_week=5,
            min_days_per_week=0,
            available_days="0,1,2,3,4,5,6",
            available_time_slots="full_day",
            fixed_days_off="",
            weekend_constraint="",
            holiday_ng=False,
        )
        db.session.add(staff)
        db.session.flush()

        legacy_qual = Qualification(code="social_worker", name="生活相談員", display_order=1)
        canonical_qual = Qualification(code="counselor", name="相談員", display_order=9)
        db.session.add_all([legacy_qual, canonical_qual])
        db.session.flush()

        db.session.add(StaffQualification(staff_id=staff.id, qualification_id=legacy_qual.id))
        db.session.add(
            PlacementRule(
                name="相談員 午前1名以上",
                rule_type="qualification_min",
                target_qualification_ids_json=json.dumps([legacy_qual.id]),
                target_gender="",
                period="am",
                min_count=1,
                is_hard=True,
                penalty_weight=100,
                apply_weekdays="0,1,2,3,4,5,6",
                is_active=True,
            )
        )
        db.session.commit()

        app_module._normalize_qualifications()
        db.session.commit()

        quals = Qualification.query.order_by(Qualification.id).all()
        assert [(q.code, q.name) for q in quals] == [("counselor", "相談員")]

        links = StaffQualification.query.filter_by(staff_id=staff.id).all()
        assert len(links) == 1
        assert links[0].qualification_id == quals[0].id

        rule = PlacementRule.query.one()
        assert json.loads(rule.target_qualification_ids_json) == [quals[0].id]
