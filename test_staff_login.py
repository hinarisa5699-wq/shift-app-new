"""職員ごとのログイン（1人1パスワード）のテスト。

ユーザー依頼 2026-08:「パスワード1人ずつにしたら？」
共通パスワードだと、退職した人がパスワードを覚えている限りシフトを見られてしまう。
1人1アカウントにして、退職月を過ぎたら自動的に入れなくなることを確かめる。
"""
import re
from datetime import date

from test_generate_endpoint import _make_app, _seed, _login

# 発行後に出るパスワードカード1枚から「氏名／ID／パスワード」を読み取る
_CARD = re.compile(
    r'text-2xl font-bold text-gray-900">(.+?) さん</p>.*?'
    r'tracking-wider">\s*([A-Za-z0-9_-]+)\s*</td>.*?'
    r'tracking-wider">\s*([A-Za-z0-9_-]+)\s*</td>',
    re.S,
)


def _cards_of(res):
    """応答のカード画面から発行内容を読み取る。"""
    return [
        {"name": n.strip(), "login_id": i.strip(), "password": p.strip()}
        for n, i, p in _CARD.findall(res.data.decode("utf-8", "replace"))
    ]


def _issue_all(admin_client):
    """管理者としてログイン済みのクライアントで、全員分のパスワードを発行する。

    平文パスワードは応答のHTML（カード）にしか載らない（クッキーには保存しない）
    ので、戻り値のページから読み取る。
    """
    res = admin_client.post("/api/staff/login-passwords/issue-all")
    res.issued = _cards_of(res)
    return res


def test_issue_all_creates_login_for_active_staff(tmp_path, monkeypatch):
    """まとめて発行すると、在籍中の職員にIDとパスワードが割り当てられる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    res = _issue_all(admin)
    assert res.status_code == 200

    rows = res.issued
    assert len(rows) == 8                     # _seed で登録した8名
    assert all(r["login_id"] and r["password"] for r in rows)
    # IDは職員ごとに違う
    assert len({r["login_id"] for r in rows}) == 8

    from models import Staff
    with flask_app.app_context():
        for st in Staff.query.all():
            assert st.login_id
            assert st.login_password_hash          # ハッシュで保存されている
            assert st.login_password_hash != ""


def test_staff_can_login_with_own_password(tmp_path, monkeypatch):
    """発行されたID・パスワードでログインでき、閲覧ページに入れる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    staff_client = flask_app.test_client()
    r = _login(staff_client, me["login_id"], me["password"])
    assert r.status_code in (301, 302)
    assert "/view" in r.headers.get("Location", "")

    # 閲覧はできる
    assert staff_client.get("/view").status_code == 200
    # 編集・設定はできない（閲覧専用と同じ権限）
    assert staff_client.get("/staff").status_code in (301, 302)
    assert staff_client.post("/api/generate", json={"year": 2026, "month": 9}).status_code == 403


def test_wrong_password_is_rejected(tmp_path, monkeypatch):
    """パスワードが違えばログインできない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    guest = flask_app.test_client()
    r = _login(guest, me["login_id"], me["password"] + "x")
    assert r.status_code == 200                 # ログイン画面に戻る（＝入れていない）


def test_retired_staff_cannot_login_after_retirement_month(tmp_path, monkeypatch):
    """退職月を過ぎた職員はログインできない（=退職後は見られない）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    from models import db, Staff
    with flask_app.app_context():
        st = Staff.query.filter_by(login_id=me["login_id"]).first()
        st.retired = True
        # 先月末で退職＝もう入れない
        today = date.today()
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        st.retired_date = date(y, m, 1)
        db.session.commit()

    guest = flask_app.test_client()
    r = _login(guest, me["login_id"], me["password"])
    assert r.status_code == 200                 # ログイン画面のまま
    assert guest.get("/view").status_code in (301, 302)   # 入れていない


