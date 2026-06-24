# worklog 2026-06-24 — おやすみ／つうじょう モード自動切替＋挨拶（feature/proactive）

## 1. このセッションでやったこと（概要）

presence の状態遷移に「**モードプリセットの自動適用**」を相乗りさせ、挨拶と同時にモード切替するようにした。
ユーザーがダッシュボードで作成済みのプリセット（`おやすみ`/`つうじょう`）を再利用する。前回（2026-06-22）実装した
proactive（自発会話）の拡張で、ブランチは `feature/proactive` のまま。

### きっかけ（ケンジ要望 2026-06-24）
「ダッシュボードからモードを自分で作成できるようにした。時間と在室状況をもとにモード変更してほしい。
22時になったら『おやすみ』と言ってモード変更、6:30になったらモード変更して『おはよう』」

### 確定した方針
| 遷移 | きっかけ | 動作 | 順序の理由 |
|---|---|---|---|
| `active_quiet`（新規）| 22:00 在室→就寝(ACTIVE→QUIET) | 「おやすみ」→ **おやすみ**適用 | 先に喋る（おやすみはミュートするので発話前に喋り切る）|
| `quiet_active`（既存）| 6:30 起床(QUIET→ACTIVE) | **つうじょう**適用 →「おはよう」 | 先に unmute →喋る |
| `absent_active`（既存）| 帰宅(ABSENT→ACTIVE) | **つうじょう**適用 →「おかえり」 | 同上（夜ミュート帰宅でも音が戻る・ケンジ確定）|

- 境界時刻に会話中ならその回はスキップ許容（ケンジ確定）。会話中/録音中/TTS中は既存ガードで**モード切替も挨拶も両方抑止**（設計原則①＝勝手にミュート/暗転しない）。
- モード適用は **best-effort**：プリセット欠如/失敗でも挨拶は出る。Hermes 失敗でも夜モードへの切替は実行（夜モードは確実な側）。

## 2. 構成図（追加分）

```
ProactiveSpeaker.on_state_change(old, new)   (proactive.py)
  ├ control.proactive_enabled() ガード（ダッシュボードトグル）
  ├ _match_transition → _Transition（preset_role: day/night/None, preset_first, exempt_quiet_hours）
  ├ _skip_reason(transition)：device/voice_turn/multiturn/tts_lock/recording/quiet*/room/cooldown/cap
  │     *quiet hours は transition.exempt_quiet_hours=True（active_quiet）でスキップ
  ├ refire 前倒しスタンプ（モード切替＋挨拶の両方をカバー）
  └ 順序制御:
       preset_first=True (day) : _apply_mode → _speak     （unmute先・帰宅/起床）
       preset_first=False(night): _speak → _apply_mode     （喋り切ってからミュート・就寝）

  _apply_mode(t): role→名前解決(day_preset/night_preset)→ control.apply_preset(best-effort・例外/失敗ログのみ)
  _speak(t)     : ask_hermes(situation, system_prompt=PROACTIVE_SYSTEM_PROMPT) → _perform_speak（失敗=沈黙）
```

## 3. 変更ファイル（feature/proactive・未コミット）

- `gateway/stackchan_mcp/proactive.py`:
  - `_Transition` に `preset_role` / `preset_first` / `exempt_quiet_hours` フィールド追加
  - `_ALL_TRANSITIONS` に `active_quiet`（ACTIVE→QUIET・night・speak-first・quiet例外）追加、既存2件に role=day 付与
  - `DEFAULT_TRANSITIONS = "absent_active,quiet_active,active_quiet"`
  - `DEFAULT_DAY_PRESET="つうじょう"` / `DEFAULT_NIGHT_PRESET="おやすみ"` 定数 + `ProactiveConfig.day_preset/night_preset` + env `STACKCHAN_PROACTIVE_DAY_PRESET`/`_NIGHT_PRESET`（空文字でその側の切替を無効化＝挨拶のみ）
  - `on_state_change`：refire スタンプを前倒し → preset_first で順序分岐。`_speak`/`_apply_mode`/`_preset_for` を新設
  - `_skip_reason(transition)`：引数化、exempt 時 quiet をスキップ
  - `_perform_speak`：refire スタンプを撤去（on_state_change に移動）
