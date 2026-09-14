"""公休日数の警告と看護師配置の回帰テスト（ユーザー指摘 2026-09）。

1. 休み希望をたくさん出した職員に「公休日数が目標とずれている」と警告しない。
   （竹下さん: 公休15日と入れてあるが休み希望17日＋固定休 金土日で、
     どう組んでも公休22日にしかならない。これを「差+7日の未達」と出さない）
2. 公休日数を入れていないパートは、目標のズレを警告しない（応援職員は目標なし）。
3. デイ営業日に看護師を1人も置かない日を作らない。
4. 出勤可能日は「生成する月に登録された日」だけを見る（先月の登録で翌月が全休に
   ならない）。
"""
import datetime
import importlib


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_APP_DB_PATH", str(tmp_path / "ph.db"))
    monkeypatch.setenv("SHIFT_ADMIN_PASSWORD", "testpass")
    import config as config_module
    import app as app_module

    importlib.reload(config_module)
    app_module = importlib.reload(app_module)
    flask_app = app_module.create_app()
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _seed(flask_app):
    """2026年10月（31日・日曜4回）を想定した最小構成。"""
    from models import db, Staff, ShiftSettings, Qualification, StaffQualification

    with flask_app.app_context():
        Staff.query.delete()
        db.session.commit()

        def add(name, jc, emp, avail, fixed="", ph=0, backup=False, maxw=5):
            st = Staff(
                name=name, employment_type=emp, job_category=jc,
                staff_group=("cooking" if jc == "cooking" else "care"),
                can_visit=True, max_consecutive_days=5, max_days_per_week=maxw,
                min_days_per_week=0, available_days=avail,
                available_time_slots="full_day", fixed_days_off=fixed,
                required_days="", gender="female",
                public_holiday_count=ph, backup_only=backup,
            )
            db.session.add(st)
            db.session.flush()
            return st.id

        ids = {}
        ids["careA"] = add("介護A", "caregiver", "常勤", "0,1,2,3,4,5,6")
        ids["careB"] = add("介護B", "caregiver", "パート", "0,1,2,3,4,5,6")
        ids["careC"] = add("介護C", "caregiver", "パート", "0,1,2,3,4,5,6")
        ids["help"] = add("応援ヘルプ", "caregiver", "パート",
                          "0,1,2,3,4,5,6", backup=True)
        # 看護は2人。看護Xは火木のみ＝水曜は看護Yしか出られない
        ids["nurseX"] = add("看護X", "nurse_rehab", "パート", "1,3", maxw=3)
        ids["nurseY"] = add("看護Y", "nurse_rehab", "常勤", "0,1,2,3,4,5,6")
        # 調理: 竹下さん役（固定休 金土日・公休15日・休み希望17日）
        ids["cookT"] = add("調理T", "cooking", "パート", "0,1,2,3,4,5,6",
                           fixed="4,5,6", ph=15)
        ids["cookU"] = add("調理U", "cooking", "パート", "0,1,2,3,4,5,6")

        q = Qualification.query.filter_by(name="看護師").first()
        if q is None:
            q = Qualification(name="看護師")
            db.session.add(q)
            db.session.flush()
        for key in ("nurseX", "nurseY"):
            db.session.add(StaffQualification(staff_id=ids[key], qualification_id=q.id))

        s = ShiftSettings.query.first() or ShiftSettings()
        s.min_visit_am = 0
        s.min_visit_pm = 0
        s.closed_days = "6"                 # 日曜休業
        s.day_service_operating_days = "1,2,3"   # 火水木がデイ
        s.floor3_day_service_days = "1,2,3"
        s.floor2_day_service_days = "1,2,3"
        s.floor3_visit_days = "0,4"
        s.floor2_visit_days = "0,4"
        s.visit_operating_days = "0,4"
        s.no_day_service_days = "0,4,5,6"
        s.care_min_by_weekday = "2,2,2,2,2,2,0"
        s.care_max_by_weekday = "3,3,3,3,3,3,0"
        s.min_staff_at_9 = 1
        s.min_staff_at_15 = 1
        s.auto_public_holidays = True
        s.min_cooking_staff = 1
        s.min_cooking_overlap = 0
        db.session.add(s)
        db.session.commit()
        return ids


def _login(client):
    client.post("/login", data={"username": "admin", "password": "testpass"},
                follow_redirects=True)


def _add_dayoffs(flask_app, staff_id, days):
    from models import db, DayOffRequest

    with flask_app.app_context():
        for d in days:
            db.session.add(DayOffRequest(
                staff_id=staff_id, date=datetime.date(2026, 10, d)))
        db.session.commit()


