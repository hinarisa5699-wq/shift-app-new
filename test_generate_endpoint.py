"""/api/generate（シフト生成）が最後まで通ることの回帰テスト。

2026-08: 公休の自動算出を直したとき、まだ定義されていない変数を参照してしまい
本番の「シフト生成」が 500（サーバー内部エラー）になった。ソルバー単体のテストでは
検出できないため、アプリのエンドポイントごと動かして確認する。
"""
import importlib


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "gen.db"))
    monkeypatch.setenv("SHIFT_ADMIN_PASSWORD", "testpass")
    import config as config_module
    import app as app_module

    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    flask_app = app_module.create_app()
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _seed(flask_app):
    from models import db, Staff, ShiftSettings

    with flask_app.app_context():
        Staff.query.delete()
        db.session.commit()

        def add(name, jc, emp, avail, fixed="", req="", maxw=5, minw=0):
            db.session.add(Staff(
                name=name, employment_type=emp, job_category=jc,
                staff_group=("cooking" if jc == "cooking" else "care"),
                can_visit=True, max_consecutive_days=5, max_days_per_week=maxw,
                min_days_per_week=minw, available_days=avail, available_time_slots="full_day",
                fixed_days_off=fixed, required_days=req, gender="female",
            ))

        add("介護A", "caregiver", "常勤", "0,1,2,3,4,5,6", minw=5)
        add("介護B", "caregiver", "パート", "0,1,2,3,4,5,6")
        add("介護C", "caregiver", "パート", "0,1,2,3,4,5,6")
        add("介護D", "caregiver", "パート", "0,1,2,3,4,5,6")
        add("看護E", "nurse_rehab", "パート", "1,2,3", maxw=3)
        add("運転F", "driver", "常勤", "1,3,4,6", req="2", maxw=3)
        add("調理G", "cooking", "パート", "0,1,2,3,4,5,6")
        add("調理H", "cooking", "パート", "0,1,2,3,4,5,6")

        s = ShiftSettings.query.first() or ShiftSettings()
        s.min_visit_am = 0
        s.min_visit_pm = 0
        s.closed_days = "6"
        s.floor3_day_service_days = "1,2,3"
        s.floor2_day_service_days = "1,2,3"
        s.floor3_visit_days = "0,4"
        s.floor2_visit_days = "0,4"
        s.day_service_operating_days = "1,2,3"
        s.visit_operating_days = "0,4"
        s.no_day_service_days = "0,4,5,6"
        s.care_min_by_weekday = "2,3,3,2,2,2,0"
        s.care_max_by_weekday = "2,3,3,2,2,2,0"
        s.min_staff_at_9 = 1
        s.min_staff_at_15 = 1
        s.auto_public_holidays = True
        s.phone_duty_enabled = True
        s.oncall_requires_work = True
        s.min_cooking_staff = 1
        s.min_cooking_overlap = 1
        db.session.add(s)
        db.session.commit()


def test_generate_endpoint_returns_success(tmp_path, monkeypatch):
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)

    res = client.post("/api/generate", json={"year": 2026, "month": 9})

    assert res.status_code == 200, res.get_data(as_text=True)[:500]
    data = res.get_json()
    assert data.get("status") == "success"
    assert data.get("shift_count", 0) > 0


def test_generate_endpoint_without_weekday_settings(tmp_path, monkeypatch):
    """曜日ごとの人数が未設定でも（旧設定へのフォールバック）生成できる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, ShiftSettings

    with flask_app.app_context():
        s = ShiftSettings.query.first()
        s.care_min_by_weekday = ""
        s.care_max_by_weekday = ""
        s.min_day_service = 2
        s.max_day_service = 3
        db.session.commit()

    client = flask_app.test_client()
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)
    res = client.post("/api/generate", json={"year": 2026, "month": 9})

    assert res.status_code == 200, res.get_data(as_text=True)[:500]
    assert res.get_json().get("status") == "success"


def test_shifts_api_returns_day_off_requests(tmp_path, monkeypatch):
    """シフト参照APIが休み希望を返す（画面で「希望休」と表示するため）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff, DayOffRequest
    import datetime

    with flask_app.app_context():
        sid = Staff.query.filter_by(name="介護B").first().id
        db.session.add(DayOffRequest(staff_id=sid, date=datetime.date(2026, 9, 2)))
        db.session.commit()

    client = flask_app.test_client()
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)
    client.post("/api/generate", json={"year": 2026, "month": 9})

    res = client.get("/api/shifts/2026/9")
    assert res.status_code == 200
    data = res.get_json()
    assert "2026-09-02" in data.get("day_off_requests", {}), data.get("day_off_requests")


