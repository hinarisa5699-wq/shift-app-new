"""Googleカレンダー（限定公開URL）の取り込み。

ユーザー依頼 2026-08:「前垣茜のみGoogleカレンダー連動して。★がついてるものは私用で変換」
（★の字はその後「⭐︎ この星でお願い」と指定あり＝絵文字の星も同じ扱い）。

- 対象は名前ではなく「職員マスタにURLを入れた職員」だけ。
- ⭐ / ★ 付きの予定は中身を出さずに「私用」へ。
- 何度取り込んでも増えない。手入力の予定は消えない。
"""
import datetime
import importlib

import pytest

import gcal


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


def _ics(*events):
    body = "".join(events)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//JP\r\n"
        + body + "END:VCALENDAR\r\n"
    )


def _event(uid, dtstart, dtend, summary, extra=""):
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTART{dtstart}\r\n"
        f"DTEND{dtend}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"{extra}"
        "END:VEVENT\r\n"
    )


FIRST = datetime.date(2026, 9, 1)
LAST = datetime.date(2026, 9, 30)


# ---------------------------------------------------------------- URLの検証
def test_url_must_be_google_ics():
    ok = "https://calendar.google.com/calendar/ical/abc%40group.calendar.google.com/private-xyz/basic.ics"
    assert gcal.normalize_ics_url(ok) == ok
    assert gcal.normalize_ics_url("  " + ok + "  ") == ok
    assert gcal.normalize_ics_url("") == ""
    # webcal:// で始まるリンクも受け取る
    assert gcal.normalize_ics_url("webcal://calendar.google.com/x/basic.ics").startswith("https://")

    for bad in ("http://calendar.google.com/x/basic.ics",
                "https://example.com/x/basic.ics",
                "https://calendar.google.com/calendar/r"):
        with pytest.raises(gcal.GoogleCalendarError):
            gcal.normalize_ics_url(bad)


# ---------------------------------------------------------------- ★→私用
def test_star_titles_become_private():
    text = _ics(
        _event("a", ";TZID=Asia/Tokyo:20260903T090000",
               ";TZID=Asia/Tokyo:20260903T100000", "デイ面接"),
        _event("b", ";TZID=Asia/Tokyo:20260903T140000",
               ";TZID=Asia/Tokyo:20260903T150000", "⭐通院"),
        _event("c", ";TZID=Asia/Tokyo:20260904T140000",
               ";TZID=Asia/Tokyo:20260904T150000", "★美容院"),
    )
    rows = gcal.extract_plans(text, FIRST, LAST)
    titles = [(r["date"].day, r["start_time"], r["title"]) for r in rows]
    assert titles == [
        (3, "09:00", "デイ面接"),
        (3, "14:00", "私用"),
        (4, "14:00", "私用"),
    ]


def test_all_day_and_multi_day_events():
    text = _ics(
        # 終日1日（DTENDは翌日＝排他）
        _event("d", ";VALUE=DATE:20260907", ";VALUE=DATE:20260908", "研修"),
        # 終日2日間
        _event("e", ";VALUE=DATE:20260910", ";VALUE=DATE:20260912", "⭐旅行"),
    )
    rows = gcal.extract_plans(text, FIRST, LAST)
    got = [(r["date"].day, r["start_time"], r["end_time"], r["title"]) for r in rows]
    assert got == [
        (7, "", "", "研修"),
        (10, "", "", "私用"),
        (11, "", "", "私用"),
    ]


def test_recurring_event_is_expanded_and_exdate_skipped():
    text = _ics(
        _event("f", ";TZID=Asia/Tokyo:20260901T190000",
               ";TZID=Asia/Tokyo:20260901T200000", "ヨガ",
               extra=("RRULE:FREQ=WEEKLY;BYDAY=TU\r\n"
                      "EXDATE;TZID=Asia/Tokyo:20260915T190000\r\n")),
    )
    rows = gcal.extract_plans(text, FIRST, LAST)
    days = [r["date"].day for r in rows]
    # 2026年9月の火曜は 1/8/15/22/29。15日は EXDATE で除く
    assert days == [1, 8, 22, 29]
    assert all(r["title"] == "ヨガ" and r["start_time"] == "19:00" for r in rows)


def test_cancelled_event_is_skipped():
    text = _ics(
        _event("g", ";TZID=Asia/Tokyo:20260903T090000",
               ";TZID=Asia/Tokyo:20260903T100000", "取り消した予定",
               extra="STATUS:CANCELLED\r\n"),
    )
    assert gcal.extract_plans(text, FIRST, LAST) == []


def test_events_outside_the_month_are_ignored():
    text = _ics(
        _event("h", ";TZID=Asia/Tokyo:20260831T090000",
               ";TZID=Asia/Tokyo:20260831T100000", "前月"),
        _event("i", ";TZID=Asia/Tokyo:20261001T090000",
               ";TZID=Asia/Tokyo:20261001T100000", "翌月"),
    )
    assert gcal.extract_plans(text, FIRST, LAST) == []


def test_utc_times_are_shown_in_japan_time():
    # 09:00Z = 日本時間 18:00
    text = _ics(_event("j", ":20260903T090000Z", ":20260903T100000Z", "会議"))
    rows = gcal.extract_plans(text, FIRST, LAST)
    assert [(r["start_time"], r["end_time"]) for r in rows] == [("18:00", "19:00")]


