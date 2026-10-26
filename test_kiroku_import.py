"""アロハハウスアプリ（カイポケから同期した予定）の取り込み。

ユーザー依頼 2026-10:「カイポケから池田友子の予定同期させる。アロハハウスアプリから
同期後、シフトへも同期するようにできる？」

- 対象は職員マスタで「カイポケの訪問予定を取り込む」にチェックを入れた職員だけ。
- 予定シートの「担当」にフルネームがある行だけ（池田和男さんの予定にはならない）。
- 何度取り込んでも増えない。手入力・Googleカレンダーの予定は消えない。
"""
import datetime
import importlib

import pytest

import kiroku

GAS_URL = "https://script.google.com/macros/s/AKfyTEST_abc-123/exec"

# アロハハウスアプリの予定シート（action=month の schedules）と同じ形
ROWS = [
    {"id": "a", "日付": "2026-10-02", "氏名": "山田花子", "種別": "訪看",
     "開始": "15:00", "終了": "15:40", "内容": "訪問看護", "担当": "池田友子", "出所": "訪看"},
    {"id": "b", "日付": "2026-10-21", "氏名": "佐藤一郎", "種別": "訪看",
     "開始": "11:45", "終了": "12:35", "内容": "訪問看護", "担当": "池田友子・大山とよみ",
     "出所": "訪看"},
    {"id": "c", "日付": "2026-10-21", "氏名": "佐藤一郎", "種別": "訪看",
     "開始": "09:00", "終了": "09:30", "内容": "訪問看護", "担当": "大山とよみ", "出所": "訪看"},
    {"id": "d", "日付": "2026-10-05", "氏名": "鈴木次郎", "種別": "受診",
     "開始": "10:00", "終了": "", "内容": "通院", "担当": "池田和男", "出所": "手入力"},
    {"id": "e", "日付": "2026-10-09", "氏名": "山田花子", "種別": "デイ",
     "開始": "", "終了": "", "内容": "アロハデイ", "担当": "", "出所": "デイ"},
]


def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SHIFT_ADMIN_PASSWORD", "testpass")
    import config as config_module
    import app as app_module
    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    flask_app = app_module.create_app()
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return app_module, flask_app


def _login_admin(client):
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"


def _setup(flask_app, url=GAS_URL, token="secret"):
    from models import db, Staff, ShiftSettings
    with flask_app.app_context():
        s = ShiftSettings.query.first()
        if s is None:
            s = ShiftSettings()
            db.session.add(s)
        s.kiroku_gas_url = url
        s.kiroku_gas_token = token
        ids = {}
        for name, on in (("池田友子", True), ("池田和男", True), ("大山とよみ", False)):
            st = Staff(
                name=name, employment_type="常勤", staff_group="care",
                can_visit=True, max_consecutive_days=5, available_days="0,1,2,3,4",
                kiroku_import=on,
            )
            db.session.add(st)
            db.session.flush()
            ids[name] = st.id
        db.session.commit()
        return ids


# ---------------------------------------------------------------- 取り出し
def test_plans_for_staff_matches_full_name_only():
    rows = kiroku.plans_for_staff(ROWS, "池田 友子")
    assert [(r["date"], r["start_time"], r["end_time"], r["title"]) for r in rows] == [
        ("2026-10-02", "15:00", "15:40", "訪問看護 山田花子様"),
        ("2026-10-21", "11:45", "12:35", "訪問看護 佐藤一郎様"),   # 2人担当でも拾う
    ]
    # 同じ「池田」でも和男さんの予定は友子さんに入らない
    assert [r["uid"] for r in kiroku.plans_for_staff(ROWS, "池田和男")] == ["d"]


@pytest.mark.parametrize("bad", [
    "http://script.google.com/macros/s/x/exec",
    "https://example.com/macros/s/x/exec",
    "https://script.google.com/macros/s/x/exec?x=1",
    "https://script.google.com.evil.example/macros/s/x/exec",
])
def test_normalize_url_rejects_other_hosts(bad):
    with pytest.raises(kiroku.KirokuError):
        kiroku.normalize_url(bad)
    assert kiroku.normalize_url(" " + GAS_URL + " ") == GAS_URL
    assert kiroku.normalize_url("") == ""