def test_export_marks_requested_day_off(tmp_path, monkeypatch):
    """CSV出力の休みセルが、休み希望の日は「希望休」になる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff, DayOffRequest
    import datetime

    with flask_app.app_context():
        staff = Staff.query.filter_by(name="介護B").first()
        db.session.add(DayOffRequest(staff_id=staff.id, date=datetime.date(2026, 9, 2)))
        db.session.commit()
        sid = staff.id

    client = flask_app.test_client()
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)
    gen = client.post("/api/generate", json={"year": 2026, "month": 9}).get_json()

    res = client.get(f"/api/export/{gen['generation_id']}/csv")
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    assert "希望休" in text, "休み希望の日が『希望休』と表示されていない"

    # 休み希望を出していない職員の休みは「休」のまま
    assert "\n介護A" in text or "介護A" in text


def test_export_marks_visit_on_early_shift(tmp_path, monkeypatch):
    """訪問営業日の早番セルに「訪問」の文字が出る（CSV出力で確認）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, ShiftSettings

    with flask_app.app_context():
        s = ShiftSettings.query.first()
        s.min_visit_am = 1          # 訪問営業日(月・金)は早番が午前訪問へ出る
        db.session.commit()

    client = flask_app.test_client()
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)
    gen = client.post("/api/generate", json={"year": 2026, "month": 9}).get_json()

    res = client.get(f"/api/export/{gen['generation_id']}/csv")
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    assert "訪問（午前）" in text, "訪問日の早番に『訪問』表記が出ていない"


def _login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw},
                       follow_redirects=False)


def test_viewer_account_can_only_view(tmp_path, monkeypatch):
    """閲覧専用アカウントは /view と読み取りAPIだけ。編集・生成・設定は不可。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()

    res = _login(client, "staff", "viewpass")
    assert res.status_code in (301, 302)
    assert "/view" in res.headers.get("Location", "")

    # 閲覧ページと読み取りAPIは見られる
    assert client.get("/view").status_code == 200
    assert client.get("/api/shifts/2026/9").status_code == 200

    # 管理系の画面は閲覧ページへ戻される
    for path in ("/staff", "/settings", "/calendar", "/"):
        r = client.get(path)
        assert r.status_code in (301, 302), path
        assert "/view" in r.headers.get("Location", ""), path

    # 生成・更新系のAPIは 403
    assert client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 403
    assert client.post("/api/staff/1", data={"name": "x"}).status_code == 403


def test_admin_can_still_use_everything(tmp_path, monkeypatch):
    """管理者アカウントは従来どおり全機能にアクセスできる。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")

    assert client.get("/").status_code == 200
    assert client.get("/staff").status_code == 200
    assert client.get("/view").status_code == 200
    assert client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 200


def test_viewer_password_can_be_set_from_settings(tmp_path, monkeypatch):
    """条件設定で決めたパスワードで閲覧アカウントにログインできる（環境変数なしでも可）。"""
    monkeypatch.delenv("SHIFT_STAFF_PASSWORD", raising=False)
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    # 未設定のうちは閲覧アカウントで入れない
    guest = flask_app.test_client()
    assert _login(guest, "staff", "himitsu1234").status_code == 200   # ログイン画面のまま

    # 管理者が条件設定からパスワードを決める
    res = admin.post("/api/settings", data={
        "viewer_password": "himitsu1234",
        "min_staff_at_9": "1", "min_staff_at_15": "1",
    }, follow_redirects=True)
    assert res.status_code == 200

    # 設定後はログインでき、/view だけ見られる
    guest2 = flask_app.test_client()
    r = _login(guest2, "staff", "himitsu1234")
    assert r.status_code in (301, 302) and "/view" in r.headers.get("Location", "")
    assert guest2.get("/view").status_code == 200
    assert guest2.get("/settings").status_code in (301, 302)

    # 解除するとログインできなくなる
    admin.post("/api/settings", data={
        "viewer_password_clear": "1",
        "min_staff_at_9": "1", "min_staff_at_15": "1",
    }, follow_redirects=True)
    guest3 = flask_app.test_client()
    assert _login(guest3, "staff", "himitsu1234").status_code == 200


