"""アロハハウスアプリ（入居者記録）の予定シートから、職員の訪問予定を取り込む。

ユーザー依頼 2026-10:「カイポケから池田友子の予定同期させる。アロハハウスアプリから
同期後、シフトへも同期するようにできる？」

流れ: カイポケ →（Chrome拡張・毎晩22時）→ アロハハウスアプリ（GAS＋スプレッドシート）
      → このモジュール → 職員のスケジュール（StaffPlan, source="kiroku"）

向きは一方通行（読み取りのみ）。アロハハウスアプリには何も書かない・消さない。
予定シートの「担当」に職員のフルネームが入っている行だけを、その職員の予定にする
（例「池田友子・前垣茜」なら両方の予定）。対象は職員マスタでチェックを入れた職員だけ。

アロハハウスアプリに入っているのはアロハハウス入居者への訪問だけなので、
それ以外のお宅への訪問はここからは入らない。
"""

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# GASのウェブアプリURLだけを受け付ける（社内URL等を打たせる誤用＝SSRFを防ぐ）
_URL_RE = re.compile(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$")

# StaffPlan.title の桁数に合わせる
_TITLE_MAX = 40
# 受け取る応答の上限（1か月ぶんの全予定・記録でも数MBに収まる）
_MAX_BYTES = 10 * 1024 * 1024


class KirokuError(Exception):
    """取り込みに失敗したときのエラー（画面にそのまま出せる日本語文言）。"""


def normalize_url(raw):
    """入力されたURLを検証して返す。空欄なら ""（連携なし）。"""
    url = (raw or "").strip()
    if not url:
        return ""
    if not _URL_RE.match(url):
        raise KirokuError(
            "アロハハウスアプリのURLは https://script.google.com/macros/s/…/exec の形で"
            "貼ってください（アロハハウスアプリの「設定」に入っているGASのURLです）。")
    return url


def _norm_name(s):
    """名前の比較用（空白を除き、異体字をそろえる）。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"\s+", "", s)
    return s.replace("髙", "高").replace("﨑", "崎").replace("邊", "辺").replace("邉", "辺")


def fetch_month_schedules(url, token, ym, timeout=40):
    """予定シートのうち、その月（"YYYY-MM"）の行を全部返す。"""
    body = json.dumps({"action": "month", "token": token or "", "ym": ym})
    full = url + "?p=" + urllib.parse.quote(body)
    req = urllib.request.Request(full, headers={"User-Agent": "shift-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise KirokuError(
            "アロハハウスアプリに接続できませんでした（HTTP {}）。"
            "設定のURLを確認してください。".format(e.code))
    except (urllib.error.URLError, OSError, TimeoutError):
        raise KirokuError(
            "アロハハウスアプリに接続できませんでした。時間をおいて試してください。")
    if len(raw) > _MAX_BYTES:
        raise KirokuError("アロハハウスアプリの予定が多すぎて取り込めませんでした。")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise KirokuError(
            "アロハハウスアプリの応答が読み取れませんでした。時間をおいて試してください。")
    if not data.get("ok"):
        err = str(data.get("error") or "")
        if "合言葉" in err:
            raise KirokuError(
                "アロハハウスアプリの合言葉が違います。設定の「合言葉」を確認してください。")
        raise KirokuError("アロハハウスアプリでエラーになりました: {}".format(err[:100]))
    rows = ((data.get("data") or {}).get("schedules")) or []
    return [r for r in rows if isinstance(r, dict)]


def _time(value):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(value or "").strip())
    return "{:02d}:{}".format(int(m.group(1)), m.group(2)) if m else ""


def plans_for_staff(rows, staff_name):
    """予定シートの行から、担当にその職員が入っている予定を取り出す。

    戻り値: [{"date": "YYYY-MM-DD", "start_time", "end_time", "title", "uid"}]
    （日付・開始時刻順）
    """
    me = _norm_name(staff_name)
    if not me:
        return []
    out = []
    for r in rows:
        names = [_norm_name(x) for x in re.split(r"[・、,/／]", str(r.get("担当") or ""))]
        if me not in names:
            continue
        d = str(r.get("日付") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        what = " ".join(str(r.get("内容") or r.get("種別") or "訪問").split())
        who = " ".join(str(r.get("氏名") or "").split())
        title = "{} {}様".format(what, who) if who else what
        out.append({
            "date": d,
            "start_time": _time(r.get("開始")),
            "end_time": _time(r.get("終了")),
            "title": title[:_TITLE_MAX],
            "uid": str(r.get("id") or ""),
        })
    out.sort(key=lambda x: (x["date"], x["start_time"] or "99:99", x["title"]))
    return out