# ---------------------------------------------------------------- 取り込みAPI
def test_import_all_brings_kaipoke_visits_into_the_schedule(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    ids = _setup(flask_app)
    calls = []

    def fake_fetch(url, token, ym, timeout=40):
        calls.append((url, token, ym))
        return ROWS
    monkeypatch.setattr(kiroku, "fetch_month_schedules", fake_fetch)

    from models import db, StaffPlan
    with flask_app.app_context():
        db.session.add(StaffPlan(
            staff_id=ids["池田友子"], date=datetime.date(2026, 10, 2),
            start_time="17:00", end_time="18:00", title="手入力の予定",
            display_order=0, source="manual",
        ))
        db.session.commit()

    client = flask_app.test_client()
    _login_admin(client)
    for _ in range(2):        # 2回押しても増えない
        res = client.post("/api/staff-plans/google-import-all",
                          json={"year": 2026, "month": 10})
        assert res.status_code == 200, res.get_data(as_text=True)
    assert calls[0] == (GAS_URL, "secret", "2026-10")

    by_name = {r["name"]: r for r in res.get_json()["results"]}
    assert by_name["池田友子"]["kiroku"] == 2
    assert by_name["池田和男"]["kiroku"] == 1
    assert "大山とよみ" not in by_name        # チェックなしは対象外

    with flask_app.app_context():
        rows = StaffPlan.query.filter_by(staff_id=ids["池田友子"]).order_by(
            StaffPlan.date, StaffPlan.source).all()
        assert [(r.date.day, r.source, r.label()) for r in rows] == [
            (2, "kiroku", "15:00-15:40 訪問看護 山田花子様"),
            (2, "manual", "17:00-18:00 手入力の予定"),       # 手入力は消えない
            (21, "kiroku", "11:45-12:35 訪問看護 佐藤一郎様"),
        ]


def test_manual_save_keeps_kiroku_plans(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    ids = _setup(flask_app)
    from models import db, StaffPlan
    with flask_app.app_context():
        db.session.add(StaffPlan(
            staff_id=ids["池田友子"], date=datetime.date(2026, 10, 2),
            start_time="15:00", end_time="15:40", title="訪問看護 山田花子様",
            display_order=0, source="kiroku", external_uid="a",
        ))
        db.session.commit()

    client = flask_app.test_client()
    _login_admin(client)
    res = client.post("/api/staff-plans", json={
        "staff_id": ids["池田友子"], "date": "2026-10-02",
        "items": [{"start": "17:00", "end": "18:00", "title": "本社"}],
    })
    assert res.status_code == 200, res.get_data(as_text=True)
    with flask_app.app_context():
        assert sorted(r.source for r in StaffPlan.query.filter_by(
            staff_id=ids["池田友子"]).all()) == ["kiroku", "manual"]


def test_import_all_shows_kiroku_error_in_japanese(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    _setup(flask_app)

    def boom(url, token, ym, timeout=40):
        raise kiroku.KirokuError("アロハハウスアプリの合言葉が違います。")
    monkeypatch.setattr(kiroku, "fetch_month_schedules", boom)

    client = flask_app.test_client()
    _login_admin(client)
    res = client.post("/api/staff-plans/google-import-all",
                      json={"year": 2026, "month": 10})
    assert res.status_code == 400
    assert "合言葉" in res.get_json()["error"]


def test_settings_keep_token_when_left_blank(tmp_path, monkeypatch):
    """合言葉は画面に出さない。空欄で保存しても消えない。おかしいURLは保存しない。"""
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    _setup(flask_app, url="", token="")
    client = flask_app.test_client()
    _login_admin(client)
    from models import ShiftSettings

    res = client.post("/api/settings", data={
        "kiroku_gas_url": GAS_URL, "kiroku_gas_token": "secret"})
    assert res.status_code in (301, 302), res.get_data(as_text=True)
    res = client.post("/api/settings", data={
        "kiroku_gas_url": "https://example.com/x", "kiroku_gas_token": ""})
    assert res.status_code in (301, 302)
    with flask_app.app_context():
        s = ShiftSettings.query.first()
        assert (s.kiroku_gas_url, s.kiroku_gas_token) == (GAS_URL, "secret")


def test_staff_form_saves_the_checkbox(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    ids = _setup(flask_app)
    client = flask_app.test_client()
    _login_admin(client)
    from models import Staff
    form = {
        "name": "大山とよみ", "employment_type": "常勤", "job_category": "nurse_rehab",
        "available_days": "0", "max_consecutive_days": "5", "kiroku_import": "1",
    }
    res = client.post(f"/api/staff/{ids['大山とよみ']}", data=form)
    assert res.status_code in (301, 302), res.get_data(as_text=True)
    with flask_app.app_context():
        assert Staff.query.get(ids["大山とよみ"]).kiroku_import is True
    form.pop("kiroku_import")
    client.post(f"/api/staff/{ids['大山とよみ']}", data=form)
    with flask_app.app_context():
        assert Staff.query.get(ids["大山とよみ"]).kiroku_import is False