# ---------------------------------------------------------------- 取り込みAPI
def _make_staff(app_module, flask_app, url=""):
    from models import db, Staff
    with flask_app.app_context():
        st = Staff(
            name="前垣茜", employment_type="常勤", staff_group="care",
            can_visit=False, max_consecutive_days=5, available_days="0,1,2,3,4",
            google_ics_url=url,
        )
        db.session.add(st)
        db.session.commit()
        return st.id


def _login_admin(client, app_module):
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"


def test_import_only_for_staff_with_url(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    staff_id = _make_staff(app_module, flask_app, url="")
    client = flask_app.test_client()
    _login_admin(client, app_module)

    res = client.post("/api/staff-plans/google-import",
                      json={"staff_id": staff_id, "year": 2026, "month": 9})
    assert res.status_code == 400
    assert "登録されていません" in res.get_json()["error"]


def test_import_is_idempotent_and_keeps_manual_plans(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    url = ("https://calendar.google.com/calendar/ical/x%40group.calendar.google.com"
           "/private-abc/basic.ics")
    staff_id = _make_staff(app_module, flask_app, url=url)

    text = _ics(
        _event("a", ";TZID=Asia/Tokyo:20260903T090000",
               ";TZID=Asia/Tokyo:20260903T100000", "デイ面接"),
        _event("b", ";TZID=Asia/Tokyo:20260903T140000",
               ";TZID=Asia/Tokyo:20260903T150000", "⭐通院"),
    )
    monkeypatch.setattr(gcal, "fetch_ics", lambda _url, timeout=20: text)

    from models import db, StaffPlan
    with flask_app.app_context():
        db.session.add(StaffPlan(
            staff_id=staff_id, date=datetime.date(2026, 9, 3),
            start_time="17:00", end_time="18:00", title="手入力の予定",
            display_order=0, source="manual",
        ))
        db.session.commit()

    client = flask_app.test_client()
    _login_admin(client, app_module)
    body = {"staff_id": staff_id, "year": 2026, "month": 9}

    res = client.post("/api/staff-plans/google-import", json=body)
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["imported"] == 2
    assert res.get_json()["private"] == 1

    # 2回押しても増えない
    res = client.post("/api/staff-plans/google-import", json=body)
    assert res.status_code == 200
    assert res.get_json()["imported"] == 2

    with flask_app.app_context():
        rows = StaffPlan.query.filter_by(
            staff_id=staff_id, date=datetime.date(2026, 9, 3)
        ).order_by(StaffPlan.source, StaffPlan.start_time).all()
        assert [(r.source, r.title) for r in rows] == [
            ("google", "デイ面接"),
            ("google", "私用"),
            ("manual", "手入力の予定"),      # 手入力は消えない
        ]


def test_staff_form_saves_and_validates_the_url(tmp_path, monkeypatch):
    """職員の編集画面でURLを保存できる／おかしいURLは弾いて元の値を残す。"""
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    staff_id = _make_staff(app_module, flask_app, url="")
    client = flask_app.test_client()
    _login_admin(client, app_module)

    from models import Staff

    good = ("https://calendar.google.com/calendar/ical/x%40group.calendar.google.com"
            "/private-abc/basic.ics")
    form = {
        "name": "前垣茜", "employment_type": "常勤", "job_category": "caregiver",
        "available_days": "0", "max_consecutive_days": "5",
        "google_ics_url": good,
    }
    res = client.post(f"/api/staff/{staff_id}", data=form, follow_redirects=False)
    assert res.status_code in (301, 302), res.get_data(as_text=True)
    with flask_app.app_context():
        assert Staff.query.get(staff_id).google_ics_url == good

    # Google以外のURLは保存せず、前の値を残す
    res = client.post(f"/api/staff/{staff_id}",
                      data=dict(form, google_ics_url="https://example.com/x.ics"),
                      follow_redirects=False)
    assert res.status_code in (301, 302)
    with flask_app.app_context():
        assert Staff.query.get(staff_id).google_ics_url == good

    # 空欄にすれば連携解除
    res = client.post(f"/api/staff/{staff_id}",
                      data=dict(form, google_ics_url=""), follow_redirects=False)
    assert res.status_code in (301, 302)
    with flask_app.app_context():
        assert Staff.query.get(staff_id).google_ics_url == ""


def test_manual_save_does_not_delete_google_plans(tmp_path, monkeypatch):
    app_module, flask_app = _load_app(tmp_path, monkeypatch)
    staff_id = _make_staff(app_module, flask_app, url="")

    from models import db, StaffPlan
    with flask_app.app_context():
        db.session.add(StaffPlan(
            staff_id=staff_id, date=datetime.date(2026, 9, 3),
            start_time="09:00", end_time="10:00", title="私用",
            display_order=0, source="google", external_uid="a",
        ))
        db.session.commit()

    client = flask_app.test_client()
    _login_admin(client, app_module)
    res = client.post("/api/staff-plans", json={
        "staff_id": staff_id, "date": "2026-09-03",
        "items": [{"start": "17:00", "end": "18:00", "title": "本社"}],
    })
    assert res.status_code == 200, res.get_data(as_text=True)

    with flask_app.app_context():
        rows = StaffPlan.query.filter_by(
            staff_id=staff_id, date=datetime.date(2026, 9, 3)
        ).order_by(StaffPlan.source).all()
        assert [(r.source, r.title) for r in rows] == [
            ("google", "私用"), ("manual", "本社"),
        ]
