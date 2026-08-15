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
    """訪問（午前）は割り当てた人にだけ出る（早番には自動で付かない）。"""
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
    # 早番のセルには訪問が付かない（ユーザー依頼 2026-08 のルール変更）
    early_cells = [c for line in text.splitlines() for c in line.split(",")
                   if c.startswith("早番")]
    assert early_cells, "早番が1つも出ていない"
    assert not [c for c in early_cells if "訪問" in c], early_cells[:3]
    # 訪問は訪問の枠で割り当てられている
    assert "訪問午前のみ" in text or "兼務(訪問→デイ)" in text


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


def test_oncall_only_staff_has_no_public_holiday_target(tmp_path, monkeypatch):
    """オンコールのみ当番の職員には公休目標を課さない（誤警告を出さない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff

    with flask_app.app_context():
        s = Staff.query.filter_by(name="介護D").first()
        s.oncall_only = True
        s.has_phone_duty = True
        db.session.commit()
        sid = s.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    target = next(x["public_holiday_target"] for x in data["staff_list"] if x["id"] == sid)
    assert target == 0, "オンコールのみ職員に公休目標が付いている"

    res = client.post("/api/shift/cells", json={"year": 2026, "month": 9, "changes": []})
    msgs = [w["message"] for w in res.get_json()["warnings"]
            if w["warning_type"] == "public_holiday_unmet"]
    assert not [m for m in msgs if "介護D" in m], msgs


def test_early_and_am_visit_can_be_separated(tmp_path, monkeypatch):
    """早番と訪問（午前）を別の職員に分けられる（表示・集計も分かれる）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, ShiftSettings

    with flask_app.app_context():
        st = ShiftSettings.query.first()
        st.min_visit_am = 1
        db.session.commit()

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    gen = client.post("/api/generate", json={"year": 2026, "month": 9}).get_json()
    data = client.get("/api/shifts/2026/9").get_json()

    # 訪問営業日（月）の早番を探す
    early = next(x for x in data["shifts"]
                 if x["assignment"] == "early" and x["date"] in
                 [f"2026-09-{d:02d}" for d in (7, 14, 21, 28)])
    care_ids = [s["id"] for s in data["staff_list"] if s["department"] != "cooking"]
    other = next(i for i in care_ids if i != early["staff_id"]
                 and not any(x["date"] == early["date"] and x["staff_id"] == i
                             for x in data["shifts"]))

    csv_before = client.get(f"/api/export/{gen['generation_id']}/csv").get_data(as_text=True)
    assert "早番7:30-16:30" in csv_before

    # 別の職員に「訪問(午前)＋デイ(午後)」を割り当てる
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": early["date"], "staff_id": other,
                     "assignment": "visit_am_day_p4"}],
    })
    assert res.status_code == 200 and res.get_json()["applied"] == 1

    gen2 = client.get("/api/shifts/2026/9").get_json()["generation_id"]
    csv_after = client.get(f"/api/export/{gen2}/csv").get_data(as_text=True)
    rows = [r for r in csv_after.split("\n") if early["date"].split("-")[2].lstrip("0") + "(" in r]
    # その日の早番からは訪問表記が外れ、兼務(訪問→デイ)側が訪問担当になる
    assert "兼務(訪問→デイ)" in csv_after


