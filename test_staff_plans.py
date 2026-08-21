"""個人のスケジュール（手入力・1日に何件でも）のテスト。

ユーザー指摘 2026-08:
  「池田友子（役員兼看護師）池田和男(役員）小倉シモネ(事務）の個人のアカウントで
    スケジュールがブランクで入力できません。修正してください。
    それとスケジュールは追加機能もいれて手入力も可能にしたい。
    例 デイ面接 9時から10時 本社10時から17時 みたいに、何個でもいれていいようにして」

・職員ごとのアカウント（閲覧ロール）でも、自分のスケジュールは入れられる
・1日に何件でも入る。時間は空でもよい（終日の予定）
・他人のスケジュールや自分の勤務そのものは触れない
"""
import uuid
from datetime import date

from werkzeug.security import generate_password_hash

from test_generate_endpoint import _make_app, _login

YEAR, MONTH = 2026, 9


def _seed_plan_staff(flask_app):
    """役員兼看護師・役員・事務・介護職員を1人ずつ、個人アカウント付きで作る。"""
    from models import db, Staff, GeneratedShift
    import app as app_module

    with flask_app.app_context():
        Staff.query.delete()
        db.session.commit()

        def add(name, job_category, login_id):
            st = Staff(
                name=name, job_category=job_category,
                staff_group=app_module._job_category_to_group(job_category),
                login_id=login_id,
                login_password_hash=generate_password_hash("pw-" + login_id),
            )
            db.session.add(st)
            db.session.flush()
            return st.id

        ids = {
            "nurse_exec": add("池田友子", "nurse_rehab", "S001"),
            "exec": add("池田和男", "executive", "S003"),
            "office": add("小倉シモネ", "office", "S004"),
            "care": add("前垣茜", "caregiver", "S002"),
        }
        # /api/shift/cells はその月のシフトが1件も無いと動かないので1件だけ作る
        db.session.add(GeneratedShift(
            generation_id=str(uuid.uuid4()), date=date(YEAR, MONTH, 1),
            staff_id=ids["nurse_exec"], assignment="nurse_short",
        ))
        db.session.commit()
        return ids


def _plans_of(client, staff_id, day):
    j = client.get(f"/api/shifts/{YEAR}/{MONTH}").get_json()
    return ((j.get("plans") or {}).get(day) or {}).get(str(staff_id)) or []


def test_own_account_can_enter_multiple_plans(tmp_path, monkeypatch):
    """個人アカウントで、自分の1日に予定を何件でも入れられる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)
    me = ids["nurse_exec"]

    client = flask_app.test_client()
    assert _login(client, "S001", "pw-S001").status_code in (301, 302)
    assert client.get("/view").status_code == 200

    r = client.post("/api/staff-plans", json={
        "staff_id": me, "date": "2026-09-03",
        "items": [
            {"start": "09:00", "end": "10:00", "title": "デイ面接"},
            {"start": "10:00", "end": "17:00", "title": "本社"},
            {"start": "", "end": "", "title": "研修"},
        ],
    })
    assert r.status_code == 200

    got = _plans_of(client, me, "2026-09-03")
    assert [p["label"] for p in got] == [
        "9:00-10:00 デイ面接", "10:00-17:00 本社", "研修",
    ]

    # 空で送るとその日の予定が消える
    client.post("/api/staff-plans", json={
        "staff_id": me, "date": "2026-09-03", "items": []})
    assert _plans_of(client, me, "2026-09-03") == []


def test_typed_times_are_accepted(tmp_path, monkeypatch):
    """時刻ピッカー以外（"9時" や全角）で入れても揃えて保存する。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "S004", "pw-S004")
    r = client.post("/api/staff-plans", json={
        "staff_id": ids["office"], "date": "2026-09-07",
        "items": [{"start": "9時", "end": "１７：００", "title": " 本社  勤務 "}],
    })
    assert r.status_code == 200
    got = _plans_of(client, ids["office"], "2026-09-07")
    assert got[0]["label"] == "9:00-17:00 本社 勤務"


