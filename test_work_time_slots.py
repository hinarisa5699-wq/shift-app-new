"""勤務時間マスタ（介護看護の「何時から何時まで」）のテスト。

ユーザー依頼 2026-09:
  「勤務時間を追加する場所作って　時間流動的に変わるから　介護看護何時から何時でいれると
    シフト作成の場所にでてきて　ドラッグしてかえられるようにして」

自動作成が使う固定パターン（早番・デイ①…）とは別枠の、手直し用の時間枠。
ここで足した枠が
  ① 設定画面から追加・編集・削除でき、
  ② シフトカレンダーの手直しパレット（ドラッグ元）に出てきて、
  ③ マスへ入れた内容がそのまま保存され、
  ④ Excel/PDF/CSV と職員の閲覧画面にも時刻が出る
ことを確かめる。
"""
from datetime import date

from test_generate_endpoint import _make_app, _login

YEAR, MONTH = 2026, 9
GEN_ID = "gen-work-time"


def _seed_one_care_staff(flask_app):
    """介護職員を1人と、その人の9/1のシフトを1件だけ作る。"""
    from models import db, Staff, GeneratedShift

    with flask_app.app_context():
        Staff.query.delete()
        GeneratedShift.query.delete()
        db.session.commit()

        st = Staff(name="介護テスト", employment_type="常勤", job_category="caregiver",
                   staff_group="care")
        db.session.add(st)
        db.session.flush()
        db.session.add(GeneratedShift(
            generation_id=GEN_ID, date=date(YEAR, MONTH, 1),
            staff_id=st.id, assignment="day_pattern1",
        ))
        db.session.commit()
        return st.id


def _as_admin(flask_app):
    client = flask_app.test_client()
    assert _login(client, "admin", "testpass").status_code in (301, 302)
    return client


def test_added_work_time_appears_in_palette_and_can_be_saved(tmp_path, monkeypatch):
    """足した勤務時間がパレットに出て、マスに入れると保存される（＝ドラッグの受け皿）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    staff_id = _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    res = client.post("/api/work-times",
                      json={"start_time": "10:00", "end_time": "15:00", "label": "時短"})
    assert res.status_code == 201, res.get_json()
    slot = res.get_json()
    assert slot["code"] == "wt_%d" % slot["id"]
    assert slot["display_label"] == "時短 10:00-15:00"

    # 名前なしでも足せる（時刻がそのまま札の文字になる。表記は既存ラベルに合わせて 9:00）
    res = client.post("/api/work-times", json={"start_time": "9:00", "end_time": "13:00"})
    assert res.status_code == 201
    assert res.get_json()["display_label"] == "9:00-13:00"

    # 手直しパレット（ドラッグ元）に両方出る
    data = client.get("/api/shifts/%d/%d" % (YEAR, MONTH)).get_json()
    palette = [x for x in data["palette"]["care"] if x["code"].startswith("wt_")]
    assert [x["label"] for x in palette] == ["時短 10:00-15:00", "9:00-13:00"]
    assert data["care_labels"][slot["code"]] == "時短 10:00-15:00"

    # マスへ落とした（＝セル保存）内容がそのまま残る
    res = client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "%d-%02d-02" % (YEAR, MONTH),
                     "staff_id": staff_id, "assignment": slot["code"]}],
    })
    assert res.status_code == 200
    assert res.get_json()["applied"] == 1

    from models import GeneratedShift

    with flask_app.app_context():
        row = GeneratedShift.query.filter_by(
            date=date(YEAR, MONTH, 2), staff_id=staff_id).first()
        assert row is not None and row.assignment == slot["code"]


def test_work_time_is_validated(tmp_path, monkeypatch):
    """終了が開始より前／時刻が読めないものは受け付けない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    assert client.post("/api/work-times",
                       json={"start_time": "15:00", "end_time": "10:00"}).status_code == 400
    assert client.post("/api/work-times",
                       json={"start_time": "", "end_time": "10:00"}).status_code == 400
    assert client.post("/api/work-times",
                       json={"start_time": "25:00", "end_time": "26:00"}).status_code == 400