def test_retired_this_month_can_still_login(tmp_path, monkeypatch):
    """退職月のうちは、まだログインできる（月末まで自分のシフトを見られる）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    from models import db, Staff
    with flask_app.app_context():
        st = Staff.query.filter_by(login_id=me["login_id"]).first()
        st.retired = True
        today = date.today()
        st.retired_date = date(today.year, today.month, 1)
        db.session.commit()

    guest = flask_app.test_client()
    r = _login(guest, me["login_id"], me["password"])
    assert r.status_code in (301, 302)
    assert "/view" in r.headers.get("Location", "")


def test_logged_in_staff_is_kicked_out_when_retired(tmp_path, monkeypatch):
    """ログインしっぱなしの端末も、退職にした時点で次のアクセスで切れる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    staff_client = flask_app.test_client()
    _login(staff_client, me["login_id"], me["password"])
    assert staff_client.get("/view").status_code == 200

    from models import db, Staff
    with flask_app.app_context():
        st = Staff.query.filter_by(login_id=me["login_id"]).first()
        st.on_leave = True                       # 休職中にした
        db.session.commit()

    # 同じセッションのままでも、もう見られない
    assert staff_client.get("/view").status_code in (301, 302)


def test_revoke_blocks_login(tmp_path, monkeypatch):
    """「停止」を押した職員はログインできなくなる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    from models import Staff
    with flask_app.app_context():
        sid = Staff.query.filter_by(login_id=me["login_id"]).first().id
    admin.post("/api/staff/{}/login-password/revoke".format(sid))

    guest = flask_app.test_client()
    r = _login(guest, me["login_id"], me["password"])
    assert r.status_code == 200                  # 入れない


def test_shared_viewer_login_can_be_turned_off(tmp_path, monkeypatch):
    """共通パスワード(staff)をOFFにすると、共通では入れず個人IDでは入れる。"""
    monkeypatch.delenv("SHIFT_STAFF_PASSWORD", raising=False)
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    # 共通パスワードを設定（この時点ではまだ使える）
    admin.post("/api/settings", data={
        "viewer_password": "kyoutsuu1234",
        "shared_viewer_login_present": "1",
        "shared_viewer_login_enabled": "1",
        "min_staff_at_9": "1", "min_staff_at_15": "1",
    }, follow_redirects=True)
    g1 = flask_app.test_client()
    assert _login(g1, "staff", "kyoutsuu1234").status_code in (301, 302)

    # 共通パスワードを止める（チェックを外して保存）
    admin.post("/api/settings", data={
        "shared_viewer_login_present": "1",
        "min_staff_at_9": "1", "min_staff_at_15": "1",
    }, follow_redirects=True)

    g2 = flask_app.test_client()
    assert _login(g2, "staff", "kyoutsuu1234").status_code == 200   # 共通は入れない

    g3 = flask_app.test_client()
    r = _login(g3, me["login_id"], me["password"])
    assert r.status_code in (301, 302)                              # 個人は入れる


def test_login_id_cannot_collide_with_role_accounts(tmp_path, monkeypatch):
    """職員のログインIDに admin などの予約語は設定できない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    from models import Staff
    with flask_app.app_context():
        st = Staff.query.order_by(Staff.id).first()
        sid, before = st.id, st.login_id

    admin.post("/api/staff/{}".format(sid), data={
        "name": "介護A", "job_category": "caregiver", "employment_type": "常勤",
        "login_id": "admin",
    }, follow_redirects=True)

    with flask_app.app_context():
        assert Staff.query.get(sid).login_id != "admin"
        assert Staff.query.get(sid).login_id == before or before == ""