def test_palette_has_standalone_visit_shifts(tmp_path, monkeypatch):
    """パレットに「訪問(午前)のみ」「訪問(午後)のみ」があり、割り当てられる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()

    labels = [x["label"] for x in data["palette"]["care"]]
    assert "訪問(午前)のみ" in labels and "訪問(午後)のみ" in labels

    care_id = next(s["id"] for s in data["staff_list"] if s["department"] != "cooking")
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-07", "staff_id": care_id, "assignment": "visit_am"}],
    })
    assert res.status_code == 200 and res.get_json()["applied"] == 1
    after = client.get("/api/shifts/2026/9").get_json()
    assert any(x["date"] == "2026-09-07" and x["staff_id"] == care_id
               and x["assignment"] == "visit_am" for x in after["shifts"])


def test_visit_slot_keeps_original_shift_label(tmp_path, monkeypatch):
    """訪問（午前）を移しても、その人のシフト表示は元のまま＋訪問（午前）が付く。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    gen = client.post("/api/generate", json={"year": 2026, "month": 9}).get_json()
    data = client.get("/api/shifts/2026/9").get_json()

    # デイに入っている職員を1人選ぶ
    target = next(x for x in data["shifts"] if x["assignment"] == "day_pattern1")

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": target["date"], "staff_id": target["staff_id"],
                     "visit_slot": "am"}],
    })
    assert res.status_code == 200 and res.get_json()["applied"] == 1

    after = client.get("/api/shifts/2026/9").get_json()
    row = next(x for x in after["shifts"]
               if x["date"] == target["date"] and x["staff_id"] == target["staff_id"])
    assert row["assignment"] == "day_pattern1", "シフト本体が兼務に置き換わってしまった"
    assert row["visit_slot"] == "am"

    gen2 = after["generation_id"]
    csv_text = client.get(f"/api/export/{gen2}/csv").get_data(as_text=True)
    assert "デイ8:30-17:30 訪問（午前）" in csv_text.replace("\r", ""), \
        "出力で『デイ…＋訪問（午前）』の形になっていない"

    # 解除もできる
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": target["date"], "staff_id": target["staff_id"],
                     "visit_slot": None}],
    })
    after2 = client.get("/api/shifts/2026/9").get_json()
    row2 = next(x for x in after2["shifts"]
                if x["date"] == target["date"] and x["staff_id"] == target["staff_id"])
    assert not row2["visit_slot"]


def test_whitelist_staff_gets_scheduled_on_registered_days(tmp_path, monkeypatch):
    """出勤可能日だけ登録した職員は、その日に入る（0日にならない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff, StaffWorkableDate
    import datetime

    with flask_app.app_context():
        s = Staff.query.filter_by(name="介護D").first()
        s.workable_dates_mode = "only"
        db.session.add(StaffWorkableDate(staff_id=s.id, date=datetime.date(2026, 9, 5)))
        db.session.add(StaffWorkableDate(staff_id=s.id, date=datetime.date(2026, 9, 26)))
        db.session.commit()
        sid = s.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()

    days = sorted(x["date"] for x in data["shifts"]
                  if x["staff_id"] == sid and x["assignment"] != "off")
    assert days == ["2026-09-05", "2026-09-26"], f"登録日に入っていない: {days}"

    target = next(x["public_holiday_target"] for x in data["staff_list"] if x["id"] == sid)
    assert target == 28, f"公休目標が登録日数に合っていない: {target}"


def _add_executive(flask_app, name="役員I"):
    """役員（自動作成の対象外・休みだけ手入力）を1名足す。"""
    from models import db, Staff

    with flask_app.app_context():
        st = Staff(
            name=name, employment_type="常勤", job_category="executive",
            staff_group="care", can_visit=False, max_consecutive_days=5,
            max_days_per_week=5, min_days_per_week=0,
            available_days="0,1,2,3,4,5,6", available_time_slots="full_day",
            gender="male",
        )
        db.session.add(st)
        db.session.commit()
        return st.id


def test_executive_is_not_scheduled_but_listed(tmp_path, monkeypatch):
    """役員は自動作成でシフトが入らないが、名前は一覧に出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    res = client.post("/api/generate", json={"year": 2026, "month": 9})
    assert res.status_code == 200, res.get_data(as_text=True)[:300]

    data = client.get("/api/shifts/2026/9").get_json()
    assert any(s["id"] == exec_id for s in data["staff_list"]), "一覧に名前が出ていない"
    assert not [x for x in data["shifts"] if x["staff_id"] == exec_id], "役員に勤務が入っている"
    # 公休の目標も持たない（公休不足の警告を出さない）
    st = next(s for s in data["staff_list"] if s["id"] == exec_id)
    assert st["public_holiday_target"] == 0


