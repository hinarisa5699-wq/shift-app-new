"""職員本人が出す希望（休み希望・出勤可能日）と、その締め切りのテスト。

ユーザー依頼 2026-09:
  「シフト個人のIDから 出勤不可の日、または出勤可能な日 登録出来るようにして
    シフト作成へ連動したい　登録したら登録日時も出るように　締め切り設定できるようにしたい」

出し方は管理画面と同じ3通り（ユーザー指摘 2026-09:「3パターンあります」）:
  ① 休み希望              … DayOffRequest
  ② 出勤可能日（限定）    … StaffWorkableDate + workable_dates_mode="only"
  ③ 出勤可能日（追加・振替）… StaffWorkableDate + workable_dates_mode="extra"

保存先は管理画面と同じテーブルなので、出した希望はそのままシフト自動作成に効く。
"""
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from test_generate_endpoint import _make_app, _login

YEAR, MONTH = 2026, 10


def _seed_two_staff(flask_app):
    """個人アカウントを持つ職員を2人作る（自分と他人）。"""
    from models import db, Staff
    import app as app_module

    with flask_app.app_context():
        Staff.query.delete()
        db.session.commit()

        def add(name, login_id):
            st = Staff(
                name=name, job_category="caregiver",
                staff_group=app_module._job_category_to_group("caregiver"),
                login_id=login_id,
                login_password_hash=generate_password_hash("pw-" + login_id),
            )
            db.session.add(st)
            db.session.flush()
            return st.id

        ids = {"me": add("前垣茜", "S002"), "other": add("池田友子", "S001")}
        db.session.commit()
        return ids


def _as_staff(flask_app, login_id):
    client = flask_app.test_client()
    assert _login(client, login_id, "pw-" + login_id).status_code in (301, 302)
    return client


def _as_admin(flask_app):
    client = flask_app.test_client()
    assert _login(client, "admin", "testpass").status_code in (301, 302)
    return client


def _set_deadline(flask_app, at):
    """その月の締め切りを直接入れる（at=None なら消す）。"""
    from models import db, RequestDeadline

    with flask_app.app_context():
        RequestDeadline.query.filter_by(year=YEAR, month=MONTH).delete()
        if at is not None:
            db.session.add(RequestDeadline(year=YEAR, month=MONTH, deadline_at=at))
        db.session.commit()