def test_single_card_can_be_printed_for_one_staff(tmp_path, monkeypatch):
    """職員1人分のカードだけを発行・印刷できる（追加やID変更のとき）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    _issue_all(admin)

    from models import Staff
    with flask_app.app_context():
        st = Staff.query.order_by(Staff.id).first()
        sid, name = st.id, st.name

    res = admin.post("/api/staff/{}/login-password".format(sid))
    assert res.status_code == 200
    cards = _cards_of(res)
    assert len(cards) == 1                      # その人のカードだけ
    assert cards[0]["name"] == name
    body = res.data.decode("utf-8", "replace")
    assert "印刷用PDF" in body                  # PDFダウンロードのボタンがある
    assert "/view" in body                      # 開く場所（URL）が載っている

    # 新しいパスワードで入れる
    guest = flask_app.test_client()
    r = _login(guest, cards[0]["login_id"], cards[0]["password"])
    assert r.status_code in (301, 302)


def test_reissue_invalidates_the_old_password(tmp_path, monkeypatch):
    """カードを再発行すると、前のパスワードでは入れなくなる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    from models import Staff
    with flask_app.app_context():
        sid = Staff.query.filter_by(login_id=me["login_id"]).first().id
    new_card = _cards_of(admin.post("/api/staff/{}/login-password".format(sid)))[0]

    old = flask_app.test_client()
    assert _login(old, me["login_id"], me["password"]).status_code == 200      # 旧は不可
    new = flask_app.test_client()
    assert _login(new, new_card["login_id"], new_card["password"]).status_code in (301, 302)


def test_new_staff_gets_own_card(tmp_path, monkeypatch):
    """あとから追加した職員にも、その人のカードだけを発行できる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    _issue_all(admin)

    admin.post("/api/staff", data={
        "name": "新人 さくら", "job_category": "caregiver", "employment_type": "パート",
        "available_days": "0",
    }, follow_redirects=True)

    from models import Staff
    with flask_app.app_context():
        st = Staff.query.filter_by(name="新人 さくら").first()
        assert st is not None
        assert st.login_id                       # IDは自動で付く
        assert not st.login_password_hash        # パスワードはまだ未発行
        sid = st.id

    cards = _cards_of(admin.post("/api/staff/{}/login-password".format(sid)))
    assert len(cards) == 1 and cards[0]["name"] == "新人 さくら"

    guest = flask_app.test_client()
    assert _login(guest, cards[0]["login_id"], cards[0]["password"]).status_code in (301, 302)


def test_changing_login_id_then_card_uses_new_id(tmp_path, monkeypatch):
    """ログインIDを変えたあとに出すカードには、新しいIDが載る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    _issue_all(admin)

    from models import Staff
    with flask_app.app_context():
        sid = Staff.query.order_by(Staff.id).first().id

    admin.post("/api/staff/{}".format(sid), data={
        "name": "介護A", "job_category": "caregiver", "employment_type": "常勤",
        "login_id": "kaigo-a",
    }, follow_redirects=True)

    cards = _cards_of(admin.post("/api/staff/{}/login-password".format(sid)))
    assert cards[0]["login_id"] == "kaigo-a"

    guest = flask_app.test_client()
    assert _login(guest, "kaigo-a", cards[0]["password"]).status_code in (301, 302)


def test_reissue_all_makes_cards_for_everyone(tmp_path, monkeypatch):
    """「全員のカードを作り直す」で、在籍中の全員分のカードが出る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    _issue_all(admin)

    res = admin.post("/api/staff/login-passwords/reissue-all")
    assert res.status_code == 200
    assert len(_cards_of(res)) == 8


def test_cards_are_not_kept_in_the_session_cookie(tmp_path, monkeypatch):
    """平文パスワードをクッキー（セッション）に残さない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    issued = _issue_all(admin).issued
    assert issued

    with admin.session_transaction() as sess:
        blob = repr(dict(sess))
    for row in issued:
        assert row["password"] not in blob

    # 画面を開き直しても、もうパスワードは表示されない
    body = admin.get("/staff/logins").data.decode("utf-8", "replace")
    for row in issued:
        assert row["password"] not in body