def test_executive_accepts_only_day_off(tmp_path, monkeypatch):
    """役員のセルには「休み」だけ入れられる（他のシフトは入らない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-01", "staff_id": exec_id, "assignment": "exec_off"}],
    })
    assert res.status_code == 200
    assert res.get_json()["applied"] == 1
    after = client.get("/api/shifts/2026/9").get_json()
    assert [x for x in after["shifts"]
            if x["staff_id"] == exec_id and x["assignment"] == "exec_off"]

    # 介護のシフトは受け付けない
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-02", "staff_id": exec_id, "assignment": "early"}],
    })
    assert res.get_json()["applied"] == 0
    after = client.get("/api/shifts/2026/9").get_json()
    assert not [x for x in after["shifts"]
                if x["staff_id"] == exec_id and x["date"] == "2026-09-02"]


def test_executive_day_off_is_not_counted_as_work(tmp_path, monkeypatch):
    """役員の「休み」は出勤日数にも介護の配置人数にも数えない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-01", "staff_id": exec_id, "assignment": "exec_off"}],
    })
    data = client.get("/api/shifts/2026/9").get_json()
    gen = data["generation_id"]
    csv_text = client.get(f"/api/export/{gen}/csv").get_data(as_text=True)
    row = next(l for l in csv_text.split("\n") if l.startswith("役員I")).strip()
    assert row.split(",")[1] == "休", f"休みが入っていない: {row[:40]}"
    assert row.split(",")[-1] == "0", f"出勤日数が0になっていない: {row[-40:]}"
    # 介護職員の休みは「exec_off」を使えない
    care_id = next(s["id"] for s in data["staff_list"]
                   if s["department"] != "cooking" and s["id"] != exec_id)
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-03", "staff_id": care_id, "assignment": "exec_off"}],
    })
    assert res.get_json()["applied"] == 0


def test_cell_edit_can_move_to_another_day(tmp_path, monkeypatch):
    """間違えて入れた日のシフトを、別の日へ移せる（ユーザー依頼 2026-08）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    data = client.get("/api/shifts/2026/9").get_json()
    src = next(x for x in data["shifts"]
               if x["assignment"] not in ("off", "cook_off")
               and not x["assignment"].startswith("cooking_"))
    # 同じ職員の、シフトが入っていない別の日へ移す
    used = {x["date"] for x in data["shifts"] if x["staff_id"] == src["staff_id"]}
    other = next(f"2026-09-{d:02d}" for d in range(1, 31)
                 if f"2026-09-{d:02d}" not in used)

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [
            {"date": src["date"], "staff_id": src["staff_id"], "assignment": "off"},
            {"date": other, "staff_id": src["staff_id"], "assignment": src["assignment"]},
        ],
    })
    assert res.status_code == 200, res.get_data(as_text=True)[:300]
    after = client.get("/api/shifts/2026/9").get_json()
    assert not [x for x in after["shifts"]
                if x["date"] == src["date"] and x["staff_id"] == src["staff_id"]]
    assert [x for x in after["shifts"]
            if x["date"] == other and x["staff_id"] == src["staff_id"]
            and x["assignment"] == src["assignment"]]


def test_executive_plan_can_be_entered_and_shown(tmp_path, monkeypatch):
    """役員の予定（時間つき）を入れて、出力にもそのまま出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [
            {"date": "2026-09-01", "staff_id": exec_id, "assignment": "exec:9:00-12:00 デイ面接"},
            {"date": "2026-09-02", "staff_id": exec_id, "assignment": "exec:在宅勤務"},
        ],
    })
    assert res.status_code == 200
    assert res.get_json()["applied"] == 2

    data = client.get("/api/shifts/2026/9").get_json()
    got = {x["date"]: x["assignment"] for x in data["shifts"] if x["staff_id"] == exec_id}
    assert got["2026-09-01"] == "exec:9:00-12:00 デイ面接"
    assert got["2026-09-02"] == "exec:在宅勤務"

    csv_text = client.get(f"/api/export/{data['generation_id']}/csv").get_data(as_text=True)
    row = next(l for l in csv_text.split("\n") if l.startswith("役員I"))
    assert "9:00-12:00 デイ面接" in row, row[:120]
    assert "在宅勤務" in row


