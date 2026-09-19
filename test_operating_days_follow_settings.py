"""営業曜日は「設定した曜日」のとおりに動くかのテスト。

ユーザー依頼 2026-09:「設定で何曜日って決めたら設定どおりして」。

きっかけ:
  デイ／訪問の営業曜日は、階別のチェック（3階デイ・2階デイ・3階訪問・2階訪問）から
  計算した値を別の列にも保存しており、シフト自動作成はその「保存列」を読んでいた。
  階別を変えずに保存列だけ書き換えられた古いデータ（seed スクリプトがそうしていた）だと、
  設定画面では訪問＝火・金なのに実際は火・木に入る、という食い違いが起きた。
  → 保存列は見ず、毎回 operating_day_sets() で階別から計算する形にした。
"""
from models import ShiftSettings


def _settings(**kw):
    s = ShiftSettings()
    s.floor3_day_service_days = kw.get("f3ds", "1,4,6")   # 火金日
    s.floor2_day_service_days = kw.get("f2ds", "0,3,5,6")  # 月木土日
    s.floor3_visit_days = kw.get("f3v", "1,4")             # 火金
    s.floor2_visit_days = kw.get("f2v", "1,4")             # 火金
    s.external_day_service_days = kw.get("ext", "2")       # 水
    # わざと食い違う古い保存列を入れる（これに引きずられないことを確かめる）
    s.day_service_operating_days = kw.get("stale_ds", "0,2,4")   # 月水金
    s.visit_operating_days = kw.get("stale_v", "1,3")            # 火木
    s.no_day_service_days = kw.get("stale_nods", "1,3,5,6")
    return s


def test_operating_days_come_from_the_floor_checkboxes():
    """保存列が古くても、階別のチェックどおりの曜日になる。"""
    sets = _settings().operating_day_sets()
    assert sets["visit"] == [1, 4], "訪問は設定どおり火・金であるべき"
    assert sets["day_service"] == [0, 1, 3, 4, 5, 6]
    assert sets["no_day_service"] == [2]   # デイ営業日の裏返し（水）


def test_changing_the_setting_changes_the_result():
    """チェックを変えれば結果もそのまま変わる。"""
    sets = _settings(f3v="0", f2v="3").operating_day_sets()   # 3階=月, 2階=木
    assert sets["visit"] == [0, 3]

    sets = _settings(f3v="", f2v="").operating_day_sets()     # 訪問なし
    assert sets["visit"] == []


def test_sync_rewrites_the_stale_columns():
    """控えの列は階別から計算し直して書き戻す（1度直せば2度目は変化なし）。"""
    s = _settings()
    assert s.sync_derived_operating_days() is True
    assert s.visit_operating_days == "1,4"
    assert s.day_service_operating_days == "0,1,3,4,5,6"
    assert s.no_day_service_days == "2"
    # もう変えるところはない
    assert s.sync_derived_operating_days() is False


def test_to_dict_reports_the_calculated_days():
    """設定のJSONも計算後の曜日を返す（古い保存列をそのまま出さない）。"""
    d = _settings().to_dict()
    assert d["visit_operating_days"] == "1,4"
    assert d["day_service_operating_days"] == "0,1,3,4,5,6"
    assert d["no_day_service_days"] == "2"


def test_generation_uses_the_setting_not_the_stale_column(tmp_path, monkeypatch):
    """シフト生成も設定どおりの曜日で動く（保存列が古くても引きずらない）。"""
    from test_generate_endpoint import _make_app, _seed, _login

    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)

    from models import db, ShiftSettings as SS, GeneratedShift, Staff

    with flask_app.app_context():
        s = SS.query.first()
        s.min_visit_am = 1          # 訪問を1名必要にする（_seed は0にしている）
        s.min_visit_pm = 0
        # 設定: 訪問は火・金。保存列だけ古い値（火・木）にしておく。
        s.floor3_visit_days = "1,4"
        s.floor2_visit_days = "1,4"
        s.floor3_day_service_days = "0,1,2,3,4"
        s.floor2_day_service_days = "0,1,2,3,4"
        s.external_day_service_days = ""
        s.visit_operating_days = "1,3"
        s.day_service_operating_days = "0,2,4"
        s.no_day_service_days = "1,3,5,6"
        db.session.commit()

    client = flask_app.test_client()
    assert _login(client, "admin", "testpass").status_code in (301, 302)
    res = client.post("/api/generate", json={"year": 2026, "month": 10})
    assert res.status_code == 200, res.get_json()

    visit_codes = {"visit_am", "visit_pm", "visit_am_day_p4", "day_p3_visit_pm"}
    with flask_app.app_context():
        care_ids = {r.id for r in Staff.query.filter_by(staff_group="care").all()}
        weekdays = {
            r.date.weekday() for r in GeneratedShift.query.all()
            if r.staff_id in care_ids and r.assignment in visit_codes
            and r.date.month == 10
        }
    assert weekdays, "訪問が1件も入っていません"
    assert weekdays <= {1, 4}, (
        f"設定は火・金なのに {sorted(weekdays)} の曜日に訪問が入りました"
        "（0=月〜6=日）。古い保存列を読んでいる可能性があります。"
    )
    assert 3 not in weekdays, "古い保存列（火・木）のまま木曜に訪問が入っています"
