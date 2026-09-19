"""シフト表の勤務表示を職種の名前で出すテスト。

ユーザー依頼 2026-09:
  「内田さんはドライバーのみで水曜日ドライバーとしてでます」「介護人数にふくめない」
  「大山さんも看護ってだして　介護職員のカウントではなく看護師です」

デイの札のままだと何の仕事か分からないので、マスの表示を
  ドライバー … 「デイ8:30-17:30」→「ドライバー8:30-17:30」
  看護師     … 「デイ9:00-16:00」→「看護9:00-16:00」
と出す。時刻や「午前のみ」はそのまま残す。

看護師は早番・遅番・訪問の札までは置き換えない（何の勤務か分からなくなるため）。
ドライバーは送迎しかしないので、デイ以外の札でも時刻を拾って置き換える。

人数カウントの扱いは従来どおり（看護師・ドライバーは介護の配置人数に数えない）で、
ここは表示だけの変更であることも確かめる。
"""
import re

import export

DRIVER = {"id": 1, "name": "ドライバー職員", "job_category": "driver",
          "qualifications": [], "qualification_codes": []}
NURSE = {"id": 2, "name": "看護職員", "job_category": "nurse_rehab",
         "qualifications": ["看護師"], "qualification_codes": ["nurse"]}
CARE = {"id": 3, "name": "介護職員", "job_category": "caregiver",
        "qualifications": ["介護福祉士"], "qualification_codes": ["care_worker"]}
PT = {"id": 4, "name": "PT職員", "job_category": "nurse_rehab",
      "qualifications": ["理学療法士"], "qualification_codes": ["pt"]}


def _register(staff_list):
    """_build_daily_data と同じ要領で表示置き換えの対象を登録する。"""
    export._DRIVER_IDS.clear()
    export._NURSE_DISPLAY_IDS.clear()
    for s in staff_list:
        if str(s.get("job_category", "") or "") == "driver":
            export._DRIVER_IDS.add(s["id"])
        elif export._is_nurse_display_staff(s):
            export._NURSE_DISPLAY_IDS.add(s["id"])


def test_driver_day_shift_shows_as_driver():
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(1, "デイ8:30-17:30") == "ドライバー8:30-17:30"


def test_nurse_day_shift_shows_as_nursing():
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(2, "デイ9:00-16:00") == "看護9:00-16:00"


def test_care_staff_label_is_untouched():
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(3, "デイ8:30-17:30") == "デイ8:30-17:30"


def test_nurse_keeps_early_late_and_visit_labels():
    """看護師の早番・遅番・訪問は置き換えない（何の勤務か分かるように）。"""
    _register([DRIVER, NURSE, CARE])
    for label in ("早番7:30-16:30", "遅番9:30-18:30", "訪問午前のみ",
                  "兼務(訪問→デイ)", "看護9:30-13:30"):
        assert export._display_label_for(2, label) == label


def test_driver_non_day_label_keeps_only_the_time():
    """ドライバーは送迎しかしないので、デイ以外の札でも置き換える。"""
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(1, "早番7:30-16:30") == "ドライバー7:30-16:30"
    assert export._display_label_for(1, "訪問午前のみ") == "ドライバー"


def test_am_only_suffix_is_kept():
    """「デイ午前のみ」のように時刻が無い札でも、後ろの言葉を残す。"""
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(2, "デイ午前のみ") == "看護午前のみ"
    assert export._display_label_for(2, "デイ午後13:30-17:30") == "看護午後13:30-17:30"


def test_pt_is_not_relabelled_as_nursing():
    """理学療法士は「看護」と出さない（看護師資格の人だけが対象）。"""
    _register([DRIVER, NURSE, CARE, PT])
    assert export._display_label_for(4, "デイ9:00-16:00") == "デイ9:00-16:00"


def test_empty_label_stays_empty():
    _register([DRIVER, NURSE, CARE])
    assert export._display_label_for(1, "") == ""


def test_headcount_rule_is_unchanged():
    """表示だけの変更で、人数カウントの扱いは従来どおりであること。"""
    assert export._is_nurse_or_pt_staff(DRIVER) is True
    assert export._is_nurse_or_pt_staff(NURSE) is True
    assert export._is_nurse_or_pt_staff(PT) is True
    assert export._is_nurse_or_pt_staff(CARE) is False


def test_screen_side_uses_the_same_rule():
    """画面（app.js / view.html）にも同じ置き換えが入っていること。"""
    app_js = open("static/js/app.js", encoding="utf-8").read()
    view_html = open("templates/view.html", encoding="utf-8").read()
    for src in (app_js, view_html):
        assert "ドライバー" in src
        assert "'看護'" in src or '"看護"' in src
        assert re.search(r"job_category\s*===\s*'driver'", src)
        assert "nurse" in src