def test_editing_time_updates_existing_cells_and_delete_is_blocked_while_used(
        tmp_path, monkeypatch):
    """時刻を直すと入れてある日の表示も変わる。使っている枠は消せない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    staff_id = _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    slot = client.post("/api/work-times",
                       json={"start_time": "10:00", "end_time": "15:00"}).get_json()
    client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "%d-%02d-02" % (YEAR, MONTH),
                     "staff_id": staff_id, "assignment": slot["code"]}],
    })

    # 使用中は削除を止める（消すと表のセルが読めなくなるため）
    res = client.delete("/api/work-times/%d" % slot["id"])
    assert res.status_code == 409
    assert "使われている" in res.get_json()["error"]

    # 時刻の変更はコードを変えないので、入れてある日の表示だけが変わる
    res = client.put("/api/work-times/%d" % slot["id"], json={"start_time": "11:00"})
    assert res.status_code == 200
    assert res.get_json()["display_label"] == "11:00-15:00"
    data = client.get("/api/shifts/%d/%d" % (YEAR, MONTH)).get_json()
    assert data["care_labels"][slot["code"]] == "11:00-15:00"

    # 使っていない枠は消せる。消したら札もラベルも消える
    other = client.post("/api/work-times",
                        json={"start_time": "13:00", "end_time": "17:00"}).get_json()
    assert client.delete("/api/work-times/%d" % other["id"]).status_code == 200
    data = client.get("/api/shifts/%d/%d" % (YEAR, MONTH)).get_json()
    assert other["code"] not in data["care_labels"]
    assert all(x["code"] != other["code"] for x in data["palette"]["care"])


def test_work_time_shows_in_exports_and_counts_as_care_work(tmp_path, monkeypatch):
    """Excel/PDF/CSV に時刻が出て、午前・午後の人数にも数える。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    staff_id = _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    slot = client.post("/api/work-times",
                       json={"start_time": "10:00", "end_time": "15:00"}).get_json()
    client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "%d-%02d-01" % (YEAR, MONTH),
                     "staff_id": staff_id, "assignment": slot["code"]}],
    })

    import export

    # 10:00-15:00 は 12:30 をまたぐので午前にも午後にも在籍している
    assert export.ASSIGNMENT_LABELS[slot["code"]] == "10:00-15:00"
    assert slot["code"] in export._DAY_AM_SET
    assert slot["code"] in export._DAY_PM_SET
    assert slot["code"] in export._CARE_WORK_SET
    # 出力した文字から元のコードへ戻せる（手修正Excelの取り込み用）
    assert export.parse_shift_cell("10:00-15:00")["assignment"] == slot["code"]

    res = client.get("/api/export/%s/csv" % GEN_ID)
    assert res.status_code == 200
    assert "10:00-15:00" in res.get_data(as_text=True)
    assert client.get("/api/export/%s/excel" % GEN_ID).status_code == 200
    assert client.get(
        "/api/export/%s/pdf?group=care&half=first" % GEN_ID).status_code == 200