def test_staff_display_order_applies_everywhere(tmp_path, monkeypatch):
    """職員一覧で決めた並び順が、シフト表（閲覧API）の職員順にも反映される。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    from models import Staff
    with flask_app.app_context():
        ids = [s.id for s in Staff.query.order_by(Staff.id).all()]

    # 逆順に並べ替える
    data = {"order_{}".format(sid): str(len(ids) - i) for i, sid in enumerate(ids)}
    res = admin.post("/api/staff/order", data=data, follow_redirects=True)
    assert res.status_code == 200

    import app as app_module
    with flask_app.app_context():
        ordered = [s.id for s in app_module._ordered_staff().all()]
    assert ordered == list(reversed(ids))

    # 職員一覧の表示もその順番
    body = admin.get("/staff").data.decode("utf-8", "replace")
    with flask_app.app_context():
        names = [Staff.query.get(sid).name for sid in reversed(ids)]
    positions = [body.index(n) for n in names]
    assert positions == sorted(positions), "職員一覧が並び順どおりに出ていない"


def test_new_staff_goes_to_the_end_of_the_order(tmp_path, monkeypatch):
    """あとから追加した職員は、並び順のいちばん最後に入る。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    from models import Staff
    with flask_app.app_context():
        ids = [s.id for s in Staff.query.order_by(Staff.id).all()]
    admin.post("/api/staff/order",
               data={"order_{}".format(sid): str(i + 1) for i, sid in enumerate(ids)},
               follow_redirects=True)

    admin.post("/api/staff", data={
        "name": "あとから 太郎", "job_category": "caregiver",
        "employment_type": "パート", "available_days": "0",
    }, follow_redirects=True)

    import app as app_module
    with flask_app.app_context():
        assert app_module._ordered_staff().all()[-1].name == "あとから 太郎"


def test_login_cards_pdf_matches_meal_card_size(tmp_path, monkeypatch):
    """カードPDFが食事カードと同じ寸法・配置になっている（裏表ラミネート用）。"""
    import export

    data = export.build_login_cards_pdf(
        [{"name": "テスト 太郎{}".format(i), "login_id": "s{}".format(i),
          "password": "abcd{:04d}".format(i)} for i in range(7)],
        "https://example.invalid/view",
    )
    assert data[:4] == b"%PDF"

    fitz = __import__("fitz")
    doc = fitz.open(stream=data, filetype="pdf")
    assert doc.page_count == 2, "6枚/ページで折り返していない"
    mm = 25.4 / 72.0

    page = doc[0]
    assert abs(page.rect.width * mm - 210.0) < 0.5    # A4縦
    assert abs(page.rect.height * mm - 297.0) < 0.5

    frames = sorted(
        (r for r in (d["rect"] for d in page.get_drawings())
         if r.width > 200 and r.height > 100),   # 枠だけ（下線は除く）
        key=lambda r: (round(r.y0), r.x0),
    )
    assert len(frames) == 6, "1ページ6枚になっていない"
    # 食事カードPDFの実測値と同じ
    for f in frames:
        assert abs(f.width * mm - 87.8) < 0.2, "カード幅が87.8mmでない"
        assert abs(f.height * mm - 72.2) < 0.2, "カード高が72.2mmでない"
    xs = sorted({round(f.x0 * mm, 1) for f in frames})
    ys = sorted({round(f.y0 * mm, 1) for f in frames})
    assert xs == [14.3, 108.2], xs
    assert ys == [10.1, 88.1, 166.2], ys


def test_login_cards_pdf_endpoint(tmp_path, monkeypatch):
    """カード画面からPDFをダウンロードできる。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    issued = _issue_all(admin).issued

    res = admin.post("/api/staff/login-cards.pdf", data={
        "c_name": [c["name"] for c in issued],
        "c_id": [c["login_id"] for c in issued],
        "c_pw": [c["password"] for c in issued],
    })
    assert res.status_code == 200
    assert res.mimetype == "application/pdf"
    assert res.data[:4] == b"%PDF"

    fitz = __import__("fitz")
    doc = fitz.open(stream=res.data, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    for c in issued:
        assert c["password"] in text, "PDFにパスワードが入っていない"


def test_renumber_login_ids_follows_display_order(tmp_path, monkeypatch):
    """ログインIDを並び順の番号で振り直せる（S001, S002 …）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")

    from models import Staff
    with flask_app.app_context():
        ids = [s.id for s in Staff.query.order_by(Staff.id).all()]
    # 1,2,3,5,6,7,8,9 と欠番(4)をつくる（食事カードの欠番と同じ状況）
    numbers = [1, 2, 3, 5, 6, 7, 8, 9]
    admin.post("/api/staff/order",
               data={"order_{}".format(sid): str(n) for sid, n in zip(ids, numbers)},
               follow_redirects=True)

    res = admin.post("/api/staff/login-ids/renumber", follow_redirects=True)
    assert res.status_code == 200

    import app as app_module
    with flask_app.app_context():
        got = [(s.display_order, s.login_id) for s in app_module._ordered_staff().all()]
    assert got == [(n, "S{:03d}".format(n)) for n in numbers], got