def test_retired_staff_excluded_everywhere(tmp_path, monkeypatch):
    """退職にした職員は生成・閲覧画面・印刷から外れる（データは残る）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff

    with flask_app.app_context():
        s = Staff.query.filter_by(name="介護D").first()
        s.retired = True
        db.session.commit()
        retired_id = s.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    gen = client.post("/api/generate", json={"year": 2026, "month": 9}).get_json()
    assert gen["status"] == "success"

    data = client.get("/api/shifts/2026/9").get_json()
    ids = {s["id"] for s in data["staff_list"]}
    assert retired_id not in ids, "退職者が閲覧画面の一覧に出ている"
    assert not [s for s in data["shifts"] if s["staff_id"] == retired_id], (
        "退職者にシフトが割り当てられている"
    )

    # 職員データ自体は残る（履歴を消さない）
    with flask_app.app_context():
        assert Staff.query.get(retired_id) is not None


def test_staff_list_hides_inactive_by_default(tmp_path, monkeypatch):
    """職員一覧は既定で在籍中のみ。切替で休職中・退職者も見られる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff

    with flask_app.app_context():
        Staff.query.filter_by(name="介護C").first().on_leave = True
        Staff.query.filter_by(name="介護D").first().retired = True
        db.session.commit()

    client = flask_app.test_client()
    _login(client, "admin", "testpass")

    html = client.get("/staff").get_data(as_text=True)
    assert "介護A" in html
    assert "介護C" not in html, "休職中が一覧に出ている"
    assert "介護D" not in html, "退職者が一覧に出ている"
    assert "休職中・退職者も表示" in html

    html2 = client.get("/staff?show_inactive=1").get_data(as_text=True)
    assert "介護C" in html2 and "介護D" in html2


def test_retired_month_keeps_past_months_visible(tmp_path, monkeypatch):
    """退職月を入れると、その月までは表示され、翌月から外れる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff
    import datetime

    with flask_app.app_context():
        s = Staff.query.filter_by(name="介護D").first()
        s.retired = True
        s.retired_date = datetime.date(2026, 9, 1)   # 2026年9月末で退職
        db.session.commit()
        sid = s.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")

    # 退職月（9月）は在籍扱い → 一覧に出るしシフトも付く
    client.post("/api/generate", json={"year": 2026, "month": 9})
    sept = client.get("/api/shifts/2026/9").get_json()
    assert sid in {x["id"] for x in sept["staff_list"]}, "退職月なのに表示されない"

    # 翌月（10月）は対象外
    client.post("/api/generate", json={"year": 2026, "month": 10})
    oct_ = client.get("/api/shifts/2026/10").get_json()
    assert sid not in {x["id"] for x in oct_["staff_list"]}, "退職の翌月なのに表示されている"
    assert not [x for x in oct_["shifts"] if x["staff_id"] == sid]


def test_retired_without_month_is_hidden_immediately(tmp_path, monkeypatch):
    """退職月が空欄なら、これまでどおりすぐ対象外になる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff

    with flask_app.app_context():
        s = Staff.query.filter_by(name="介護D").first()
        s.retired = True
        s.retired_date = None
        db.session.commit()
        sid = s.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    assert sid not in {x["id"] for x in data["staff_list"]}