# ---------------------------------------------------------------------------
# ① 休み希望
# ---------------------------------------------------------------------------
def test_staff_can_register_day_off_with_timestamp(tmp_path, monkeypatch):
    """個人アカウントから休み希望を出せて、登録日時と「本人」が残る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert [d["date"] for d in j["day_offs"]] == ["2026-10-05"]
    # 登録日時が入っていて、入れたのは本人
    assert j["day_offs"][0]["created_at"], "登録日時が記録されていない"
    assert j["day_offs"][0]["created_by"] == "staff"

    # DBにも自分のぶんだけ入っている（保存先は管理画面と同じテーブル）
    from models import DayOffRequest
    with flask_app.app_context():
        rows = DayOffRequest.query.all()
        assert len(rows) == 1
        assert rows[0].staff_id == ids["me"]
        assert rows[0].date == date(2026, 10, 5)


def test_staff_can_remove_own_day_off(tmp_path, monkeypatch):
    """自分で出した休み希望は自分で消せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    client.post("/api/my-shift-requests",
                json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-05", "action": "remove"})
    assert r.status_code == 200
    assert r.get_json()["day_offs"] == []


def test_same_date_twice_does_not_duplicate(tmp_path, monkeypatch):
    """同じ日を2回押しても二重に入らない（画面の二度押し対策）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    for _ in range(2):
        r = client.post("/api/my-shift-requests",
                        json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
        assert r.status_code == 200
    assert len(r.get_json()["day_offs"]) == 1


# ---------------------------------------------------------------------------
# ②③ 出勤可能日と、その扱い（限定／追加・振替）
# ---------------------------------------------------------------------------
def test_staff_can_register_workable_date_and_mode(tmp_path, monkeypatch):
    """出勤できる日を出せて、扱い（限定／追加・振替）も自分で選べる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    r = client.post("/api/my-shift-requests",
                    json={"kind": "workable", "date": "2026-10-11", "action": "add"})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert [d["date"] for d in j["workable_dates"]] == ["2026-10-11"]
    assert j["workable_dates"][0]["created_by"] == "staff"
    assert j["mode"] == "only"          # 既定は「この日しか出勤しない」

    r = client.post("/api/my-shift-requests/mode",
                    json={"mode": "extra", "year": YEAR, "month": MONTH})
    assert r.status_code == 200
    assert r.get_json()["mode"] == "extra"

    from models import Staff
    with flask_app.app_context():
        assert Staff.query.get(ids["me"]).workable_dates_mode == "extra"


def test_requests_are_split_by_month(tmp_path, monkeypatch):
    """画面に出るのはその月のぶんだけ（別の月の希望は混ざらない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    client.post("/api/my-shift-requests",
                json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    client.post("/api/my-shift-requests",
                json={"kind": "dayoff", "date": "2026-11-05", "action": "add"})

    j = client.get(f"/api/my-shift-requests/{YEAR}/{MONTH}").get_json()
    assert [d["date"] for d in j["day_offs"]] == ["2026-10-05"]
    j11 = client.get(f"/api/my-shift-requests/{YEAR}/11").get_json()
    assert [d["date"] for d in j11["day_offs"]] == ["2026-11-05"]


# ---------------------------------------------------------------------------
# シフト作成への連動
# ---------------------------------------------------------------------------
def test_staff_request_reaches_shift_generation(tmp_path, monkeypatch):
    """本人が出した希望が、そのままシフト生成の入力に入る。

    休み希望は DayOffRequest、出勤可能日は StaffWorkableDate と、
    管理画面から入れたときと同じテーブルに入るので連動する。
    """
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    client.post("/api/my-shift-requests",
                json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    client.post("/api/my-shift-requests",
                json={"kind": "workable", "date": "2026-10-11", "action": "add"})

    from models import DayOffRequest, StaffWorkableDate
    with flask_app.app_context():
        offs = [(r.staff_id, r.date.isoformat()) for r in DayOffRequest.query.all()]
        works = [(r.staff_id, r.date.isoformat()) for r in StaffWorkableDate.query.all()]
    assert offs == [(ids["me"], "2026-10-05")]
    assert works == [(ids["me"], "2026-10-11")]


# ---------------------------------------------------------------------------
# 他人の希望は触れない
# ---------------------------------------------------------------------------
def test_staff_cannot_touch_other_staff_requests(tmp_path, monkeypatch):
    """他人の希望は出せない（staff_id は必ずログイン中の本人から取る）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    # 他人のIDを本文に混ぜても、自分のぶんとして入る
    client.post("/api/my-shift-requests", json={
        "kind": "dayoff", "date": "2026-10-05", "action": "add",
        "staff_id": ids["other"],
    })
    from models import DayOffRequest
    with flask_app.app_context():
        rows = DayOffRequest.query.all()
        assert [r.staff_id for r in rows] == [ids["me"]]


def test_shared_viewer_account_cannot_register(tmp_path, monkeypatch):
    """共通の閲覧アカウント（誰の本人でもない）は希望を出せない。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = flask_app.test_client()
    assert _login(client, "staff", "viewpass").status_code in (301, 302)

    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 締め切り
# ---------------------------------------------------------------------------
def test_staff_blocked_after_deadline(tmp_path, monkeypatch):
    """締め切りを過ぎたら、職員は自分の画面から足すことも消すこともできない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    # 締め切り前に1件出しておく
    client.post("/api/my-shift-requests",
                json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})

    import app as app_module
    _set_deadline(flask_app, app_module._now_jst() - timedelta(minutes=1))

    j = client.get(f"/api/my-shift-requests/{YEAR}/{MONTH}").get_json()
    assert j["closed"] is True
    assert j["deadline"]

    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-06", "action": "add"})
    assert r.status_code == 409
    assert "締め切り" in r.get_json()["error"]

    # 既に出したぶんも、締め切り後は自分では消せない
    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-05", "action": "remove"})
    assert r.status_code == 409

    # 扱いの切り替えも止まる（出した希望の意味が後から変わらないように）
    r = client.post("/api/my-shift-requests/mode",
                    json={"mode": "extra", "year": YEAR, "month": MONTH})
    assert r.status_code == 409

    from models import DayOffRequest
    with flask_app.app_context():
        assert DayOffRequest.query.count() == 1