def test_login_id_is_case_insensitive(tmp_path, monkeypatch):
    """S001 でも s001 でもログインできる（カードは大文字で印刷する）。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    _issue_all(admin)
    admin.post("/api/staff/login-ids/renumber", follow_redirects=True)

    from models import Staff
    with flask_app.app_context():
        st = Staff.query.order_by(Staff.display_order, Staff.id).first()
        sid, login_id = st.id, st.login_id
    assert login_id.startswith("S"), login_id

    card = _cards_of(admin.post("/api/staff/{}/login-password".format(sid)))[0]
    assert card["login_id"] == login_id          # カードは大文字のまま

    upper = flask_app.test_client()
    assert _login(upper, login_id, card["password"]).status_code in (301, 302)
    lower = flask_app.test_client()
    assert _login(lower, login_id.lower(), card["password"]).status_code in (301, 302)


def test_renumber_keeps_passwords(tmp_path, monkeypatch):
    """IDを振り直してもパスワードは変わらない。"""
    flask_app = _make_app(tmp_path, monkeypatch)
    _seed(flask_app)
    admin = flask_app.test_client()
    _login(admin, "admin", "testpass")
    me = _issue_all(admin).issued[0]

    from models import Staff
    with flask_app.app_context():
        sid = Staff.query.filter_by(login_id=me["login_id"]).first().id
    admin.post("/api/staff/login-ids/renumber", follow_redirects=True)
    with flask_app.app_context():
        new_id = Staff.query.get(sid).login_id

    guest = flask_app.test_client()
    assert _login(guest, new_id, me["password"]).status_code in (301, 302)


def test_login_card_pdf_has_qr_code(tmp_path, monkeypatch):
    """カードPDFにQRコードが入っていて、開くアドレスが正しく入っている。"""
    import segno
    import export

    url = "https://example.invalid/view"
    data = export.build_login_cards_pdf(
        [{"name": "テスト 太郎", "login_id": "S001", "password": "abcd1234"}], url)

    fitz = __import__("fitz")
    doc = fitz.open(stream=data, filetype="pdf")
    page = doc[0]

    # QRのマス目を、描かれた黒い四角から読み直して照合する
    expect = [list(r) for r in segno.make(url, error="m").matrix]
    n = len(expect)
    mm = 25.4 / 72.0
    qr_x = export.CARD_X0_MM + export.CARD_W_MM - 5.0 - 23.0
    qr_y = export.CARD_Y0_MM + 14.5
    cell = 23.0 / n

    black = [d["rect"] for d in page.get_drawings() if d.get("fill")]
    assert black, "QRの黒い四角が1つも描かれていない"

    def is_black(r, c):
        px = (qr_x + (c + 0.5) * cell) / mm
        py = (qr_y + (r + 0.5) * cell) / mm
        return any(rect.x0 <= px <= rect.x1 and rect.y0 <= py <= rect.y1
                   for rect in black)

    wrong = sum(1 for r in range(n) for c in range(n)
                if is_black(r, c) != expect[r][c])
    assert wrong == 0, "QRのマス目が{}個ちがう".format(wrong)


def test_login_card_qr_works_without_segno(tmp_path, monkeypatch):
    """QRのライブラリが無くても、カードPDF自体は作れる（落ちない）。"""
    import builtins
    import export

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "segno":
            raise ImportError("no segno")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    data = export.build_login_cards_pdf(
        [{"name": "テスト 太郎", "login_id": "S001", "password": "abcd1234"}],
        "https://example.invalid/view")
    assert data[:4] == b"%PDF"
    assert export.qr_data_uri("https://example.invalid/view") == ""