def test_cannot_touch_other_staff_or_own_shift(tmp_path, monkeypatch):
    """個人アカウントは他人の予定も、自分の勤務そのものも変えられない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "S001", "pw-S001")

    r = client.post("/api/staff-plans", json={
        "staff_id": ids["exec"], "date": "2026-09-03",
        "items": [{"start": "", "end": "", "title": "いたずら"}]})
    assert r.status_code == 403

    # 看護師なので自分の勤務セルは消せない
    r = client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "2026-09-01", "staff_id": ids["nurse_exec"],
                     "assignment": ""}]})
    assert r.status_code == 403


def test_executive_account_can_set_own_day_off(tmp_path, monkeypatch):
    """役員・事務の個人アカウントは、自分の勤務欄を「終日休み」にできる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "S003", "pw-S003")

    r = client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "2026-09-05", "staff_id": ids["exec"],
                     "assignment": "exec_off"}]})
    assert r.status_code == 200

    # 他人の勤務欄は変えられない
    r = client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "2026-09-05", "staff_id": ids["care"],
                     "assignment": "exec_off"}]})
    assert r.status_code == 403


def test_shared_viewer_account_cannot_enter_plans(tmp_path, monkeypatch):
    """共通の閲覧アカウント(staff)は誰の予定も入れられない。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "viewpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "staff", "viewpass")
    r = client.post("/api/staff-plans", json={
        "staff_id": ids["exec"], "date": "2026-09-09",
        "items": [{"start": "", "end": "", "title": "だめ"}]})
    assert r.status_code == 403


def test_admin_can_enter_plans_for_anyone(tmp_path, monkeypatch):
    """管理者は誰の予定でも入れられる。1日20件が上限。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "admin", "testpass")

    r = client.post("/api/staff-plans", json={
        "staff_id": ids["care"], "date": "2026-09-11",
        "items": [{"start": "", "end": "", "title": "面談"}]})
    assert r.status_code == 200

    r = client.post("/api/staff-plans", json={
        "staff_id": ids["care"], "date": "2026-09-11",
        "items": [{"start": "", "end": "", "title": "x"}] * 21})
    assert r.status_code == 400


def test_deleting_staff_removes_their_plans(tmp_path, monkeypatch):
    """職員を消すと、その人の予定も残らない。"""
    from models import db, Staff, StaffPlan

    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed_plan_staff(flask_app)

    client = flask_app.test_client()
    _login(client, "admin", "testpass")
    client.post("/api/staff-plans", json={
        "staff_id": ids["care"], "date": "2026-09-12",
        "items": [{"start": "", "end": "", "title": "面談"}]})

    with flask_app.app_context():
        assert StaffPlan.query.filter_by(staff_id=ids["care"]).count() == 1
        db.session.delete(Staff.query.get(ids["care"]))
        db.session.commit()
        assert StaffPlan.query.filter_by(staff_id=ids["care"]).count() == 0


def test_cooking_patterns_9_to_16_and_9_to_19_exist(tmp_path, monkeypatch):
    """調理シフトに 9:00-16:00 と 9:00-19:00 の種類がある。

    ユーザー依頼 2026-08:「調理のシフト 9時から16時 項目追加」
    「調理9時から19時も追加」。
    コード番号ではなく時間帯で保証するので、番号がずれている環境でも入る。
    """
    from models import ShiftPattern

    flask_app = _make_app(tmp_path, monkeypatch)
    with flask_app.app_context():
        for start, end in (("09:00", "16:00"), ("09:00", "19:00")):
            rows = ShiftPattern.query.filter_by(
                staff_group="cooking", start_time=start, end_time=end).all()
            assert len(rows) == 1, (start, end)
            assert "{}-{}".format(start.lstrip("0"), end) in rows[0].label