def test_executive_plan_survives_regeneration(tmp_path, monkeypatch):
    """再生成しても役員の予定は残る（手で入れたものなので消さない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-04", "staff_id": exec_id, "assignment": "exec:本部勤務"}],
    })

    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    assert [x for x in data["shifts"]
            if x["staff_id"] == exec_id and x["assignment"] == "exec:本部勤務"]


def test_executive_can_take_oncall(tmp_path, monkeypatch):
    """役員は勤務に入らなくてもオンコール当番は持てる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app, name="役員オンコール")
    from models import db, Staff, ShiftSettings

    with flask_app.app_context():
        st = Staff.query.get(exec_id)
        st.has_phone_duty = True
        # 他の職員は当番を持たない設定にして、役員だけが候補になるようにする
        for other in Staff.query.filter(Staff.id != exec_id).all():
            other.has_phone_duty = False
        settings = ShiftSettings.query.first()
        settings.phone_duty_enabled = True
        settings.oncall_requires_work = True      # 出勤者限定でも役員は例外
        settings.oncall_fairness_mode = "off"
        db.session.commit()

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    res = client.post("/api/generate", json={"year": 2026, "month": 9})
    assert res.status_code == 200, res.get_data(as_text=True)[:300]

    data = client.get("/api/shifts/2026/9").get_json()
    oncall = data.get("oncall", {})
    assert oncall, "オンコールが1日も割り当てられていない"
    assert any(v == "役員オンコール" for v in oncall.values()), oncall
    # 勤務そのものは入っていない
    assert not [x for x in data["shifts"] if x["staff_id"] == exec_id]


def _set_exec_password(flask_app, pw="execpass"):
    from models import db, ShiftSettings
    from werkzeug.security import generate_password_hash

    with flask_app.app_context():
        s = ShiftSettings.query.first()
        s.exec_password_hash = generate_password_hash(pw)
        db.session.commit()


def test_exec_account_logs_in_to_viewer(tmp_path, monkeypatch):
    """役員アカウント（yakuin）は閲覧ページに入る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    _set_exec_password(flask_app)
    client = flask_app.test_client()

    res = _login(client, "yakuin", "execpass")
    assert res.status_code in (301, 302)
    assert "/view" in res.headers.get("Location", "")
    assert client.get("/view").status_code == 200
    assert client.get("/api/shifts/2026/9").status_code == 200

    # 管理系の画面・APIは使えない
    for path in ("/staff", "/settings", "/calendar"):
        assert client.get(path).status_code in (301, 302), path
    assert client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 403


def test_exec_account_can_edit_only_executive_plans(tmp_path, monkeypatch):
    """役員アカウントは役員の予定だけ入れられる（他人のシフトは不可）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    _set_exec_password(flask_app)

    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    admin.post("/api/generate", json={"year": 2026, "month": 9})
    care_id = next(s["id"] for s in admin.get("/api/shifts/2026/9").get_json()["staff_list"]
                   if s["department"] != "cooking" and s["id"] != exec_id)

    client = flask_app.test_client()
    _login(client, "yakuin", "execpass")

    ok = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-05", "staff_id": exec_id, "assignment": "exec:本部勤務"}],
    })
    assert ok.status_code == 200, ok.get_data(as_text=True)[:200]

    # 他の職員のシフトは変えられない
    ng = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-05", "staff_id": care_id, "assignment": "early"}],
    })
    assert ng.status_code == 403

    # 役員に介護のシフトも入れられない
    ng2 = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-06", "staff_id": exec_id, "assignment": "early"}],
    })
    assert ng2.status_code == 403

    after = admin.get("/api/shifts/2026/9").get_json()
    assert [x for x in after["shifts"]
            if x["staff_id"] == exec_id and x["assignment"] == "exec:本部勤務"]