- `gateway/tests/test_proactive.py`:
  - `install_stubs` に `control.apply_preset` スタブ + `modes`/`seq` レコーダ + `apply_ok` 引数
  - 既存修正：from_env 既定遷移3件・day/night_preset 既定／非対象遷移から (ACTIVE,QUIET) 除外
  - 新規8ケース：active_quiet が quiet中発火＋speak→mute順・day遷移は mode→speak順・会話中は両方抑止・モード失敗でも挨拶・Hermes失敗でも夜モード適用・空プリセット名で切替無効

env・systemd drop-in は変更不要（既定で active_quiet 有効・プリセット名が既存に一致）。dashboard も変更不要（既存 🗣️自発会話トグル1つで全体 ON/OFF）。

## 4. 検証

- **pytest 1020 passed / ruff clean**（`gateway/` で `.venv/bin/python -m pytest -q` + `ruff check stackchan_mcp tests`）
- import 健全性・遷移テーブルを直接確認済（active_quiet: active→quiet, night, first=False, exempt=True）。
- **デプロイ済**：ケンジが `sudo systemctl restart stackchan-gateway` 実施（06:49:25 起動）。新コード反映・`proactive.available=true`・`esp32_connected=true` 確認。

## 5. ★実機 E2E（夜に実施・ここから再開）

ケンジが朝に外出のため夜に持ち越し。**コード/テスト/デプロイは完了**。夜は「トグル ON →誘発→観測」のみ。

### 前提・現在のクリーン状態（テスト残渣なし）
- proactive トグル = **OFF**（外出中の無人発火防止のため戻した）。夜に ON にする。
- sleep_window = `22:00-06:30`（実値）/ absent_after_s = `1080`（実値）。**操作したら必ずこの値に復元**。
- proactive_state.json = 無し（本日未発話・daily cap 4 は余裕）。

### ★タイミングの肝（重要）
- **おやすみ（active_quiet）は quiet 例外**＝実時刻に関係なく誘発可。
- **おはよう（quiet_active）は proactive quiet 窓(22:00-06:30)の対象**＝実時刻が 22:00-06:30 の**外**でないと抑止される（＝夜中に「おはよう」と言わない正しい挙動）。
  → **両方テストするなら 22:00 より前（例 20:00-21:45）に実施するのが理想**。22:00 以降だと おやすみ のみ誘発可（おはよう は翌朝 or `STACKCHAN_PROACTIVE_QUIET` 一時変更＝要 restart）。

### 手順（私が :8080 経由で駆動・sudo不要）
1. トグル ON：`curl -s -X POST http://127.0.0.1:8080/control/proactive -d '{"proactive_enabled": true}'`
2. ケンジが StackChan 正面（FoV内）に座る → `GET /control/presence` で `state=active` を確認。
   （UNKNOWN→ACTIVE は非対象遷移なので、座っても挨拶は出ない＝正しい）
3. **おやすみ誘発**：sleep_window を「今」を含む値に（例 今21:30 なら `{"sleep_window":"21:00-23:30"}` を `POST /control/presence/config`）→ 次ポーリング(5s)で ACTIVE→QUIET → 「おやすみ」発話**後**に画面暗転＆ミュート。
   - 観測：`/control/status` の `muted=true / volume=0 / brightness=15`、ケンジが「おやすみ」を聞く。
   - **★語尾切れチェック**：発話後にミュートするので「おやすみ」末尾が切れないか確認。切れたら proactive.py の night 適用前に小さなガード遅延（TTS 末尾分）を追加する。
4. **おはよう誘発**：sleep_window を実値へ（22:00 前なら `{"sleep_window":"22:00-06:30"}`、22:00 後なら今を含まない窓 例 `{"sleep_window":"02:00-05:00"}`）→ QUIET→ACTIVE → つうじょう適用(unmute/bright=75)**後**に「おはよう」。
5. （任意）**帰宅誘発**：`{"absent_after_s": 15}` に一時短縮 → ケンジが FoV から外れて ~20s →（last_seen が伸びて ABSENT）→ 戻る → ACTIVE → 「おかえり」＋つうじょう。
6. **復元**：`{"sleep_window":"22:00-06:30","absent_after_s":1080}` を POST。`/control/presence` で実値を確認。
7. green 後：commit（feature/proactive）+ learning-report（ケンジに作成可否は確認済＝作る予定）。

