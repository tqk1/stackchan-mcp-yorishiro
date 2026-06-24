# 2026-06-22 自発的会話エンジン（Hermes 自発判断層）実装

ブランチ: `feature/proactive`（`feature/multiturn` = presence v2 から分岐）
ステータス: **コード実装完了・pytest 1014 passed / ruff clean。実機 E2E は未実施（要 razer-server デプロイ）。未コミット。**

---

## 1. 作業概要（なぜ・何を）

ケンジさんの要望「在室管理の精度が上がったので自発的な会話をさせたい」を受け、Phase D の核心で未実装だった **Hermes 自発判断層（状態遷移駆動の自発発話）** を実装した。

要望は元々4つ（①Obsidian知識源 / ②自発会話 / ③不在時Eufy掃除 / ④朝のTV提案）あったが、②が③④の共通基盤であること、③④①にはそれぞれ別の判断（プライバシー・Eufy方式・TV登録）が必要なことから、**今回は②に集中**することにした（ケンジさん判断）。③④①の決定事項は計画ファイル末尾に記録済み。

設計の骨子（既存 worklog/memory で確立済みの方針を踏襲）:
- **観測＝gateway が機械的に**：在室状態の「意味ある遷移」を gateway が検出・フィルタ
- **判断（言葉）＝Hermes**：遷移発火時に gateway が Hermes に「状況を伝えて一言生成」を依頼
- **設計原則①（夫婦の会話に割り込まない）厳守**：既存 heartbeat の多層ガードを全て通す
- **設計原則④（Hermes は reactive のまま）**：Hermes 本体は無改造、gateway がトリガーするだけ
- **opt-in**：env 未設定なら完全無効

### ブランチ判断（重要）
着手前に「presence v1（develop/feature/eufy）」と「presence v2 就寝ラッチ（feature/multiturn 未マージ）」の分岐に気づいた。**朝の起床トリガー（QUIET→ACTIVE）は v2 の就寝ラッチに直接依存する**（v1 ではじっと寝た人を ABSENT と誤判定し「起床」と「帰宅」を区別できない）。そのため `feature/multiturn` から `feature/proactive` を分岐して v2 の上に実装した（ケンジさん選択肢A）。他機能（multiturn 会話）のマージ状態には触れていない。

---

## 2. 構成図

```
PresenceMonitor (presence.py, v2)
  _poll_once() → _set_state(new, notify=True)
     │  new != old のとき create_task(_fire_change(old,new))  ← 投げっぱなし・例外握り潰し
     ▼  register_on_state_change で登録された observer を呼ぶ
ProactiveSpeaker.on_state_change(old, new)   (proactive.py 新規)
  ├ control.proactive_enabled()  False なら return（ダッシュボードトグル・実行時）
  ├ 遷移フィルタ _match_transition：ABSENT→ACTIVE / QUIET→ACTIVE のみ
  │   （src が UNKNOWN の遷移は定義上存在しない＝起動直後/再接続は無音）
  ├ _skip_reason()：device / voice_turn_active / multiturn gap / tts_lock /
  │   recording / quiet hours / room empty / cooldown / daily cap
  ├ _refire_suppressed(key)：同種遷移の再発火クールダウン（既定10分・in-memory）
  ├ ask_hermes(状況文, system_prompt=PROACTIVE_SYSTEM_PROMPT)   ← 言葉は Hermes
  └ _perform_speak：set_avatar(happy) → synthesize_and_send → set_avatar(idle)
                    → 日次カウント++（atomic 永続化）
```

`update_config`（ダッシュボードのしきい値変更）由来の状態変化は `notify=False` で observer を呼ばない＝再チューニングを「起床/帰宅」と誤認しない。

---

## 3. 変更ファイル

新規:
- `gateway/stackchan_mcp/proactive.py` — `ProactiveSpeaker` / `ProactiveConfig` / `PROACTIVE_SYSTEM_PROMPT` / `_ALL_TRANSITIONS`
- `gateway/tests/test_proactive.py` — 24 ケース