def test_settings_page_shows_work_time_section(tmp_path, monkeypatch):
    """設定画面に「介護看護の勤務時間」の追加欄と登録済みの行が出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)
    client.post("/api/work-times", json={"start_time": "10:00", "end_time": "15:00"})

    html = client.get("/settings").get_data(as_text=True)
    assert "介護看護の勤務時間" in html
    assert "new-care-time-start" in html
    assert "addCareTimeSlot()" in html
    assert 'value="10:00"' in html


def test_split_shift_with_break(tmp_path, monkeypatch):
    """中抜け勤務（1人が午前と夕方の2回に分かれて入る）＋休憩を登録できる。

    ユーザー依頼 2026-09:「日曜日営業になった　７時半から１３時　３０分休憩
    そのあと１７時から１９時まで」「１人の人が担当」。
    """
    flask_app = _make_app(tmp_path, monkeypatch)
    staff_id = _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    res = client.post("/api/work-times", json={
        "label": "日曜", "start_time": "7:30", "end_time": "13:00",
        "start_time2": "17:00", "end_time2": "19:00", "break_minutes": 30,
    })
    assert res.status_code == 201, res.get_json()
    slot = res.get_json()
    assert slot["is_split"] is True
    assert slot["time_text"] == "7:30-13:00/17:00-19:00"
    assert slot["display_label"] == "日曜 7:30-13:00/17:00-19:00"
    # 5時間30分 + 2時間 - 休憩30分 = 7時間
    assert slot["work_minutes"] == 7 * 60
    assert "中抜け" in slot["detail_text"] and "休憩30分" in slot["detail_text"]
    # 7:30 から 19:00 まで在籍するので午前・午後どちらの人数にも数える
    assert slot["covers_am"] and slot["covers_pm"]

    # 1枚の札としてパレットに出る（2つには割れない）
    data = client.get("/api/shifts/%d/%d" % (YEAR, MONTH)).get_json()
    chips = [x for x in data["palette"]["care"] if x["code"] == slot["code"]]
    assert len(chips) == 1
    assert chips[0]["label"] == "日曜 7:30-13:00/17:00-19:00"
    assert "中抜け" in chips[0]["title"]
    assert "中抜け" in data["care_details"][slot["code"]]

    # マスへ入れて保存でき、出力にも中抜けの時刻が出る
    res = client.post("/api/shift/cells", json={
        "year": YEAR, "month": MONTH,
        "changes": [{"date": "%d-%02d-01" % (YEAR, MONTH),
                     "staff_id": staff_id, "assignment": slot["code"]}],
    })
    assert res.status_code == 200 and res.get_json()["applied"] == 1

    import export

    assert export.ASSIGNMENT_LABELS[slot["code"]] == "日曜 7:30-13:00/17:00-19:00"
    csv_text = client.get("/api/export/%s/csv" % GEN_ID).get_data(as_text=True)
    assert "7:30-13:00/17:00-19:00" in csv_text


def test_split_shift_is_validated(tmp_path, monkeypatch):
    """中抜け後の時間は「両方入れる」「1つめの後ろ」でないと受け付けない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    def post(**kw):
        body = {"start_time": "7:30", "end_time": "13:00"}
        body.update(kw)
        return client.post("/api/work-times", json=body)

    # 片方だけ
    res = post(start_time2="17:00")
    assert res.status_code == 400 and "両方" in res.get_json()["error"]
    # 1つめの終了より前から始まっている
    res = post(start_time2="11:00", end_time2="19:00")
    assert res.status_code == 400 and "後から" in res.get_json()["error"]
    # 2つめの中で逆転している
    res = post(start_time2="19:00", end_time2="17:00")
    assert res.status_code == 400
    # 休憩が長すぎる
    res = post(break_minutes=600)
    assert res.status_code == 400 and "休憩" in res.get_json()["error"]


def test_split_can_be_turned_off_later(tmp_path, monkeypatch):
    """中抜けをやめる（2つめの時間を空にする）と通しの勤務に戻る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed_one_care_staff(flask_app)
    client = _as_admin(flask_app)

    slot = client.post("/api/work-times", json={
        "start_time": "7:30", "end_time": "13:00",
        "start_time2": "17:00", "end_time2": "19:00", "break_minutes": 30,
    }).get_json()

    res = client.put("/api/work-times/%d" % slot["id"],
                     json={"start_time2": "", "end_time2": ""})
    assert res.status_code == 200
    after = res.get_json()
    assert after["is_split"] is False
    assert after["time_text"] == "7:30-13:00"
    assert after["work_minutes"] == 5 * 60   # 5時間30分 - 休憩30分
