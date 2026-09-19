"""閲覧アプリ（スマホ）を開いたとき、今日の月が出ることのテスト。

ユーザー依頼 2026-09:
  「スマホアプリ　開いたら今日の自分の予定が表示するようにして
    現在ログインしたら１０月が表示されてしまう」

/api/shifts/available が「いちばん新しい月」を返していたため、9月に開いても
翌月の10月が出ていた。今日の月にシフトがあれば、まずその月を開くようにする。

閲覧画面は返ってきた年月を読み込み、その月が今日の月なら今日を含む週を開く
（view.html の defaultWeekIndex）。だからここで月が正しければ今日の予定が出る。
"""
from datetime import date

from test_generate_endpoint import _make_app, _login


def _seed_months(flask_app, months):
    """(year, month) ごとに1日だけシフトを作る。"""
    from models import db, Staff, GeneratedShift

    with flask_app.app_context():
        Staff.query.delete()
        GeneratedShift.query.delete()
        db.session.commit()

        st = Staff(name="閲覧テスト", employment_type="常勤",
                   job_category="caregiver", staff_group="care")
        db.session.add(st)
        db.session.flush()
        for i, (y, m) in enumerate(months):
            db.session.add(GeneratedShift(
                generation_id=f"gen-{y}-{m}", date=date(y, m, 1),
                staff_id=st.id, assignment="day_pattern1",
            ))
        db.session.commit()


def _as_admin(flask_app):
    client = flask_app.test_client()
    assert _login(client, "admin", "testpass").status_code in (301, 302)
    return client


def _default_of(client):
    res = client.get("/api/shifts/available")
    assert res.status_code == 200
    return res.get_json()["default"]


def _shift_month(y, m, delta):
    total = (y * 12 + (m - 1)) + delta
    return total // 12, total % 12 + 1


def test_today_month_is_opened_when_it_has_shifts(tmp_path, monkeypatch):
    """今日の月と翌月の両方にシフトがあるなら、今日の月を開く。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    today = date.today()
    nxt = _shift_month(today.year, today.month, 1)
    _seed_months(flask_app, [(today.year, today.month), nxt])

    assert _default_of(_as_admin(flask_app)) == {
        "year": today.year, "month": today.month}


def test_next_month_is_opened_when_today_has_no_shifts(tmp_path, monkeypatch):
    """今日の月がまだ作られていなければ、これから先でいちばん近い月を開く。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    today = date.today()
    nxt = _shift_month(today.year, today.month, 1)
    after = _shift_month(today.year, today.month, 2)
    _seed_months(flask_app, [nxt, after])

    assert _default_of(_as_admin(flask_app)) == {"year": nxt[0], "month": nxt[1]}


def test_newest_past_month_is_opened_when_nothing_ahead(tmp_path, monkeypatch):
    """過去の月しか無ければ、いちばん新しい月を開く（従来どおり）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    today = date.today()
    prev1 = _shift_month(today.year, today.month, -1)
    prev2 = _shift_month(today.year, today.month, -2)
    _seed_months(flask_app, [prev2, prev1])

    assert _default_of(_as_admin(flask_app)) == {"year": prev1[0], "month": prev1[1]}


def test_no_shifts_at_all_returns_none(tmp_path, monkeypatch):
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_months(flask_app, [])
    assert _default_of(_as_admin(flask_app)) is None


def test_month_list_still_contains_every_month(tmp_path, monkeypatch):
    """開く月を変えても、選べる月の一覧は今までどおり全部返す。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    today = date.today()
    prev = _shift_month(today.year, today.month, -1)
    nxt = _shift_month(today.year, today.month, 1)
    _seed_months(flask_app, [prev, (today.year, today.month), nxt])

    res = _as_admin(flask_app).get("/api/shifts/available")
    got = {(m["year"], m["month"]) for m in res.get_json()["months"]}
    assert got == {prev, (today.year, today.month), nxt}


def test_view_page_opens_person_mode_for_a_staff_login(tmp_path, monkeypatch):
    """職員としてログインしたら、最初からその人のページが開くこと。"""
    monkeypatch.setenv("SHIFT_STAFF_PASSWORD", "staffpass")
    flask_app = _make_app(tmp_path, monkeypatch)
    today = date.today()
    _seed_months(flask_app, [(today.year, today.month)])

    from werkzeug.security import generate_password_hash

    from models import db, Staff
    with flask_app.app_context():
        st = Staff.query.filter_by(name="閲覧テスト").first()
        st.login_id = "v001"
        st.login_password_hash = generate_password_hash("pw12345")
        db.session.commit()
        staff_id = st.id

    client = flask_app.test_client()
    assert _login(client, "v001", "pw12345").status_code in (301, 302)
    html = client.get("/view").get_data(as_text=True)
    assert str(staff_id) in html          # その人のIDが埋め込まれている
    assert "mode: 'person'" in html       # 最初から本人のページで開く