改修:
- `presence.py` — `register_on_state_change` / `_set_state(notify=)` / `_fire_change`、`_poll_once`・`update_config` の `_state` 代入を `_set_state` 経由に
- `hermes_bridge.py` — `ask_hermes` に `system_prompt` キーワード追加（後方互換・既存 `session_id` と並列）
- `gateway.py` — `_proactive` 属性、`start()` で `from_env` 生成し presence に observer 登録、`stop()` で参照解放
- `control.py` — `_default_proactive()`（env シード）、`load_state`/`save_state` に `proactive_enabled` キー、`proactive_enabled()` / `set_proactive_enabled()`
- `http_server.py` — `control_proactive` ハンドラ + `/control/proactive` ルート + `_build_control_status` に `proactive` ブロック
- `tests/test_control.py`・`test_hermes_bridge.py`・`test_http_server.py`・`test_presence.py` — 既存の dict 完全一致アサート2件を新キー対応に更新＋新規ケース追加

---

## 4. 用語解説

- **状態遷移フック (state-change hook)**: presence が状態を更新する瞬間にコールバックを発火する仕組み。本実装で `register_on_state_change` として新設。observer は同期/非同期どちらも可で、`_fire_change` 内で例外を握り潰す（監視ループを落とさない）。
- **就寝ラッチ (sleep latch, `_asleep`)**: presence v2 の機構。就寝時間帯に在室を確認したら立ち、起床時間帯にクリア。じっと寝た人（TMOS が動く温源しか見えない）を夜通し QUIET に保持し、「就寝」と「外泊」を区別する。朝の `QUIET→ACTIVE` がこのラッチ解除に紐づくため、自発の「おはよう」が意味を持つ。
- **refire cooldown（再発火クールダウン）**: 同じ種類の遷移（例 ABSENT→ACTIVE）が短時間で往復しても1回しか喋らせないための in-memory タイマー。monotonic 時刻は再起動で無意味になるため永続化しない（再起動直後の1回の再挨拶は許容）。日次カウントは wall-clock 日付なので永続化する。
- **opt-in master switch（`STACKCHAN_PROACTIVE`）とダッシュボードトグル（`proactive_enabled`）の二段**: env が「スピーカーを作るか」を、ダッシュボードが「実行時に喋らせるか」を制御。env を seed に既定値を決め、永続トグルが実行時の真実（multiturn トグルと同流儀。ダッシュボード OFF が env ON に勝つ）。

---

## 5. 検証

- **pytest 1014 passed / ruff clean**（`gateway/` で `.venv/bin/python -m pytest -q` + `ruff check stackchan_mcp tests`）
- test_proactive.py の最重要回帰: **`UNKNOWN→ACTIVE` で発火しない**（起動直後/device再接続の誤挨拶防止）。加えて全ガード・cooldown・refire・daily cap・atomic 永続化・Hermes失敗時の沈黙をカバー
- presence フック: 遷移発火・無変化で不発・`update_config` で不発（notify=False）・observer例外の握り潰し・async observer の await をカバー

### 未実施（要 razer-server）
実機 E2E（`STACKCHAN_PRESENCE_POLL_SEC` + `STACKCHAN_PROACTIVE=1` 設定 → `sudo systemctl restart stackchan-gateway`）:
1. 起動直後に無音 / 2. 退室→ABSENT確定 / 3. 帰室→「おかえり」1回・即往復で再発話なし / 4. 会話中に割り込まない / 5. quiet hours で不発 / 6. `/control/proactive` OFF→沈黙・ON→復帰・再起動後維持

→ **learning-report は実機 E2E 完了後に作成**（E2E の所見を含めて確定させるため）。

---

## 5b. ダッシュボード UI（2026-06-22 追加・リポジトリ外）

`~/razer-dashboard/dashboard.html`（非 git・本リポジトリ外）の **🧠 応答モード**カードに **🗣️ 自発会話**トグルを追加。force_hermes / multiturn トグルと完全に同じ idiom:
- HTML: `sc-proactive` スイッチ + `sc-proactive-hint`
- render: `GET /control/status` の `proactive.{enabled,available}` を消費。`available=false`（`STACKCHAN_PROACTIVE` 未設定）なら注記を出すだけ（トグルは無効化しない＝force_hermes の `local_enabled` 注記と同 idiom）
- change: `POST /control/proactive {proactive_enabled}`、失敗時はトグルを戻す
- 検証: インライン JS 全体（912行）を `node --check` で構文 OK。視覚・実動作確認は実機 E2E（明日）で。

## 6. 残課題・次