def test_staff_can_register_before_deadline(tmp_path, monkeypatch):
    """締め切り前なら普通に出せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    import app as app_module
    _set_deadline(flask_app, app_module._now_jst() + timedelta(days=1))

    client = _as_staff(flask_app, "S002")
    j = client.get(f"/api/my-shift-requests/{YEAR}/{MONTH}").get_json()
    assert j["closed"] is False
    r = client.post("/api/my-shift-requests",
                    json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    assert r.status_code == 200


def test_deadline_only_blocks_its_own_month(tmp_path, monkeypatch):
    """10月分を締め切っても、11月分はまだ出せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    import app as app_module
    _set_deadline(flask_app, app_module._now_jst() - timedelta(minutes=1))

    client = _as_staff(flask_app, "S002")
    assert client.post("/api/my-shift-requests", json={
        "kind": "dayoff", "date": "2026-10-05", "action": "add"}).status_code == 409
    assert client.post("/api/my-shift-requests", json={
        "kind": "dayoff", "date": "2026-11-05", "action": "add"}).status_code == 200


def test_admin_can_still_edit_after_deadline(tmp_path, monkeypatch):
    """締め切り後も、管理側の画面からはいつでも入れられる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)
    import app as app_module
    _set_deadline(flask_app, app_module._now_jst() - timedelta(minutes=1))

    admin = _as_admin(flask_app)
    r = admin.post(f"/api/staff/{ids['me']}/dayoff", json={"date": "2026-10-07"})
    assert r.status_code == 201, r.get_json()
    # 管理側から入れたものは「事務所」として残る
    assert r.get_json()["created_by"] == "admin"
    assert r.get_json()["created_at"]


def test_admin_sets_and_clears_deadline(tmp_path, monkeypatch):
    """管理画面から締め切りを決められる・なしに戻せる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    admin = _as_admin(flask_app)

    r = admin.post(f"/api/request-deadline/{YEAR}/{MONTH}",
                   json={"deadline": "2026-09-20T17:00"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["deadline"] == "2026/09/20 17:00"

    j = admin.get(f"/api/request-deadline/{YEAR}/{MONTH}").get_json()
    assert j["deadline_input"] == "2026-09-20T17:00"

    r = admin.post(f"/api/request-deadline/{YEAR}/{MONTH}", json={"deadline": ""})
    assert r.status_code == 200
    assert r.get_json()["deadline"] == ""
    from models import RequestDeadline
    with flask_app.app_context():
        assert RequestDeadline.query.count() == 0


def test_staff_cannot_set_deadline(tmp_path, monkeypatch):
    """職員アカウントは締め切りを触れない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    r = client.post(f"/api/request-deadline/{YEAR}/{MONTH}",
                    json={"deadline": "2026-09-20T17:00"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 管理画面の受付状況
# ---------------------------------------------------------------------------
def test_admin_sees_submission_status(tmp_path, monkeypatch):
    """管理画面で、誰が何件出したか・最後の登録日時が分かる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)

    staff = _as_staff(flask_app, "S002")
    staff.post("/api/my-shift-requests",
               json={"kind": "dayoff", "date": "2026-10-05", "action": "add"})
    staff.post("/api/my-shift-requests",
               json={"kind": "workable", "date": "2026-10-11", "action": "add"})

    admin = _as_admin(flask_app)
    j = admin.get(f"/api/request-deadline/{YEAR}/{MONTH}").get_json()
    rows = {r["staff_id"]: r for r in j["staff"]}

    mine = rows[ids["me"]]
    assert mine["day_off_count"] == 1
    assert mine["workable_count"] == 1
    assert mine["submitted_by_staff"] is True
    assert mine["last_submitted_at"], "最後の登録日時が出ていない"

    other = rows[ids["other"]]
    assert other["day_off_count"] == 0
    assert other["last_submitted_at"] == ""


def test_old_rows_without_timestamp_do_not_break(tmp_path, monkeypatch):
    """この機能より前から入っている行（登録日時なし）でも画面が壊れない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_two_staff(flask_app)

    from models import db, DayOffRequest
    with flask_app.app_context():
        db.session.add(DayOffRequest(staff_id=ids["me"], date=date(YEAR, MONTH, 3)))
        db.session.commit()

    client = _as_staff(flask_app, "S002")
    j = client.get(f"/api/my-shift-requests/{YEAR}/{MONTH}").get_json()
    assert j["day_offs"][0]["created_at"] == ""

    admin = _as_admin(flask_app)
    j = admin.get(f"/api/request-deadline/{YEAR}/{MONTH}").get_json()
    row = next(r for r in j["staff"] if r["staff_id"] == ids["me"])
    assert row["day_off_count"] == 1
    assert row["last_submitted_at"] == ""


# ---------------------------------------------------------------------------
# 画面が出るか
# ---------------------------------------------------------------------------
def test_view_page_shows_request_panel_for_own_account(tmp_path, monkeypatch):
    """個人アカウントの /view に「シフトの希望を出す」欄が出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = _as_staff(flask_app, "S002")

    html = client.get("/view").get_data(as_text=True)
    assert "シフトの希望を出す" in html
    assert "req-off-add" in html and "req-work-add" in html
    assert "const REQ_ON = true" in html
    # 希望はたいてい翌月分なので、月を切り替えるボタンを置いている
    assert "req-next-month" in html


def test_view_page_hides_request_panel_for_shared_account(tmp_path, monkeypatch):
    """共通の閲覧アカウントには希望の欄を出さない（誰の希望か決まらないため）。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    client = flask_app.test_client()
    assert _login(client, "staff", "viewpass").status_code in (301, 302)

    html = client.get("/view").get_data(as_text=True)
    assert "シフトの希望を出す" not in html
    assert "const REQ_ON = false" in html


def test_calendar_page_shows_deadline_panel(tmp_path, monkeypatch):
    """管理画面に「希望の受付」パネルと締め切りの入力欄が出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_two_staff(flask_app)
    admin = _as_admin(flask_app)

    html = admin.get("/calendar").get_data(as_text=True)
    assert "希望の受付（休み希望・出勤可能日）" in html
    assert "req-deadline-input" in html


def test_submitted_day_off_is_honored_by_generation(tmp_path, monkeypatch):
    """本人が出した休み希望どおりに、その日が休みでシフトが組まれる。

    「シフト作成へ連動したい」の一番大事なところなので、実際に生成して確かめる。
    """
    from test_generate_endpoint import _seed
    from models import db, Staff, GeneratedShift

    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)

    with flask_app.app_context():
        st = Staff.query.filter_by(name="介護B").first()
        st.login_id = "S050"
        st.login_password_hash = generate_password_hash("pw-S050")
        db.session.commit()
        sid = st.id

    # 本人が自分のログインから休み希望を出す
    staff = _as_staff(flask_app, "S050")
    r = staff.post("/api/my-shift-requests",
                   json={"kind": "dayoff", "date": "2026-09-02", "action": "add"})
    assert r.status_code == 200, r.get_json()

    # 事務所がシフトを作る
    admin = _as_admin(flask_app)
    gen = admin.post("/api/generate", json={"year": 2026, "month": 9})
    assert gen.status_code == 200, gen.get_json()

    # その日に勤務が入っていない＝休み希望が通っている
    with flask_app.app_context():
        worked = GeneratedShift.query.filter_by(
            staff_id=sid, date=date(2026, 9, 2)).all()
        assert all((g.assignment or "") in ("", "off", "cook_off") for g in worked),             [g.assignment for g in worked]

    # 画面にも「希望休」として出る
    j = admin.get("/api/shifts/2026/9").get_json()
    assert "2026-09-02" in (j.get("day_off_requests") or {})