### コミット予定の中身
- proactive.py / test_proactive.py（上記）。dashboard.html（非git）は今回変更なし。
- メッセージ案：`feat(gateway): proactive mode auto-switch — おやすみ/つうじょう presets on presence transitions`

## 7. E2E 第1ラウンド結果 + 「間」の追加（2026-06-24 夜・21:0x）

### ★遷移を実時刻待ちせず誘発する技（重要・将来のE2Eで再利用）
`update_config`（sleep_window 変更）は **eager+silent**（394行 `_set_state(_derive_state(), notify=False)`）＝設定変更由来の状態変化では proactive observer を呼ばない（再チューニングを起床/帰宅と誤認しない意図的設計）。
→ **window 操作だけでは proactive は発火しない**。代わりに **「窓の境界を1〜2分先に置く」**:
- おやすみ誘発: 開始を ~2分先に（例 now 21:06 → `{"sleep_window":"21:08-23:55"}`）。eager は現在 ACTIVE のまま silent → **実時刻が 21:08 を跨いだ通常ポーリング(notify=True)で ACTIVE→QUIET 発火**（本番と同経路）。
- おはよう誘発: 終了を ~2分先に（now 含む窓）→ 境界跨ぎで QUIET→ACTIVE。
- 復元: 実値 `22:00-06:30` に戻すと eager で ACTIVE に silent 復帰。**device は別途 `/control/presets/apply つうじょう` で戻す**（state は silent なのでモードは戻らない）。
- 占有駆動（おかえり）は absent_after_s 短縮＋FoV 出入りで発火（こちらは通常ポーリング）。

### おやすみ E2E 結果 ✅
21:08:11 ACTIVE→QUIET 発火（vol100 のまま＝発話中）→ 21:08:17 muted/vol0/bright15（おやすみ適用）。
- ケンジ確認: Hermes が「**そろそろ休んで明日に備えましょう**」と発話（文脈的な自然文）・**語尾切れなし**（「〜ましょう」まで聴取）・画面暗転 ✅。
- **★語尾切れ対策のガード遅延は不要と確定**（synthesize_and_send が実時間ペーシングで送出済みのため）。

### ★ケンジ フィードバック → 「間」を実装
「発話の直後にモード変更（ミュート/暗転）すると唐突。**1〜2秒おいてから**が自然」。
→ 語尾切れ対策ではなく**演出の間**。`proactive.py` に `DEFAULT_MODE_DELAY_S=1.5` + `ProactiveConfig.mode_switch_delay_s` + env `STACKCHAN_PROACTIVE_MODE_DELAY_S`（0で即時）。`on_state_change` の発話↔モード適用の間に `_mode_switch_pause(preset)`（プリセット適用時のみ・0なら no-op）を挿入。day(apply→間→speak)/night(speak→間→apply) 両方に対称適用。
- test: seq に "delay" を挟む形へ更新＋遅延可変/0 即時の2ケース追加。**pytest 1022 / ruff clean**。
- **★未デプロイ**（要 `sudo systemctl restart stackchan-gateway`）。再起動後に おやすみ/おはよう を再誘発して「間」の自然さを確認 → green 後 commit + learning-report。

### 第1ラウンド後のクリーン状態
device=つうじょう復元・sleep_window=22:00-06:30・absent_after_s=1080・**トグル OFF**（旧コード自然発火防止）。再テスト時に ON。

## 6. フォローアップ（将来・今回スコープ外・ケンジ要望 2026-06-24）

「データを溜めて半自動で学習し精度を高めてほしい。**『その時間にいたか？』の確認を Discord で Hermes 経由で**してほしい」
→ 在室データ蓄積 → 曜日×時間帯の在室確率マップを半自動学習。Hermes が Discord で在室の真偽を聞いてラベル（教師信号）を集める。
→ memory `project_future_sensors`（曜日×時間帯マップ学習）/ `project_life_support_vision`（生活支援ビジョン）と統合する将来フェーズ。今回の commit には含めない。
```