- **実機 E2E（明日）**→ green 後に commit + learning-report
- 後続フェーズ（計画ファイル末尾に決定事項記録済み）: ④朝のTV提案（②基盤＋SwitchBot IR）/ ①Obsidian知識源（vault.py・Hermesに関連箇所のみ）/ ③Eufy掃除（既存設定の調査から）

---

## 7. セッション引き継ぎ（2026-06-22 clear 前・★次セッションはここから）

### 状態
- **②自発会話: コード実装完了・pytest 1014 passed / ruff clean・ダッシュボードトグル追加(node --check OK)。すべて未コミット（ブランチ `feature/proactive`）。**
- 新規 `proactive.py` / `test_proactive.py`、改修 `presence.py`/`hermes_bridge.py`/`gateway.py`/`control.py`/`http_server.py` + 既存テスト調整。dashboard.html（非git）も編集済。

### デプロイ状況（トグルが「動かない」の原因＝デプロイ未反映）
- 動いている gateway は**古いコード**（systemd `stackchan-gateway`・2026-06-21 07:25 起動）。新 `/control/proactive` ルートと status の `proactive` ブロックを持たない → POST 404・状態未同期。
- サービスは **editable install**（import 元 = `gateway/stackchan_mcp/__init__.py` のソースツリー）→ **リスタートで新コードが反映される**。
- `STACKCHAN_PROACTIVE` は**未設定**（drop-in は heartbeat/local-llm/multiturn のみ）。未設定だと再起動しても `available=false` でトグル無効。
- **sudo はパスワード必要**（セッションからは実行不可）。ユーザーに `!` 実行を依頼する。

### ★トグルを有効化する手順（ユーザーが sudo で実行）
```
printf '[Service]\nEnvironment=STACKCHAN_PROACTIVE=1\n' | sudo tee /etc/systemd/system/stackchan-gateway.service.d/proactive.conf
sudo systemctl daemon-reload && sudo systemctl restart stackchan-gateway
```
→ ダッシュボード再読込で 🗣️自発会話トグルが ON（available=true）。朝の「おはよう」(QUIET→ACTIVE) も有効化。**この手順はまだユーザー未実行（clear のため保留）**。

### 在室モード つうじょう/おやすみ（ユーザー要望・2026-06-22）
- 要望: 在室状況に合わせモードを つうじょう/おやすみ に。6:30-22:00=つうじょう / 22:00-6:30=おやすみ。
- **検出は既に実装・稼働済み**: `~/.stackchan/presence_state.json` の `sleep_window="22:00-06:30"`（確認済）で presence が ACTIVE(つうじょう)/QUIET(おやすみ=在室×夜)/ABSENT(不在) を判定中。要望の時間割と完全一致。
- **★未決定（clear で中断・次セッションで再質問）**: 「おやすみモードで何をするか」=
  1. **挨拶だけ（おはよう/おやすみ）** ← 推奨。在室で 6:30→「おはよう」(既存)、22:00→「おやすみ」(新規)。
  2. 挨拶＋見た目（おやすみ中は画面暗く/LED 落ち着いた色、つうじょうで戻す。dashboard 明るさ/LED 流用）。
  3. 見た目だけ（喋らず画面/LED のみ夜モード）。
- 実装メモ（①採用時）: `proactive.py` の `_ALL_TRANSITIONS` に `active_quiet`（ACTIVE→QUIET・「おやすみ」）を追加し、`STACKCHAN_PROACTIVE_TRANSITIONS` 既定に含める。**注意: 22:00 の「おやすみ」は proactive の quiet 窓(既定22:00-06:30)に被って抑制される** → その遷移だけ quiet-hours ガードを例外にする実装が必要（朝の「おはよう」は 6:30=quiet 終了直後なので問題なし）。

### 次アクションの順序
1. ユーザーが上記 sudo 手順を実行 → トグル/おはよう を実機確認。
2. おやすみモードの方針（①/②/③）回答 → 実装（①なら active_quiet 追加 + quiet 例外）。
3. 実機 E2E（§5 の6項目）→ green 後に `feature/proactive` を commit + learning-report 作成。

関連: 計画 `~/.claude/plans/obsidianvault-kenji-obsidian-web-stack-buzzing-thacker.md`、メモリ `project_phase_bd_deferred`。
