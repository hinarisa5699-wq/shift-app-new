"""ユーザーが追加した調理シフト種類も「許可シフトパターン」に保存できること。

不具合: 保存時の妥当コード判定が静的セット（cooking_1〜8）だったため、UIで追加した
6:30-14:30 などにチェックを入れても保存時に捨てられ、職員一覧を開き直すと
チェックが外れていた。solver へも渡らないので、そのシフトは一生割り当たらない。
"""
import importlib


def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "test.db"))
    import config as config_module
    import app as app_module
    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    return app_module, app_module.create_app()


def test_custom_cooking_type_is_kept(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    from models import db, ShiftPattern

    with flask_app.app_context():
        # UI で追加した想定の種類（コードは自動採番なので静的セットに載らない）
        db.session.add(ShiftPattern(
            code="cooking_12", staff_group="cooking", label="(9) 6:30-14:30",
            start_time="06:30", end_time="14:30", has_break=False, break_minutes=0,
            display_order=99, period="full", covers_am=True, covers_pm=True,
        ))
        db.session.commit()

        kept = app_module.normalize_allowed_pattern_codes(
            ["cooking_4", "cooking_12"], "cooking"
        )
        assert kept == ["cooking_4", "cooking_12"], kept

        # マスタに無いコードは従来どおり捨てる
        assert app_module.normalize_allowed_pattern_codes(
            ["cooking_999"], "cooking"
        ) == []


def test_morning_single_shift_type_exists_after_migration(tmp_path, monkeypatch):
    """朝食あり日の1人勤務用 6:30-14:30 がマスタに用意されること。"""
    _app_module, flask_app = _load_app(tmp_path, monkeypatch)
    from models import ShiftPattern

    with flask_app.app_context():
        rows = ShiftPattern.query.filter_by(
            staff_group="cooking", start_time="06:30", end_time="14:30"
        ).all()
        assert len(rows) == 1, [r.code for r in rows]
