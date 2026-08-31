"""Googleカレンダー（限定公開URL・iCal形式）の取り込み。

ユーザー依頼 2026-08:「前垣茜のみGoogleカレンダー連動して。★がついてるものは私用で変換」。

向きは Googleカレンダー → アプリ の一方通行（読み取りのみ）。
アプリからGoogleカレンダーへは何も書かない・消さない。

「前垣茜のみ」は名前で判定せず、職員マスタにURLを入れた職員だけを対象にする
（Staff.google_ics_url）。あとから別の職員を足したくなってもコード変更が要らない。

タイトルに ★（または ☆）が入っている予定は、中身を出さずに「私用」へ置き換える。
他の職員にも見えるシフト表に私的な予定名が出ないようにするため。
"""

import datetime
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar

JST = ZoneInfo("Asia/Tokyo")

# 限定公開URLは calendar.google.com/calendar/ical/... の形。
#   他ホストを弾いて、社内URL等を打たせる誤用（SSRF）を防ぐ。
_ALLOWED_HOSTS = ("calendar.google.com", "www.google.com")

# 中身を伏せる印と、その置き換え先。
#   ユーザー指定 2026-08:「⭐︎ この星でお願い」＝絵文字の星(U+2B50)。
#   ただし端末・IMEによって ★(U+2605) や ☆(U+2606) が入力されることがあるので、
#   取りこぼして私的な予定名が表に出るより安全側に倒して、どれでも「私用」にする。
PRIVATE_MARKS = ("⭐", "★", "☆", "\U0001f31f")  # ⭐ ★ ☆ 🌟
PRIVATE_TITLE = "私用"

# StaffPlan.title の桁数に合わせる
_TITLE_MAX = 40
# 予定名が空のときの表示
_NO_TITLE = "予定"
# 取り込むICSの上限（およそ数万件ぶん。これを超えるカレンダーは想定しない）
_MAX_BYTES = 5 * 1024 * 1024


class GoogleCalendarError(Exception):
    """取り込みに失敗したときのエラー（画面にそのまま出せる日本語文言）。"""


def normalize_ics_url(raw):
    """入力されたURLを検証して返す。空欄なら ""（連携なし）。"""
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        raise GoogleCalendarError(
            "URLは https:// で始まるGoogleカレンダーの限定公開URLを貼ってください。")
    host = url[len("https://"):].split("/", 1)[0].split("@")[-1].lower()
    if host not in _ALLOWED_HOSTS:
        raise GoogleCalendarError(
            "Googleカレンダーの限定公開URL（calendar.google.com のURL）を貼ってください。")
    if "/basic.ics" not in url and not url.endswith(".ics"):
        raise GoogleCalendarError(
            "iCal形式のURL（末尾が .ics のもの）を貼ってください。"
            "Googleカレンダーの設定 →「カレンダーの統合」→「非公開URL（iCal形式）」です。")
    return url