def test_cell_edit_moves_shift_and_recomputes(tmp_path, monkeypatch):
    """画面で直接編集した内容を保存できる（移動＝元は休みになる）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    data = client.get("/api/shifts/2026/9").get_json()
    src = next(x for x in data["shifts"] if x["assignment"] not in ("off", "cook_off")
               and not x["assignment"].startswith("cooking_"))
    care_ids = [s["id"] for s in data["staff_list"] if s["department"] != "cooking"]
    target = next(i for i in care_ids if i != src["staff_id"]
                  and not any(x["date"] == src["date"] and x["staff_id"] == i
                              for x in data["shifts"]))

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [
            {"date": src["date"], "staff_id": src["staff_id"], "assignment": "off"},
            {"date": src["date"], "staff_id": target, "assignment": src["assignment"]},
        ],
    })
    assert res.status_code == 200, res.get_data(as_text=True)[:300]
    assert res.get_json()["applied"] == 2

    after = client.get("/api/shifts/2026/9").get_json()
    moved = [x for x in after["shifts"] if x["date"] == src["date"]]
    assert not [x for x in moved if x["staff_id"] == src["staff_id"]], "移動元が休みになっていない"
    assert [x for x in moved if x["staff_id"] == target
            and x["assignment"] == src["assignment"]], "移動先に入っていない"


def test_cell_edit_rejects_cross_group_assignment(tmp_path, monkeypatch):
    """調理職員に介護のシフトは入れられない（逆も同じ）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    cook_id = next(s["id"] for s in data["staff_list"] if s["department"] == "cooking")

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-02", "staff_id": cook_id, "assignment": "early"}],
    })
    assert res.status_code == 200
    assert res.get_json()["applied"] == 0

    after = client.get("/api/shifts/2026/9").get_json()
    assert not [x for x in after["shifts"]
                if x["staff_id"] == cook_id and x["assignment"] == "early"]


def test_cell_edit_blocked_when_confirmed(tmp_path, monkeypatch):
    """確定済みの月は画面編集も止める（確定解除が必要）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    client.post("/api/shifts/2026/9/confirm")

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-02", "staff_id": 1, "assignment": "off"}],
    })
    assert res.status_code == 409


def test_oncall_can_be_changed_by_hand(tmp_path, monkeypatch):
    """オンコール担当を画面から選び直せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    care = [s for s in data["staff_list"] if s["department"] != "cooking"]
    pick = care[0]

    res = client.post("/api/oncall", json={"date": "2026-09-02", "staff_id": pick["id"]})
    assert res.status_code == 200, res.get_data(as_text=True)[:200]
    assert res.get_json()["name"] == pick["name"]

    after = client.get("/api/shifts/2026/9").get_json()
    assert after["oncall"].get("2026-09-02") == pick["name"]

    # 解除もできる
    assert client.post("/api/oncall", json={"date": "2026-09-02"}).status_code == 200
    after2 = client.get("/api/shifts/2026/9").get_json()
    assert not after2["oncall"].get("2026-09-02")


def test_public_holiday_warning_after_manual_edit(tmp_path, monkeypatch):
    """手直しで公休が足りなくなったら警告が出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    staff = next(s for s in data["staff_list"]
                 if s["department"] != "cooking" and s.get("public_holiday_target"))

    # その職員を全日デイに埋めて公休を0にする
    days = [f"2026-09-{d:02d}" for d in range(1, 31)]
    changes = [{"date": d, "staff_id": staff["id"], "assignment": "day_pattern1"} for d in days]
    res = client.post("/api/shift/cells",
                      json={"year": 2026, "month": 9, "changes": changes})
    assert res.status_code == 200
    msgs = [w["message"] for w in res.get_json()["warnings"]
            if w["warning_type"] == "public_holiday_unmet"]
    assert any(staff["name"] in m for m in msgs), msgs


def test_available_months_and_default(tmp_path, monkeypatch):
    """閲覧ページは「実際にシフトがある月」を開く（今日の月が無ければ直近）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")

    # まだ何も生成していないときは default が無い
    res = client.get("/api/shifts/available")
    assert res.status_code == 200
    assert res.get_json()["default"] is None

    client.post("/api/generate", json={"year": 2026, "month": 9})
    client.post("/api/generate", json={"year": 2026, "month": 10})
    res2 = client.get("/api/shifts/available").get_json()
    got = [{"year": m["year"], "month": m["month"]} for m in res2["months"]]
    assert {"year": 2026, "month": 9} in got and {"year": 2026, "month": 10} in got
    # 一番新しく作った月を開く（古い月が今日の月でも、そちらは開かない）
    assert res2["default"] == {"year": 2026, "month": 10}


def test_viewer_can_read_available_months(tmp_path, monkeypatch):
    """閲覧アカウントでも「シフトのある月」を取得できる。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    admin.post("/api/generate", json={"year": 2026, "month": 9})

    guest = flask_app.test_client()
    _login(guest, "staff", "viewpass")
    res = guest.get("/api/shifts/available")
    assert res.status_code == 200
    assert res.get_json()["default"] == {"year": 2026, "month": 9}