def test_heavy_dayoff_requests_do_not_trigger_public_holiday_warning(
        tmp_path, monkeypatch):
    """休み希望と固定休で公休が目標を超える月は、公休日数の警告を出さない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed(flask_app)
    # 固定休が金土日。そのうえ月曜と水曜を全部休み希望に出す＝火木しか出られない
    _add_dayoffs(flask_app, ids["cookT"],
                 [5, 12, 19, 26, 7, 14, 21, 28, 3, 4, 10, 11, 17, 18, 24, 25, 31])

    client = flask_app.test_client()
    _login(client)
    res = client.post("/api/generate", json={"year": 2026, "month": 10})
    assert res.status_code == 200, res.get_data(as_text=True)[:500]

    data = client.get("/api/shifts/2026/10").get_json()
    ph_warnings = [w["message"] for w in data["warnings"]
                   if w.get("warning_type") == "public_holiday_unmet"]
    assert not [m for m in ph_warnings if "調理T" in m], ph_warnings

    # 休み希望はすべて休みになっている（＝反映されている）
    worked = {x["date"] for x in data["shifts"]
              if x["staff_id"] == ids["cookT"] and x["assignment"] != "cook_off"}
    for d in (5, 12, 19, 26, 7, 14, 21, 28):
        assert f"2026-10-{d:02d}" not in worked, f"希望休の{d}日に出勤している"
    # 金曜（固定休）にも入らない
    for d in (2, 9, 16, 23, 30):
        assert f"2026-10-{d:02d}" not in worked, f"固定休の金曜{d}日に出勤している"


def test_part_time_auto_target_is_not_warned(tmp_path, monkeypatch):
    """公休日数が未入力のパートは自動算出のズレを警告しない／応援職員は目標なし。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed(flask_app)
    client = flask_app.test_client()
    _login(client)
    client.post("/api/generate", json={"year": 2026, "month": 10})

    data = client.get("/api/shifts/2026/10").get_json()
    warned = {s["name"]: s["public_holiday_warn"] for s in data["staff_list"]}
    targets = {s["name"]: s["public_holiday_target"] for s in data["staff_list"]}
    # パート・未入力 → 目標は目安として使うが、ズレても警告しない
    assert warned["介護B"] is False, warned
    # 応援職員は目標そのものを持たない
    assert targets["応援ヘルプ"] == 0, targets
    # 常勤は自動算出される（2026年10月＝平日22日・暦31日 → 公休9日）
    assert targets["介護A"] == 9, targets
    assert warned["介護A"] is True, warned
    # 手入力してあるパートは入力値がそのまま目標で、警告の対象
    assert targets["調理T"] == 15, targets
    assert warned["調理T"] is True, warned


def test_no_day_without_any_nurse(tmp_path, monkeypatch):
    """デイ営業日に看護師が1人も出勤しない日を作らない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed(flask_app)
    client = flask_app.test_client()
    _login(client)
    res = client.post("/api/generate", json={"year": 2026, "month": 10})
    assert res.status_code == 200, res.get_data(as_text=True)[:500]

    data = client.get("/api/shifts/2026/10").get_json()
    nurse_ids = {ids["nurseX"], ids["nurseY"]}
    on_duty = {}
    for x in data["shifts"]:
        if x["staff_id"] in nurse_ids and x["assignment"] not in ("off", ""):
            on_duty.setdefault(x["date"], set()).add(x["staff_id"])

    missing = []
    for d in range(1, 32):
        dt = datetime.date(2026, 10, d)
        if dt.weekday() not in (1, 2, 3):      # デイ営業日（火水木）だけ見る
            continue
        if not on_duty.get(dt.isoformat()):
            missing.append(dt.isoformat())
    assert not missing, f"看護師が1人もいない日がある: {missing}"

    assert not [w for w in data["warnings"]
                if w.get("warning_type") == "nurse_understaffed"], data["warnings"]


def test_workable_dates_of_other_month_do_not_block_generation(
        tmp_path, monkeypatch):
    """先月だけ出勤可能日を登録した職員が、翌月に1日も出勤できなくならない。"""
    from models import db, StaffWorkableDate

    flask_app = _make_app(tmp_path, monkeypatch)
    ids = _seed(flask_app)
    with flask_app.app_context():
        # 9月にだけ出勤可能日を登録（10月には1件もない）
        db.session.add(StaffWorkableDate(
            staff_id=ids["nurseX"], date=datetime.date(2026, 9, 8)))
        db.session.add(StaffWorkableDate(
            staff_id=ids["nurseX"], date=datetime.date(2026, 9, 22)))
        db.session.commit()

    client = flask_app.test_client()
    _login(client)
    client.post("/api/generate", json={"year": 2026, "month": 10})

    data = client.get("/api/shifts/2026/10").get_json()
    worked = [x["date"] for x in data["shifts"]
              if x["staff_id"] == ids["nurseX"] and x["assignment"] != "off"]
    assert worked, "9月の出勤可能日の登録で10月が全休になっている"