def fetch_ics(url, timeout=20):
    """限定公開URLからICSを取ってくる。"""
    req = urllib.request.Request(url, headers={"User-Agent": "shift-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise GoogleCalendarError(
                "カレンダーを読み取れませんでした（URLが違うか、無効になっています）。"
                "Googleカレンダーの設定で非公開URLを取り直して貼り直してください。")
        raise GoogleCalendarError(
            "Googleカレンダーに接続できませんでした（HTTP {}）。".format(e.code))
    except (urllib.error.URLError, OSError, TimeoutError):
        raise GoogleCalendarError(
            "Googleカレンダーに接続できませんでした。時間をおいて試してください。")
    if len(raw) > _MAX_BYTES:
        raise GoogleCalendarError("カレンダーの件数が多すぎて取り込めませんでした。")
    text = raw.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text:
        raise GoogleCalendarError(
            "カレンダーの形式が読み取れませんでした。"
            "「非公開URL（iCal形式）」のURLか確認してください。")
    return text


def _clean_title(summary):
    """予定名を1行に整える。★付きは中身を出さずに「私用」へ。"""
    text = " ".join(str(summary or "").split())
    if any(mark in text for mark in PRIVATE_MARKS):
        return PRIVATE_TITLE
    if not text:
        return _NO_TITLE
    return text[:_TITLE_MAX]


def _as_local(value):
    """DTSTART/DTEND の値を (日付, 時刻文字列) に直す。

    終日予定は date 型で来るので時刻は ""。時刻付きはJSTに直して "HH:MM"。
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=JST)
        local = value.astimezone(JST)
        return local.date(), local.strftime("%H:%M")
    return value, ""


def _occurrence_rows(start_value, end_value, title):
    """1件の予定を「日付ごとの行」に展開する。

    終日予定の DTEND は翌日（排他）なので1日戻す。
    日をまたぐ予定は日ごとに1行ずつ作り、初日は開始時刻のみ・最終日は終了時刻のみ入れる。
    """
    start_date, start_time = _as_local(start_value)
    if end_value is None:
        end_date, end_time = start_date, ""
    else:
        end_date, end_time = _as_local(end_value)

    all_day = (start_time == "" and end_time == "")
    if all_day and end_date > start_date:
        end_date = end_date - datetime.timedelta(days=1)
    # 24:00 ちょうど終わりの予定は前日扱いにする（0:00 の行を作らない）
    if not all_day and end_time == "00:00" and end_date > start_date:
        end_date = end_date - datetime.timedelta(days=1)
        end_time = ""
    if end_date < start_date:
        end_date = start_date

    rows = []
    day = start_date
    while day <= end_date:
        first = (day == start_date)
        last = (day == end_date)
        rows.append({
            "date": day,
            "start_time": start_time if first else "",
            "end_time": end_time if last else "",
            "title": title,
        })
        day += datetime.timedelta(days=1)
    return rows


def _recurrence_key(value):
    """RECURRENCE-ID / EXDATE を突き合わせる用のキー（日付単位で見る）。"""
    d, _t = _as_local(value)
    return d


def _expand_rrule(component, start_value, window_start, window_end):
    """RRULE を持つ予定の開始日時を、対象期間ぶんだけ列挙する。"""
    rule_text = component.get("RRULE")
    if rule_text is None:
        return []
    try:
        text = rule_text.to_ical().decode("utf-8")
    except Exception:
        return []

    naive = isinstance(start_value, datetime.date) and not isinstance(
        start_value, datetime.datetime)
    if naive:
        dtstart = datetime.datetime.combine(start_value, datetime.time(0, 0))
    else:
        dtstart = start_value
        if dtstart.tzinfo is not None:
            dtstart = dtstart.astimezone(JST).replace(tzinfo=None)

    # 日をまたぐ予定を取りこぼさないよう、前後に少し余裕を持たせて数える
    lo = datetime.datetime.combine(
        window_start - datetime.timedelta(days=7), datetime.time(0, 0))
    hi = datetime.datetime.combine(
        window_end + datetime.timedelta(days=7), datetime.time(23, 59))
    try:
        rule = rrulestr(text, dtstart=dtstart)
        occurrences = list(rule.between(lo, hi, inc=True))
    except Exception:
        return []

    if naive:
        return [o.date() for o in occurrences]
    return [o.replace(tzinfo=JST) for o in occurrences]


def _exdates(component):
    """EXDATE（この回はナシ）の日付集合。"""
    raw = component.get("EXDATE")
    if raw is None:
        return set()
    items = raw if isinstance(raw, list) else [raw]
    out = set()
    for item in items:
        for dt in getattr(item, "dts", []):
            out.add(_recurrence_key(dt.dt))
    return out


def extract_plans(ics_text, first_day, last_day):
    """ICS本文から、対象期間の予定を日付ごとの行にして返す。

    戻り値: [{"date": date, "start_time": "09:00", "end_time": "10:00",
              "title": "デイ面接", "uid": "..."}]（日付・開始時刻順）
    """
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception:
        raise GoogleCalendarError("カレンダーの中身を読み取れませんでした。")

    masters = []
    overrides = []
    for comp in cal.walk("VEVENT"):
        status = str(comp.get("STATUS") or "").upper()
        if status == "CANCELLED":
            continue
        if comp.get("DTSTART") is None:
            continue
        if comp.get("RECURRENCE-ID") is not None:
            overrides.append(comp)
        else:
            masters.append(comp)

    # 変更された回（RECURRENCE-ID）は、元の繰り返しの同じ日を差し替える
    overridden = set()
    for comp in overrides:
        uid = str(comp.get("UID") or "")
        overridden.add((uid, _recurrence_key(comp.get("RECURRENCE-ID").dt)))

    rows = []

    def _emit(comp, start_value, end_value):
        title = _clean_title(comp.get("SUMMARY"))
        uid = str(comp.get("UID") or "")
        for row in _occurrence_rows(start_value, end_value, title):
            if first_day <= row["date"] <= last_day:
                row["uid"] = uid
                rows.append(row)

    for comp in masters:
        start_value = comp.get("DTSTART").dt
        end_prop = comp.get("DTEND")
        end_value = end_prop.dt if end_prop is not None else None
        uid = str(comp.get("UID") or "")

        if comp.get("RRULE") is None:
            _emit(comp, start_value, end_value)
            continue

        # 繰り返し予定: 1回ぶんの長さを保ったまま、開始日時だけずらして並べる
        span = None
        if end_value is not None:
            s_date, _ = _as_local(start_value)
            e_date, _ = _as_local(end_value)
            if isinstance(start_value, datetime.datetime) and isinstance(
                    end_value, datetime.datetime):
                span = end_value - start_value
            else:
                span = e_date - s_date

        skip = _exdates(comp)
        for occ_start in _expand_rrule(comp, start_value, first_day, last_day):
            key = _recurrence_key(occ_start)
            if key in skip or (uid, key) in overridden:
                continue
            occ_end = (occ_start + span) if span is not None else None
            _emit(comp, occ_start, occ_end)

    for comp in overrides:
        start_value = comp.get("DTSTART").dt
        end_prop = comp.get("DTEND")
        _emit(comp, start_value, end_prop.dt if end_prop is not None else None)

    rows.sort(key=lambda r: (r["date"], r["start_time"] or "99:99", r["title"]))
    return rows


def import_month(url, first_day, last_day):
    """限定公開URLから対象期間の予定を取ってくる（取得＋解釈をまとめたもの）。"""
    return extract_plans(fetch_ics(url), first_day, last_day)
