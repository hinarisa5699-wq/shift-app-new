"""画面のJavaScriptが壊れていないかの確認。

2026-08: 変数名の直し漏れ（isOffice）でカレンダーが表示できなくなった。
Node があれば構文チェックを行い、あわせて「宣言していない変数を使っていないか」を
関数単位の簡易チェックで見る（画面を開かなくても気づけるようにする）。
"""
import re
import shutil
import subprocess

import pytest

APP_JS = "static/js/app.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node が無い環境ではスキップ")
def test_app_js_syntax_is_valid():
    res = subprocess.run(["node", "--check", APP_JS], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:500]


def test_app_js_uses_no_undeclared_local_names():
    """「_ か is で始まるその場限りの変数」が宣言されているかだけを見る。

    ここで拾いたいのは名前を付け替えたときの直し漏れ。ブラウザ標準の名前まで
    見ようとすると誤検出だらけになるので、対象をローカル変数の書き方に絞る。
    """
    src = open(APP_JS, encoding="utf-8").read()
    declared = set(re.findall(r"\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", src))
    for args in re.findall(r"function\s*[\w$]*\s*\(([^)]*)\)", src):
        declared |= {a.strip().split("=")[0].strip() for a in args.split(",") if a.strip()}
    for args in re.findall(r"\(([^()]*)\)\s*=>", src):
        declared |= {a.strip().split("=")[0].strip() for a in args.split(",") if a.strip()}
    declared |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", src))

    used = set(re.findall(r"(?<![.\w$])(_[A-Za-z][\w$]*|is[A-Z][\w$]*)(?![\w$]*\s*:)", src))
    missing = sorted(u for u in used if u not in declared)
    assert not missing, f"宣言していない変数を使っています: {missing}"