def test_exec_plan_allowed_even_when_confirmed(tmp_path, monkeypatch):
    """確定済みの月でも役員の予定は入れられる（勤務表は変わらないため）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    exec_id = _add_executive(flask_app)
    _set_exec_password(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    admin.post("/api/generate", json={"year": 2026, "month": 9})
    assert admin.post("/api/shifts/2026/9/confirm").status_code == 200

    client = flask_app.test_client()
    _login(client, "yakuin", "execpass")
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-07", "staff_id": exec_id, "assignment": "exec:在宅勤務"}],
    })
    assert res.status_code == 200, res.get_data(as_text=True)[:200]

    # 通常のシフトは確定済みなので変えられない
    care_id = next(s["id"] for s in admin.get("/api/shifts/2026/9").get_json()["staff_list"]
                   if s["department"] != "cooking" and s["id"] != exec_id)
    blocked = admin.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-07", "staff_id": care_id, "assignment": "early"}],
    })
    assert blocked.status_code == 409


def _add_office(flask_app, name="事務J"):
    """事務職員（自動作成の対象外・予定だけ手入力）を1名足す。"""
    from models import db, Staff

    with flask_app.app_context():
        st = Staff(
            name=name, employment_type="常勤", job_category="office",
            staff_group="care", can_visit=False, max_consecutive_days=5,
            max_days_per_week=5, min_days_per_week=0,
            available_days="0,1,2,3,4,5,6", available_time_slots="full_day",
            gender="female",
        )
        db.session.add(st)
        db.session.commit()
        return st.id


def _set_office_password(flask_app, pw="jimupass"):
    from models import db, ShiftSettings
    from werkzeug.security import generate_password_hash

    with flask_app.app_context():
        s = ShiftSettings.query.first()
        s.office_password_hash = generate_password_hash(pw)
        db.session.commit()


def test_office_staff_is_not_scheduled_or_counted(tmp_path, monkeypatch):
    """事務は自動作成に入らず、介護の配置人数にも数えない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    office_id = _add_office(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    assert client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 200

    data = client.get("/api/shifts/2026/9").get_json()
    assert any(s["id"] == office_id for s in data["staff_list"]), "一覧に名前が出ていない"
    assert not [x for x in data["shifts"] if x["staff_id"] == office_id]
    st = next(s for s in data["staff_list"] if s["id"] == office_id)
    assert st["public_holiday_target"] == 0

    # 事務の予定を入れても出勤日数には数えない
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-01", "staff_id": office_id, "assignment": "exec:在宅勤務"}],
    })
    csv_text = client.get(
        f"/api/export/{data['generation_id']}/csv").get_data(as_text=True)
    row = next(l for l in csv_text.splitlines() if l.startswith("事務J")).strip()
    assert row.split(",")[1] == "在宅勤務", row[:60]

    # 介護の配置人数には影響しない（デイ午前の人数が変わらない）
    def _day_am_line(text):
        return next(l for l in text.splitlines() if l.startswith("デイ午前")).strip()

    before_csv = csv_text
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-02", "staff_id": office_id, "assignment": "exec:終日デイ"}],
    })
    after_csv = client.get(
        f"/api/export/{data['generation_id']}/csv").get_data(as_text=True)
    assert _day_am_line(before_csv) == _day_am_line(after_csv)


