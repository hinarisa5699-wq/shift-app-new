# シフト自動作成くん

介護施設向け 1か月分シフト自動作成 Web アプリ。
OR-Tools CP-SAT ソルバーで制約充足最適化を行い、Flask + SQLite でローカル動作する。

## クイックスタート

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5050
```

Windows の場合は `run.bat` をダブルクリック。

---

## ファイル構成

| ファイル | 行数 | 責務 |
|----------|------|------|
| `app.py` | 1223 | Flask ルーティング、CRUD API、シフト生成の起動 |
| `solver.py` | 1760 | CP-SAT ソルバー本体（介護/調理を独立解決→マージ） |
| `export.py` | 625 | Excel (.xlsx) / CSV エクスポート |
| `models.py` | 409 | SQLAlchemy モデル定義（11テーブル） |
| `config.py` | 11 | Flask / SQLAlchemy 設定 |
| `seed_aloha.py` | — | アロハ施設の実運用データ投入 |
| `seed_staff.py` | — | 汎用テスト職員データ投入 |
| `test_solver_latest_requirements.py` | — | ソルバーの自動テスト |

### テンプレート (templates/)

| ファイル | 画面 |
|----------|------|
| `base.html` | 共通レイアウト（ナビゲーション） |
| `index.html` | トップ（年月選択→生成ボタン） |
| `calendar.html` | シフトカレンダー表示 + Excel/CSVダウンロード |
| `staff_list.html` | 職員一覧 |
| `staff_form.html` | 職員登録/編集フォーム |
| `settings.html` | 条件設定（最低人数、営業曜日等） |

### 静的ファイル (static/)

- `css/style.css` — アプリ全体のスタイル
- `js/app.js` — カレンダー操作、非同期通信、UI制御

---

## ソルバー設計 (solver.py)

### 二段構成

1. **介護ソルバー**: `_solve_care()` — 介護職員のシフトを決定
2. **調理ソルバー**: `_solve_cooking()` — 調理職員のシフトを決定
3. `generate_shift()` で両者をマージして返す

### 介護シフトパターン

```
CARE_ASSIGNMENTS = [
    "off",               # 休み
    "day_pattern1",      # デイ① 8:30-17:30（フルタイム）
    "day_pattern2",      # デイ② 9:00-16:00（時短）
    "day_pattern3",      # デイ③ 8:30-12:30（午前半日）
    "day_pattern4",      # デイ④ 13:30-17:30（午後半日）
    "visit_am",          # 訪問午前
    "visit_pm",          # 訪問午後
    "day_p3_visit_pm",   # 兼務A: ③デイ→PM訪問（休憩12:30-13:30）
    "visit_am_day_p4",   # 兼務B: AM訪問→④デイ（休憩12:00-13:00）
]
```

### 在籍判定グループ

| 定数 | 内容 | 使用箇所 |
|------|------|----------|
| `PRESENT_AT_9` | 9時に事業所にいるパターン | min_staff_at_9 制約 |
| `PRESENT_AT_15` | 15時に事業所にいるパターン | min_staff_at_15 制約 |
| `PRESENT_FULL_DAY` | 9時〜16時通しで在籍 | 電話当番候補 |
| `DAY_AM_ASSIGNMENTS` | 午前デイに寄与 | 午前人数制約 |
| `DAY_PM_ASSIGNMENTS` | 午後デイに寄与 | 午後人数制約 |

### 主要な制約一覧

| カテゴリ | 制約 | ハード/ソフト |
|----------|------|--------------|
| 人数 | デイ最低/最大人数 (9時/15時) | ハード |
| 人数 | 訪問最低人数 (AM/PM) | ハード |
| 人数 | 兼務者最低人数/日 | ハード |
| 労務 | 連勤上限 | ハード |
| 労務 | 週勤務日数上限/下限 | ハード |
| 労務 | 休み希望 | ハード |
| 労務 | 祝日NG職員の自動休み | ハード |
| 配置 | 看護師/PTはデイ4名から除外 | ハード |
| 配置 | 男性午前優先 | ハード/ソフト切替可 |
| 配置 | 電話当番 (社員のみ、連続禁止) | ハード |
| 配置 | 許可パターン制限 | ハード |
| 調理 | 組み合わせルール (①②③ or ③④) | ハード |
| 均等化 | 出勤日数の均等化 | ソフト |
| 均等化 | 休憩スロットの月間均等配分 | ソフト |
| 均等化 | 相談員ローテーション均等 | ソフト |

### ポストプロセス

ソルバー解決後に以下を付与:
- `_assign_break_times()` — 休憩時間のずらし割り当て
- `_assign_counselor_rotation()` — 相談員2時間ローテーション

---

## DBスキーマ (models.py)

```
staff                    ← 職員マスタ
├── day_off_request      ← 休み希望 (1:N)
├── generated_shift      ← 生成結果 (1:N)
├── staff_qualification  ← 保有資格 (M:N via qualification)
└── staff_allowed_pattern ← 許可パターン制限 (1:N)

shift_settings           ← シフト条件設定（1レコード）
shift_warning            ← ソルバー警告
shift_pattern            ← シフトパターン定義マスタ
qualification            ← 資格マスタ
placement_rule           ← 配置ルール（汎用）
cooking_combo_rule       ← 調理組み合わせルール
```

### staff テーブルの主要カラム

| カラム | 型 | 説明 |
|--------|------|------|
| `name` | String | 氏名 |
| `employment_type` | String | 常勤/時短正社員/パート/管理者 |
| `staff_group` | String | "care" or "cooking" |
| `can_visit` | Boolean | 訪問兼務可否 |
| `gender` | String | male/female/空 |
| `has_phone_duty` | Boolean | 電話当番対象 |
| `holiday_ng` | Boolean | 祝日出勤不可 |
| `max_consecutive_days` | Integer | 連勤上限 |
| `max_days_per_week` | Integer | 週勤務日数上限 |
| `available_days` | String | 勤務可能曜日 (CSV: 0=月〜6=日) |
| `available_time_slots` | String | full_day/am_only/pm_only |
| `weekend_constraint` | String | 空/one_off（土日どちらか休み） |

### generated_shift テーブル

| カラム | 説明 |
|--------|------|
| `generation_id` | UUID（1回の生成で共通） |
| `assignment` | アサインメントコード |
| `break_start` | 休憩開始時刻 (e.g. "12:00") |
| `counselor_desk_slots` | 相談スロットインデックス JSON |
| `is_phone_duty` | 電話当番フラグ |

---

## Excel出力 (export.py)

- **2シート構成**: 介護シート + 調理シート
- 各日×各職員のセルにアサインメントラベルを表示
- セル内テキストで休憩時間・相談時間を追記
- 祝日はオレンジ色 + 祝日名表示
- ヘッダーに資格情報、フッターに出勤日数
- A4横・印刷最適化設定済み

---

## 操作マニュアル

詳細は `docs/操作マニュアル.md` を参照。

基本フロー:
1. 職員登録（名前、勤務条件、資格、兼務可否）
2. 休み希望登録（カレンダーで日付クリック）
3. 条件設定（最低人数、営業曜日等）
4. シフト生成（ボタン1つ、約1分）
5. カレンダーで確認 → Excel/CSVダウンロード