def test_office_account_edits_only_office_plans(tmp_path, monkeypatch):
    """事務アカウント（jimu）は事務の予定だけ入れられる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    office_id = _add_office(flask_app)
    exec_id = _add_executive(flask_app)
    _set_office_password(flask_app)

    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    admin.post("/api/generate", json={"year": 2026, "month": 9})

    client = flask_app.test_client()
    res = _login(client, "jimu", "jimupass")
    assert res.status_code in (301, 302)
    assert "/view" in res.headers.get("Location", "")

    ok = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-03", "staff_id": office_id, "assignment": "exec:本部勤務"}],
    })
    assert ok.status_code == 200, ok.get_data(as_text=True)[:200]

    # 役員の予定は触れない
    ng = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": "2026-09-03", "staff_id": exec_id, "assignment": "exec:本部勤務"}],
    })
    assert ng.status_code == 403
    assert client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 403


def test_visit_can_be_removed_from_early_shift(tmp_path, monkeypatch):
    """早番に自動で付く「訪問（午前）」をゴミ箱で外せる（visit_slot="none"）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, ShiftSettings

    with flask_app.app_context():
        s = ShiftSettings.query.first()
        s.min_visit_am = 1
        db.session.commit()

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    data = client.get("/api/shifts/2026/9").get_json()
    early = next(x for x in data["shifts"] if x["assignment"] == "early")
    # 早番の人に訪問（午前）を付けてから、ゴミ箱で外す
    client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": early["date"], "staff_id": early["staff_id"],
                     "visit_slot": "am"}],
    })
    mid = client.get("/api/shifts/2026/9").get_json()
    assert [x for x in mid["shifts"] if x["date"] == early["date"]
            and x["staff_id"] == early["staff_id"] and x["visit_slot"] == "am"]

    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": early["date"], "staff_id": early["staff_id"],
                     "visit_slot": "none"}],
    })
    assert res.status_code == 200, res.get_data(as_text=True)[:200]

    after = client.get("/api/shifts/2026/9").get_json()
    row = next(x for x in after["shifts"]
               if x["date"] == early["date"] and x["staff_id"] == early["staff_id"])
    assert row["visit_slot"] == "none"
    assert row["assignment"] == "early", "シフト自体は早番のまま"

    # 出力でもその日その人には訪問が付かない
    csv_after = client.get(
        f"/api/export/{after['generation_id']}/csv").get_data(as_text=True)
    day = int(early["date"].split("-")[2])
    staff_name = next(s["name"] for s in after["staff_list"]
                      if s["id"] == early["staff_id"])
    line = next(l for l in csv_after.splitlines() if l.startswith(staff_name))
    cell = line.split(",")[day]
    assert "早番" in cell and "訪問" not in cell, cell


def test_visit_slot_can_be_added_under_existing_shift(tmp_path, monkeypatch):
    """デイのシフトはそのままで「訪問（午前）」だけを足せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})

    data = client.get("/api/shifts/2026/9").get_json()
    target = next(x for x in data["shifts"] if x["assignment"] == "day_pattern1")
    res = client.post("/api/shift/cells", json={
        "year": 2026, "month": 9,
        "changes": [{"date": target["date"], "staff_id": target["staff_id"],
                     "visit_slot": "am"}],
    })
    assert res.status_code == 200

    after = client.get("/api/shifts/2026/9").get_json()
    row = next(x for x in after["shifts"]
               if x["date"] == target["date"] and x["staff_id"] == target["staff_id"])
    assert row["assignment"] == "day_pattern1", "元のシフトが消えている"
    assert row["visit_slot"] == "am"

    csv_text = client.get(
        f"/api/export/{after['generation_id']}/csv").get_data(as_text=True)
    day = int(target["date"].split("-")[2])
    name = next(s["name"] for s in after["staff_list"] if s["id"] == target["staff_id"])
    line = next(l for l in csv_text.splitlines() if l.startswith(name))
    cell = line.split(",")[day]
    assert "デイ8:30-17:30" in cell and "訪問（午前）" in cell, cell


def test_shifts_api_exposes_can_visit(tmp_path, monkeypatch):
    """手直し画面で訪問NGの職員を弾けるよう、can_visit を返す。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    from models import db, Staff

    with flask_app.app_context():
        st = Staff.query.filter_by(name="介護B").first()
        st.can_visit = False
        db.session.commit()
        no_visit_id = st.id

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/generate", json={"year": 2026, "month": 9})
    data = client.get("/api/shifts/2026/9").get_json()
    by_id = {s["id"]: s for s in data["staff_list"]}
    assert by_id[no_visit_id]["can_visit"] is False
    assert any(s["can_visit"] for s in data["staff_list"]), "訪問可の職員も居るはず"

    # 自動作成でも訪問は割り当てられない
    visit_codes = {"visit_am", "visit_pm", "visit_am_day_p4", "day_p3_visit_pm"}
    assert not [x for x in data["shifts"]
                if x["staff_id"] == no_visit_id and x["assignment"] in visit_codes]
