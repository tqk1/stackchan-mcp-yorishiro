# tasks/todo.md — 現役タスク

> 完了済みの Phase 0〜F 作業記録は `tasks/todo-archive-2026Q2.md` に移動した（原文のまま）。
> 各 Phase の詳細な振り返りは `docs/phase-a〜f-report.md` / `docs/worklog/` を参照。
> このファイルには **まだ生きている未完了項目** と **直近の作業文脈** だけを残す。

最終整理: 2026-06-15。直前ステータス: **ダッシュボード機能拡張プロジェクト 全5フェーズ実装完了**（フェーズ1〜3 `bf161b3`、フェーズ4 両方スキップ決着、フェーズ5 人間工学的仕上げ＝フル案・HTML-only を実装＋Playwright自動検証パス 2026-06-15）。残: ①ユーザー実機1往復チェック（=フェーズ2(h)積み残し同時クローズ）②learning-report（全5フェーズ）。それ以前: Phase F フォロー完了 + ② ウェイクワードはクローズ（タップ/背面なで運用）、Phase A〜E + C1 クローズ済み。詳細は `docs/phase-f-report.md` および archive の 2026-06-14 セッション群を参照。

---

## 現役タスク（まだやるべき生きた未完了項目）

### ★★ 着手中: 静止在室検知（object_raw 併用＋適応ベースライン） 2026-06-27

**背景**: 1m着座で静止すると、製品の presence アルゴリズムが体を背景吸収し `presence≈0` → デバウンス(1080s)だけで `active` 維持 → 18分で誤 ABSENT。生サーモパイル `object_raw` は静止体を保持していると判明（部屋ウォークスルー計測 2026-06-27）。

**計測結果（同時刻の空室基準比 Δ）**: 自分の席1m=+745 / 恵梨子席=+290 / 立ち=+260 / キッチン=+134 / ソファ=+29(死角) ／ 空室ノイズ sd≈±35。室温 30.9→31.6℃ で空室 object_raw が -8697→-9043（約-510/℃）連動 → **固定しきい値は不可・適応ベースライン必須**を実データで確定。

**設計**: `occupied = moving(presence>200/pres_flag) OR (object_raw − 適応ベースライン > マージン)`。ベースラインは不在判定中のみ object_raw を EMA 追従（室温ドリフト追従）・在室中は凍結（静止体を吸収しない）。`moving` 検知で armed、armed 中のみ static 判定（緩く温まる壁を誤ラッチしない）・disarm 時ベースラインを新鮮な空室値へ resync。劣化しても従来より悪化しない純増分。

- [x] presence.py: 定数・`_update_occupancy()`・`_poll_once` 配線・snapshot/_append_log に観測値(obj_baseline/obj_armed/static_present)追加
- [x] **設計修正（履歴検証で発覚）**: 当初の「在室中ベースライン凍結」は 9.7日ログ replay で **2.6日連続ラッチ**（古い凍結基準が日内ドリフトに追従できず張り付き）→ **非対称EMA常時追従**へ（下げ=速 alpha0.08/τ60s・上げ=遅 alpha0.004/τ40分）。再 replay: static点灯 53→15.4%・純増 31.7→6.3%・最長run 2.6日→**75.5分**・≥120分 0件・夜間01-04時 2-4%。`scratch`相当の replay.py は scratchpad に。
- [x] test_presence.py: cold start／arm→静止保持／disarm／空室追従／壁(no moving)非ラッチ／**長時間静止は最終的に解除(アンチラッチ)**（+9ケース）
- [x] pytest **1071 passed** + ruff clean
- [x] **実機 E2E green（2026-06-27 15:23 restart）**: 恵梨子席で9分静止→`active`維持・**309秒分救済**（埋め込み消失をstatic保持）/ arm ゲート（接近+186でも動作未確認なら非ラッチ）/ 離脱で即 disarm・再ラッチ0 / 室温30.9→32.3℃を baseline 追従。E2Eログ scratchpad/e2e.jsonl
- [x] learning-report 要否＝**作る**（ケンジ 2026-06-27）
- [x] worklog（`docs/worklog/2026-06-27-static-presence.md`）＋ learning-report（`docs/static-presence-report.md`・初学者向け・事件簿形式）作成
- [ ] commit（feature ブランチ）— ケンジ確認後
- フォローアップ候補: マージン/alpha のダッシュボード露出（現状は定数）・寒い季節の空室 object_raw を事後検証（τ_upは保持時間↔誤ラッチのトレードオフ）・本命1m席でのE2E（弱め恵梨子席で既にgreen）

---

### （クローズ済み・参考）おやすみ／つうじょう モード自動切替＋挨拶 2026-06-24 — ブランチ `feature/proactive`

**方針確定（ケンジ 2026-06-24）**: presence 遷移に「プリセット適用（モード切替）」を相乗りさせ挨拶と同時に行う。ユーザー作成 `おやすみ`/`つうじょう` プリセットを再利用。単一トグル（🗣️自発会話）で ON/OFF。帰宅でも つうじょう 適用・境界会話中のスキップ許容（確定）。

| 遷移 | きっかけ | 動作 | 順序 |
|---|---|---|---|
| active_quiet（新規）| 22:00 ACTIVE→QUIET | 「おやすみ」→ おやすみ適用 | 発話→適用（ミュート前に喋る）|
| quiet_active（既存）| 6:30 QUIET→ACTIVE | つうじょう適用 →「おはよう」 | 適用→発話（unmute後）|
| absent_active（既存）| 帰宅 ABSENT→ACTIVE | つうじょう適用 →「おかえり」 | 適用→発話 |

- [x] proactive.py: `_Transition` に preset_role/preset_first/exempt_quiet_hours 追加・active_quiet 遷移追加・DEFAULT_TRANSITIONS 更新・ProactiveConfig に day_preset/night_preset(env 上書き・既定 つうじょう/おやすみ)・_skip_reason(transition) で exempt 時 quiet スキップ・on_state_change で refire 前倒し＋順序制御＋_apply_mode(best-effort)
- [x] test_proactive.py: active_quiet(quiet中発火/順序/モード適用)・→ACTIVE で day 適用（+8 ケース）
- [x] pytest **1020 passed** + ruff clean
- [ ] 実機 E2E（restart 後・睡眠窓を一時操作で おやすみ→おはよう／任意で帰宅 おかえり）← **sudo restart 待ち**
- [ ] commit + learning-report

**フォローアップ（将来・今回スコープ外）**: 在室データ蓄積→曜日×時間帯の在室確率マップを半自動学習し精度向上／「その時間にいたか?」を Discord で Hermes 経由確認しラベル収集（教師信号）。memory `project_future_sensors`/`project_life_support_vision` の学習アイデアと統合。

### ★★ 着手中: マルチターン会話（Phase 1 MVP）2026-06-17  — ブランチ `feature/multiturn`

**プラン**: `~/.claude/plans/codex-fluffy-breeze.md`（承認済み）。調査=Explore×3＋Plan＋独立Claude赤チーム。
**設計確定**: 継続トリガー=Hermes応答末尾「？/?」/ 対象=route==hermesのみ / firmware改修なし(VAD自停止 PollTouchpad 再利用) / 二段方式(MVP=gateway sleep、保険=firmware continuation Phase4条件付き)。
**赤チーム指摘(必須対策)**: P0自己拾音(AEC off・TTS完了通知無し→duration_ms+マージン sleep＋実機確認) / P1 heartbeat再発(multiturn_active 別フラグ) / P1カウンタ競合(成功パス確定・ConnectionError握る) / P2文脈(API key依存・session id 日跨ぎ→Phase2)。

- [x] `multiturn.py` 新規: `MultiturnSession` dataclass + `should_continue()` 純関数 + reset/`is_gap_stale` 判定 + env getters
- [x] `gateway.py` `__init__` に `self.multiturn` / `self.multiturn_active=False`
- [x] `hermes_bridge.py` `_run_voice_turn` 成功パス末尾に `_maybe_continue`（is_enabled 先行ゲート / **固定 guard sleep**＝duration_ms 非依存に補正 / send_listen_state を try/except ConnectionError）
- [x] `hermes_bridge.py` `handle_voice_turn` finally: 継続中は表示クリアをスキップ / 入口で stale-gap リセット / 空transcriptで counter リセット
- [x] `heartbeat.py` `_skip_reason` に `multiturn_active` ゲート（`_multiturn_suppresses`＝stale で自己失効）
- [x] `control.py` `is_muted()` 薄アクセサ追加（routing_force_hermes ミラー）
- [x] 定数 env 化: `STACKCHAN_MULTITURN`(既定off) / `MAX_MULTITURN_TURNS`(4) / `MULTITURN_SESSION_TIMEOUT_S`(60) / `MULTITURN_TTS_GUARD_MS`(既定1000・安全側)
- [x] pytest: test_multiturn(新規) + test_hermes_bridge 継続マトリクス + test_heartbeat multiturn ゲート（**943 passed**・ruff clean）
- [x] worklog: docs/worklog/2026-06-17-multiturn-p1.md

#### Phase 2 文脈保持（会話単位 session id）2026-06-17 ✅ 実装完了

**確定（ユーザー）**: 既定 ON / 会話ウィンドウ 180s（`HERMES_SESSION_WINDOW_S`）/ `HERMES_SESSION_ID` を base 名前空間として `<base>-<uuid8>` にローテ / `HERMES_SESSION_WINDOW_S=0` で旧固定 id 挙動に復帰。Hermes 無改造・heartbeat 非共有・API key gating 維持。
**実害**: このデプロイは KEY も SESSION_ID も設定済み = 固定 id に全会話が日跨ぎ蓄積中だった。

- [x] `multiturn.py`: `MultiturnSession.session_id` + `conversation_id()` メソッド（窓内再利用/窓超過ローテ/window=0 無効）/ `session_window_s()` getter / `new_session_id(base)` 純関数 / `reset()` は session_id 温存
- [x] `hermes_bridge.py`: `ask_hermes(text, *, session_id=None)`（None で env フォールバック）/ `generate_reply` thread / `handle_voice_turn` 入口で会話 id 算出＋`last_activity` 毎ターン stamp / `_run_voice_turn(hermes_session_id=...)`
- [x] pytest 追加（test_multiturn +9 / test_hermes_bridge +5）＋既存フェイク更新（**955 passed**・ruff clean）
- [x] worklog: docs/worklog/2026-06-17-multiturn-p2.md

#### 残（Phase 1 + 2 共通）

- [x] commit 済み（`77883c3` feat(gateway): multi-turn voice — continuation + per-conversation Hermes context）
- [x] 実機 E2E: ①自己拾音ゲート ②文脈保持（`STACKCHAN_MULTITURN=1` 起動・worklog p2 に記録）

#### Phase 3 — 仕上げ（ダッシュボードトグル + UX）2026-06-17 着手

**スコープ確定（ユーザー 2026-06-17）**: ①ダッシュボード ON/OFF トグル+永続化 ②上限到達時の「タップして続けてね」字幕 UX を実装。継続ターン短縮 LISTEN_TIMEOUT は見送り（firmware 領域・Phase 4）。全完了後 learning-report 作成（Phase 1-3 まとめ）。
**設計判断（確定）**: 永続トグルが実行時の主制御。env `STACKCHAN_MULTITURN` は state ファイル不在時の初期既定値に降格（dashboard OFF が env=1 に勝つ＝直感的、既存 systemd drop-in も維持）。
**参照パターン**: `control.py:346-360` routing_force_hermes / `http_server.py:555-562` control_routing。字幕は既存 `control.set_device_subtitle`。

- [x] (1) `control.py`: `_default_multiturn()`(env 由来) + `load_state`/`save_state` に `multiturn`(bool) + `multiturn_enabled()`/`set_multiturn()`（routing_force_hermes ミラー）。docstring 更新
- [x] (2) `http_server.py`: `control_multiturn` エンドポイント（body `{"enabled": bool}`）+ `_build_control_status` routing ブロックに `multiturn` 追加 + Route 登録
- [x] (3) `hermes_bridge.py`: `_maybe_continue` のゲートを `multiturn.is_enabled()` → `control.multiturn_enabled()` に変更
- [x] (4) UX: `_maybe_continue` で「上限到達×質問」を検知し `multiturn_prompt_pending` を立てる → `handle_voice_turn` finally で字幕「タップして続けてね」表示（それ以外はクリア）。`gateway.py` に `self.multiturn_prompt_pending=False`。`multiturn.TAP_TO_CONTINUE_HINT` 定数
- [x] (5) pytest **964 passed**（+9: control 5 / http 2 / hermes_bridge 2、完全一致テスト2件追従）・ruff clean。`_patch_voice_pipeline` で `multiturn_enabled`→`is_enabled` 束縛（live 非依存化）
- [x] (6) dashboard.html（非git）: 「🧠応答モード」に「🔄連続会話」トグル追加（HTML/status追従/handler）。JS構文OK・ID整合3/3。status_api.py は変更不要（POST 汎用転送・status は既存 GET allowlist）
- [x] (7) 実機 E2E（ユーザー 2026-06-17・「挙動はいい感じです」）: gateway 再起動（PID 1581616）→ `/control/status` に `routing.multiturn:true` 反映確認 → dashboard トグル ON/OFF・再起動後維持・上限字幕・自然な会話すべて green
- [x] (8) E2E green 後: commit（feature/multiturn・`20517be`）✅ / learning-report `docs/multiturn-report.md`（Phase 1-3 まとめ）作成済 ✅ / worklog `docs/worklog/2026-06-17-multiturn-p3.md` 作成済。**マルチターン Phase 1-3 全クローズ。** 残=report のコミット（ユーザー提案待ち）/ develop マージ・push（指示時）

（Phase 4 firmware continuation は条件付き・MVP で問題が出た場合のみ — プラン参照）

---

### ★ 次フェーズ（計画確定・着手前）: 在室状態マシン + heartbeat 在室ゲート（Phase D 序盤）2026-06-16

**背景**: TMOS 在室ゲート「強い GO」（下記 TMOS 検証 Phase 3）を受け、自発提案の土台となる**在室状態マシン**を実装する。
ケンジさんの最終ビジョン（朝の挨拶/室温連動エアコン/在室・睡眠・不在のモード自動切替/生活支援AI）の**共通基盤**。
今回スコープ = **ステップ1（在室ゲートまで）**（ユーザー合意 2026-06-16）。SwitchBot 自動制御・個人識別(BLE)・室温連動は次ステップ以降。
自発開発は **承認制（human-in-the-loop）**：Hermes は「気づき・提案」まで→実装は CC が承認後に行う（ユーザー合意）。
ビジョン全体の分解は memory `project_life_support_vision` 参照。

**設計（過剰にしない）**: 状態 = [在室か](TMOS presence) × [活動/就寝時間帯](時刻)
- `ABSENT`（不在 = presence ロスが N デバウンス分継続）/ `ACTIVE`（在室×活動時間帯→heartbeat 許可）/ `QUIET`（在室×就寝時間帯→heartbeat 禁止=オフモード）
- 睡眠の厳密検知は TMOS 単体では不可（静止と就寝を区別不能）→ 時刻ベース。将来 ToF/活動量で精緻化。
- 既存 heartbeat `is_quiet()`（quiet hours）を就寝時間帯にそのまま流用。
- 閾値（就寝時間帯・不在デバウンス分）は **dashboard で実行時調整＋永続化**（set_neutral_pose の流儀。ここで値固定しない）。

**触るファイル**（investigator 調査済み 2026-06-16・在室土台は既存4ファイルに完備）:
- 🆕 `gateway/stackchan_mcp/presence.py`（状態マシン+10秒ポーリング+`~/.stackchan/presence_state.json` 永続化。control.py の mkstemp+os.replace 流用。dispatch 注入は sensors.py と同設計）
- 🆕 `gateway/tests/test_presence.py`（FakeGateway + fake dispatch、test_sensors の `make_dispatch` 流用）
- ✏️ `heartbeat.py` `_skip_reason()`（在室ゲート1条件：`is_occupied()` False でスキップ）
- ✏️ `gateway.py`（PresenceMonitor start/stop 紐付け）
- ✏️ `http_server.py`（`GET /control/presence` + `/control/status` に presence 同梱 + 閾値 `POST /control/presence/config`）
- ✏️ `~/razer-dashboard/dashboard.html` + `status_api.py`（在室状態表示+閾値スライダ。GET allowlist 追加を忘れない＝過去の教訓）

**着手時に確定する技術判断**: I2C mux 競合。presence ポーリングと dashboard センサータブが mux ch3 を同時アクセス→読み裂けリスク。ESP32 dispatch 経路の直列性を確認し、無ければ `sensors` 側に `asyncio.Lock` を1本足して I2C アクセスを直列化（poll は10秒間隔で軽い）。

- [x] (1) `presence.py`: `PresenceState`(4状態 Enum: UNKNOWN/ABSENT/ACTIVE/QUIET) + `PresenceMonitor`（10秒ポーリング/状態遷移/`presence_state.json` 永続化/`allows_heartbeat()`/デバウンス/連続エラー→UNKNOWN フォールバック）。**fail-open**設計（確実に ABSENT の時だけ抑制、不明時は許可＝センサー故障で黙りっぱなしを回避）
- [x] (2) `heartbeat.py` `_skip_reason` に在室ゲート追加（`presence.allows_heartbeat()` False → "room empty"。monitor 無し/None なら従来通り）
- [x] (3) `gateway.py` で PresenceMonitor 紐付け（`__init__`/start/stop、`from_env` opt-in）
- [x] (4) `http_server.py`: `GET /control/presence` + `/control/status` に presence 同梱 + 閾値 `POST /control/presence/config`
- [x] (5) I2C mux 競合の排他制御 → `sensors.py` に `_i2c_lock`(asyncio.Lock) 追加。read_tmos/read_gesture/init_tmos/init_gesture を atomic 化（read_all/init_all は内部経由で自動ロック・再入なし）。presence poll と dashboard センサータブが同ロック共有で読み裂け防止
- [x] (6) `tests/test_presence.py`（状態遷移/デバウンス/fail-open/エラー復帰/config/snapshot/ゲート、+25）+ `test_http_server.py`（presence エンドポイント +9）。既存回帰なし
- [x] (7) dashboard: **センサータブに「🏠在室判定」カード**追加（状態/自発提案可否/最終検知 + 不在猶予スライダ + 就寝時間帯入力、専用ノート `sc-pres-note` にエラー）。閾値は初回だけ反映（操作中の上書き防止）/ poll は既存センサーポーリングに相乗り（presence は gateway-local でデバイス非接触＝mux 競合と無関係）。`status_api.py` `do_GET` allowlist に `/control/presence` 追加（POST は汎用転送で不要）。**JS構文 OK・ID 整合 8/8**。実機反映は status_api 再起動が必要（dashboard.html は即時）
- [x] (8) 機械検証: **pytest 897 passed / ruff clean**（dashboard 未着手のため JS 構文は (7) で）
- [ ] (9) 実機 E2E（在室→自発提案発火 / 不在→沈黙 / 就寝時間帯→沈黙）← ケンジさん。**前提**: `STACKCHAN_PRESENCE_POLL_SEC` を env に設定（opt-in）+ サービス再起動
- [ ] (10) **learning-report（docs/）**（ユーザー合意・作る）+ worklog（docs/worklog/）+ memory 更新 ← worklog 済、report はフェーズ完了時

### ★ 次々フェーズ（設計確定・着手は在室ゲート実機検証後）: Hermes 自発判断層 2026-06-16

**ケンジさんの設計指摘（2026-06-16）**: heartbeat（自発の発話判断）は本来 Hermes agent（思考体）の責務。gateway の機械的タイマーが weather/memo を定型処理するのは歪み。→ **観測（gateway・高頻度・機械的）と判断（Hermes・文脈的）を分離する**。発話が部屋の状態に依存し、判断は思考体が担うのが理想。

**確定方針（ユーザー選択 2026-06-16）**:
- 観測は高頻度（`STACKCHAN_PRESENCE_POLL_SEC=5` 等）、Hermes 問い合わせは **状態遷移イベント駆動**（不在→在室=帰宅/起床 等の"意味ある変化"の時だけ。1分ポーリング問い合わせ=1日1440回=トークン/割り込み爆発を回避＝原則②）。
- heartbeat を分解: **idle gesture = ファーム/gateway 反射**（在室時・高頻度OK・Hermes不要・原則②）/ **自発発話 = Hermes 判断**（状態遷移トリガー・原則③）。
- 既存 gateway 内蔵通知（weather/memo）は **当面併存**（確実な定型通知）。将来 Hermes 統合を検討。
- 在室ゲート（ステップ1）は「観測層 + 機械的安全装置」として土台に残る（捨てない）。

**着手前の設計判断/調査**（investigator 委任予定）:
- どの状態遷移を「意味ある」とするか（不在→在室=最重要 / 活動↔就寝 / 長時間在室の継続は"遷移"でないので別途＝在室経過時間のイベント化が要るか検討）
- Hermes への問い合わせ経路（`hermes_bridge` 流用 vs 自発用 別経路）。**原則④: Hermes 本体改造はしない**
- 安全装置の流用（会話中スキップ/クールダウン/日次上限は既存 heartbeat speak の `_speak_skip_reason` を再利用）

**順序**: 在室ゲート（ステップ1）の実機 E2E 完了 → 本フェーズ着手（観測が信頼できてから判断層を載せる）。

**先行実装（2026-06-17・reactive 版／ケンジ案）**: `get_presence` MCP ツールを追加（`stdio_server` tool 定義 + `_dispatch_mcp_tool` + `http_server` BYPASS_TOOLS、pytest 899）。Hermes が Discord で「今どんな状態?」と聞かれたら在室状態（state/最終検知/閾値）を読んで答えられる。**reactive なので原則①④に沿う**（自発でなく聞かれたら答える・トークンは会話時のみ）。狙い=数日運用の精度検証（自然なドッグフーディング）+ 自発判断層への橋渡し（Hermes が状態を読める第一歩）。**有効化に gateway 再起動が必要**（新ツールの反映）。

**現在のステータス（2026-06-17）= 数日運用フェーズ**: env（`STACKCHAN_PRESENCE_POLL_SEC`）設定済（ケンジ）。観測層 + get_presence で数日過ごし、センサー精度を dashboard と Discord 会話の両面で確認 → 勘所を掴んでから自発判断層に着手。

### ★ 進行中: TMOS PIR (STHS34PF80) 活用検証（2026-06-16 着手）

**部品到着**: M5Stack Unit TMOS PIR (U185 / STHS34PF80) が届いた（memory `project_future_sensors` の到着待ち品）。
**狙い**: firmware 改造ゼロ（道A）で実機評価し、**heartbeat 在室ゲート**（会話に割り込まない発話タイミング）に足るか判定。
**ハード**: I2C 0x5A / FOV80° / >2m / 焦電PIRと違い**静止在室も検知**（ここが評価の本丸）。
**環境**: このマシン＝razer-server。gateway= systemd `stackchan-gateway.service`（WS:8765/capture:8766/MCP HTTP:8767）。
ダッシュボード proxy `:8080` が `POST /control/*` を gateway:8767 へトークン付き汎用転送 → 検証は `:8080/control/i2c` で `.env`・トークン不要。
**確認済み方針（2026-06-16）**: 配線=Port.A 直挿し（PaHUB2 なし）/ ドライブ= gateway に再利用可能な `POST /control/i2c` 追加 / learning-report 作成。
i2c ツール: `i2c_scan` / `i2c_read{addr,n_bytes}` / `i2c_write{addr,bytes}` / `i2c_write_read{addr,write_bytes,n_bytes}`（Port.A 専用）。
レジスタ: WHO_AM_I=0x0F→0xD3 / CTRL1=0x20(ODR) / FUNC_STATUS=0x25(presence/motionフラグ) / TPRESENCE=0x3A,3B / TMOTION=0x3C,3D / TOBJECT=0x26,27。

- **Phase 0 — 検証ツール準備（私／ハード不要）**
  - [x] gateway `http_server.py` に `POST /control/i2c` デバッグ経路追加（op で 4 ツール呼び分け・道A enabler・再利用可）
  - [x] pytest（mock gateway）でルート単体テスト追加（+24）→ **848 passed** / **ruff clean**（回帰なし）
  - [x] `scratch/tmos_probe.py` 作成（scan / whoami / poll サブコマンド、urllib のみ・依存なし）
  - [x] **ケンジさん**: `sudo systemctl restart stackchan-gateway` 実施済み（2026-06-16）→ `:8080/control/i2c` 稼働確認
- **Phase 1 — 配線＆疎通（ケンジさん配線／私検証）** ✅ 完了
  - [x] ケンジさん: TMOS を CoreS3 **Port.A** 直挿し・gateway 接続
  - [x] `scan` → **0x5A** 検出 ✓ / `whoami` → **WHO_AM_I=0xD3** ✓（疎通OK）
- **Phase 2 — 生反応観測** ✅ 完了（2026-06-16、実機実演）
  - [x] CTRL1=0x15（ODR=4Hz・BDU）で ODR 有効化 → ライブポーリング
  - [x] **重要修正**: presence/motion の L/H を別トランザクションで読むと ODR 更新を跨いで「読み裂け」→ ±256 偽スパイク。`read_s16` を**1トランザクション2バイト読み（auto-increment 実機確認済）**に修正
  - [x] **ハブ構成判明**: ケンジが PaHUB2 系 mux（PCA9548A **@0x70**, DIP）経由に変更。scan で 0x70 のみ→チャネル探索で **ch3=TMOS(0x5A) / ch2=ジェスチャー(PAJ7620U2 0x73, part_id 0x7620)**。プローブに `--mux/--ch`（PCA9548A チャネル選択 `1<<ch`、各周回で再選択）追加。ジェスチャー初回未検出はスリープでNACK（二度読みで検出）
  - [x] 実演（8分/961サンプル、ハブ ch3）で全区間取得。証拠: `scratch/tmos_session_2026-06-16.log`
- **Phase 3 — 実用評価** ✅ 完了 → **在室ゲート採用 = 強い GO**
  - [x] **静止在室の保持＝合格**: 在室中 **211.7秒連続で PRES フラグ 100% 点灯**（presence 平均774）。静止しても減衰せず保持。初回の「減衰」は通過物の過渡で steady-state ではなかった
  - [x] **分離**: 無人 presence 平均-1（±130ノイズ）vs 在室 平均767 → **50倍超のクリーン分離**。デフォルト閾値200が中間に最適
  - [x] **誤検出**: 無人約5分（473サンプル）で **PRES フラグ誤発火 0**。※motion フラグは無人でも時々発火（46/473）→ **在室判定は presence を使う・motion は使わない**
  - [x] **離脱レイテンシ**: フレームアウトで presence が即（~1サンプル/<1-2s）<200 へ復帰 → ゲート解除が速くリンガリング無し
  - [x] **推奨**: PRES フラグ（FUNC_STATUS bit2）or presence>200 を在室信号に。ポーリング 1-2Hz/数秒間隔で十分。堅牢化に「2サンプル連続」or 軽いヒステリシス。設置時に実環境でノイズ再確認（今回は発熱PC上で±130と広め）
  - [ ] 残（任意・非ブロッキング）: 距離 1m/2m・横ずれ FOV の定量化（今回 ~50cm で presence~770）。ジェスチャー(ch2/PAJ7620)9種の動作検証。PaHUB2 道B(firmware ドライバ)化の要否。`/control/i2c` のコミット要否
- **Phase 4 — まとめ**
  - [ ] learning-report（docs/）+ worklog（docs/worklog/2026-06-16-tmos-verify.md）+ memory 更新

### ★ 進行中: センサータブ追加（TMOS + ジェスチャー リアルタイム可視化）2026-06-16

ダッシュボードに「センサー」タブを新設し、TMOS PIR(ch3/0x5A) と ジェスチャー(ch2/0x73) の検知値を
リアルタイム表示する観察ツール。家中を動いてスクショ→挙動設計の土台にする。
計画: `~/.claude/plans/m5stack-port-ai2c-v2-1-dip-compressed-aurora.md`。**両方一気に実装＋learning-report 作成（ユーザー合意）**。

- [x] (1) gateway 新規 `sensors.py`: TMOS/PAJ7620 レジスタ定義・`_s16`・mux_select・read_tmos/read_gesture/init・read_all。PAJ7620 init array は RevEng_PAJ7620 から移植（55 ペア）
- [x] (2) `http_server.py`: `GET /control/sensors` + `POST /control/sensors/init` 追加、route 登録、import
- [x] (3) `~/razer-dashboard/status_api.py`: do_GET allowlist に `/control/sensors` 追加（**status-api 再起動で反映**）
- [x] (4) テスト: `test_sensors.py`（+13）+ `test_http_server.py` に `test_control_sensors_*`（+5）。**pytest 864 passed / ruff clean**
- [x] (5) `~/razer-dashboard/dashboard.html`: センサータブ UI（トグル+TMOSカード+ジェスチャーカード）+ 独立ポーリングタイマー（≈2.5Hz=400ms、トグルON∧タブ表示中∧画面表示中）。JS構文OK・全ID存在確認済
- [x] (6a) サービス再起動済 + バックエンド E2E ✅: `GET /control/sensors`=TMOS実値(presence/温度)正常、`POST /control/sensors/init`=TMOS who_am_i 0xD3 / ジェスチャー part_id 0x7620
- [x] (6b) **ジェスチャー検出 解決（物理＝レンズ光学経路）**: 設定は M5純正(`m5stack/M5Unit-GESTURE`)と完全一致＝ソフトは正しかった。原因は物理（レンズの向き/遮蔽/フィルム）。Kenji が物理調整後、up/down/left/right を検出。`scratch/gesture_probe.py poll` で確認。**本番 `/control/sensors`（mux切替経路）でも ~9.7Hz で検出OK・TMOS 193/193**。教訓: gesture が出ない時は init より先にレンズ物理（極接近で物体信号≒0 は遮蔽/向きの典型）
- [x] (6c) ダッシュボード センサータブ 実機 E2E ✅（ユーザー「いい感じ」2026-06-16）。TMOS/ジェスチャー表示・トグル動作確認
- [ ] (7) 実地観察（Kenji・主目的）: 家中ウォークスルー→スクショ→在室ゲート閾値/ジェスチャー反射の挙動設計（継続）
- [ ] (8) learning-report（観察結果込み）+ memory 更新。IR LEDカメラ確認/物理で何が効いたかの追記
- [x] worklog: `docs/worklog/2026-06-16-sensor-tab.md` 作成済

### ★ 進行中: ダッシュボード機能拡張プロジェクト（全5フェーズ・/clear 境界で分割）

前回コンテキスト逼迫の反省から、機能追加を5フェーズに分割し各完了で `/clear` して進める。
計画全文: `~/.claude/plans/clear-100-200-floofy-shell.md`

- [x] **フェーズ1: dashboard セクション骨組み整備＋デザインの型確立** — 7カテゴリカードに再編し、コンパクト＆洗練＋スタックちゃんぽさへ刷新（音量＋ミュート1行統合・大きい現在値・顔アバターで接続表示）。型 `.row`/`.row-val`/`.slider`/`.icon-btn`/`.sc-ava` を後続フェーズも踏襲。**ユーザー承認済（2026-06-14）**。worklog: `docs/worklog/2026-06-14-phase1-dashboard-sections.md`。
- [~] **フェーズ2: 画面明るさ + LED 制御UI（着手中 2026-06-14）** — gateway(`control.py`/`http_server.py`)に HTTP制御追加 + dashboard ⑤デバイス調整カードに UI。firmware は既存MCPツール流用で **flash 不要**。LED UI は「カラーピッカー+オン/オフ」で確定（ユーザー選択）。
  - 調査確定: 明るさ=firmware `self.screen.set_brightness`(0-100, **NVS自動永続**・既定75) / LED=`self.led.set_all`(全12同色 r/g/b 0-255)・`self.led.clear`(消灯)。voice turn 中は `set_indicator`(青)優先→終了 finally で idle 色へ復元する方針。
  - [x] (a) `control.py`: `set_brightness`/`apply_persisted_brightness` + `set_led`/`apply_persisted_led`/`restore_idle_led`。`control_state.json` に `brightness`・`led` 追加（音量と同パターン）。
  - [x] (b) `http_server.py`: `POST /control/brightness`・`POST /control/led` 追加 + `/control/status` に `brightness`(未接続時None)・`led` 同梱（status_api は汎用プロキシなので無改修）。
  - [x] (c) `gateway.py` `_on_device_ready`: 接続時に明るさ・LED を復元（`apply_persisted_brightness`/`apply_persisted_led` 追加）。
  - [x] (d) `hermes_bridge.py:277`: voice turn finally の強制消灯 → `restore_idle_led`（ユーザー設定色へ復元、off なら従来通り消灯）。
  - [x] (e) `dashboard.html` ⑤カード: 明るさスライダー（既存型）+ LED カラーピッカー+オン/オフトグル + JS（`.color-swatch` 追加）。
  - [x] (f) テスト: `test_control.py`(+14)・`test_http_server.py`(+8) に brightness/LED ケース追加。既存 load_state 完全一致テスト2件も新フィールド追従。
  - [x] (g機械検証) `pytest` / `ruff clean` / dashboard JS構文・ID・タグ OK。worklog 作成済。
  - [x] (実機E2E①初版) 明るさ/LED(単色) 実機目視 OK（ユーザー確認済 2026-06-14）。
  - [x] (h) **LED を3状態に拡張**（ユーザー提案）: idle(通常・オン/オフ+色) / listening(聞き取り・準備中) / hermes(Hermes動作中) を各色設定可 + 「試」点灯ボタン。voice turn で `apply_led_state(slot)` でフェーズ点灯。ハードコード青を撤去し全色 `set_all` 化 → **60秒 idle-settle 問題が解消する見込み**。`control_state.json led` をネスト化(+旧形式マイグレーション)。`pytest 792 passed`/`ruff clean`/dashboard機械検証OK。
  - [~] (h実機検証) LED 3スロット色・試ボタン・明るさ・横並びは**フェーズ3 E2E で実機確認済**（ユーザー「いい感じ」）。残るは**会話1往復での LED フェーズ遷移（listening→hermes→idle）/60秒 idle-settle 解消の明示確認のみ** → **フェーズ5の残ユーザー実機1往復チェックと同時にクローズ**（同チェックでヘッダー状態ピルの「🎙録音中」遷移も併せて目視）。
- [x] **フェーズ3完了: 近接listen mode + トグル + LED明るさ/横並び** — flash 1回・コミット+push `bf161b3`。計画: `~/.claude/plans/drifting-finding-wind.md`、worklog: `docs/worklog/2026-06-14-phase3-proximity-led.md`
  - **音量200は除外（ユーザー確定）**: ソース確定で `vol>=100` は 0dB クリップ＝100が物理最大、200は無意味（`esp_codec_dev.c` デフォルトカーブ `_get_vol_db` L99-101）。スライダー上限100据置。PA アナログゲインは歪みリスクで見送り。
  - 近接反応を mode 3択化（reflex/listen/**off**）、デフォルト listen、`enabled` 廃止し mode 一本化、NVS永続+旧enabled migration。
  - **追加要望（2026-06-14、セッション中にユーザーから）**:
    - 要望1: 近接 listen を**トグル化**（かざす→開始 / もう一度→送信）。listening中はcooldownバイパス。**flash要**（firmware）。
    - 要望2: **LED 全体の明るさ**スライダー（gateway で r/g/b スケール、flash不要）。
    - 要望3: dashboard の **LED カラーUIを3列横並び**（flash不要）。
  - [x] (A) firmware stackchan.cc: ProxMode enum/ヘルパー・メンバ・発火ガード・HandleProximity（**トグル**）・cooldownバイパス・NVS migration・get_touch_state・set_proximity_config
  - [x] (B) gateway: stdio_server / http_server(control_proximity/_proximity_status) + **LED明るさ**（control.py `_scale_rgb`/`set_led_brightness`/2送信経路スケール、http `/control/led_brightness`）
  - [x] (C) gateway テスト: 近接mode + LED明るさ（計 798 passed）
  - [x] gateway pytest **798 passed** + ruff clean
  - [x] firmware Docker build（warning0・v2.2.6_stackchan.zip）
  - [x] (D) dashboard: 近接 select 3択 + **LED 3列横並び**（led-cols）+ **LED明るさスライダー**（sc-led-bright）
  - [x] 実機 flash + E2E（migration `mode=listen`/threshold824保持・トグル・reflex・off・LED明るさ・横並び・回帰）ユーザー「いい感じ」✅
  - [x] worklog + commit/push（bf161b3）
- [x] **フェーズ4: 両方スキップで決着（2026-06-15・実装なし）** — Codex 利用率=OpenAI に枠%を返す公式 API が無く `~/.codex/` にもキャッシュ無し→取得不可でスキップ。Gemini 利用額=自動取得は Cloud Billing→BigQuery Export が事実上唯一の経路（GCP 未設定・現状 Gemini 未使用）で設定重く見送り（将来 Gemini 使用開始なら BigQuery Export 経由で再検討）。dashboard 受け皿（`setUsage`/`remain` 再利用）は無傷で将来流用可。調査記録: `docs/worklog/2026-06-15-phase4-decision.md`、決着プラン: `~/.claude/plans/eager-enchanting-truffle.md`
- [x] **フェーズ5完了: dashboard 人間工学的仕上げ（フル案・HTML-only）** — `~/razer-dashboard/dashboard.html` のみ（git管理外・flash/コミット不要）。スコープ=**フル案**（ユーザー選択）。実装 A〜H: (A)状態ピル昇格＝録音状態を接続バッジ `sc-conn` に常時反映(🎙録音中…+パルス) (B)カードを使用頻度で3グループ再編(よく使う/調整/設定・詳細)+`.group-label` (C)LED色設定(`.led-cols`)を既存 `.ctl-toggle`/`.pad-body` で初期折りたたみ (D)首デフォルト保存に `confirm()`+警告色 `.btn-warn` (E)タッチ域44px(`.icon-btn`/`.color-swatch`/`.btn-act`/`.field`) (F)レスポンシブ(max-width 480→min(100%,580px)・狭画面でLED2列) (G)サーバータブ CC利用率を5h主/7d従に階層化 (H)孤児 `.led-slot` CSS削除。**思考中表示は gateway 未公開のため HTML-only 境界を守り対象外**（後日）。各 `.ctl` の中身・ID・イベントは不変・再利用クラスは崩さず。検証: Playwright(同梱chromium)で**コンソール/ページエラー0**・カード順序・折りたたみ初期状態・状態ピル・44px・390/768px・利用率階層を確認、`node --check`OK。worklog: `docs/worklog/2026-06-15-phase5-dashboard-ergonomics.md`、計画: `~/.claude/plans/5-shimmying-cherny.md`。**残: ①ユーザー実機1往復チェック（下記フェーズ2(h)と同時クローズ）②learning-report（全5フェーズ）**

確定方針: ウェイクワード「ハイ スタックちゃん」は今回スコープ外（microWakeWord は後日）。音量はデジタル増幅しない（安全側）。近接listen は手かざし(~10-15cm)=明示トリガー扱い。

### ★ 進行中: ダッシュボード追加要望（モード機能 + 初期タブ）2026-06-15

ダッシュボード機能拡張プロジェクト完了後のユーザー追加要望。計画: `~/.claude/plans/5-shimmying-cherny.md`。worklog: `docs/worklog/2026-06-15-mode-presets.md`。

- [x] **要望2: 初期表示をサーバータブに** — dashboard.html `activeTab='server'`+`showTab('server')`、冗長 `load()` 除去。Playwright 確認済み。
- [x] **モード機能 M1: gateway バックエンド（コミット `2d13ac7`）** — 現在の全設定（音量/ミュート/マイク感度/明るさ/LED全色・明るさ/近接/heartbeat、**首の向き neutral_pose は置き場所依存で除外**）を名前付きプリセットとして `~/.stackchan/presets` に保存・一括適用。control.py に save/list/load/delete/apply_preset + `normalize_preset_name`(パストラバーサル防止) + `_preset_lock`(voice_turn ガード)、http_server.py に `/control/presets/{list,save,apply,delete}` + `_build_preset_snapshot`。既存セッター/`load_state`/`set_neutral_pose` は不変（純追加）。`pytest 816 passed`(+18)/`ruff clean`。
- [x] **モード機能 M2: ダッシュボード UI（HTML-only・即反映）** — 「よく使う」先頭にモードカード（select+適用/🗑削除、名前入力+保存、既存の型流用・独自実装ゼロ）。`loadPresets`/`scApplyPreset`/`scSavePreset`/`scDeletePreset`。`node --check` OK + Playwright で描画・44px・**pageerror 0**・初期 disabled 確認。
- [x] **モード機能 ユーザー実機 E2E 成功（「いい感じ」2026-06-15）** — 両サービス再起動後、保存→適用の一括復元が実機で動作確認。投入直後の2バグ（①status_api `do_GET` の allowlist に `/control/presets/list` 欠落＝GET未プロキシ ②保存失敗が遠い sc-err にしか出ず無反応に見えた）も修正済み（status_api.py・dashboard.html、いずれも git管理外＝即反映、サービス再起動で反映済み）。
- 記録のみ（learning-report は不要・ユーザー確認済、worklog のみ）。**ダッシュボード追加要望2件クローズ**。

### 0. review-cleanup の実機 flash + USB-reset ブロック調査（※ CLAUDE.md 最終更新では flash+E2E 完了済み・要整合確認）

2026-06-14 全体レビューで修正した heartbeat 会話割り込みバグ等（ブランチ `feature/review-cleanup`、3コミット済み・gateway pytest 756 passed・firmware ビルド成功）を **まだ実機に焼けていない**。

- **障害**: 現在動いている develop 版ファームが esptool の自動リセット(RTS/DTR・usb-reset)をブロック → `OSError:[Errno 71] Protocol error`（pyserial `_update_rts_state` の TIOCMBIC ioctl が EPROTO、Docker・ホスト venv 両方で再現）。CoreS3 のダウンロードモード操作も USB 切断/電源オフで `/dev/ttyACM0` が頻繁消失し不安定。
- **重要手掛かり**: 前回 develop 版は Claude Code 単独（自動リセット）で焼けていた → **develop 版で USB-CDC/console 設定が変わり USB-Serial-JTAG reset を妨げる疑い**。
- **次回方針**: `firmware/sdkconfig.defaults*` / `config.json` の `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG` 等を「前回焼けた版」と diff → 恒久対策（console を UART へ等）後に焼く。詳細経緯は `docs/worklog/2026-06-14-review-cleanup.md`。
- **焼けたら E2E**: ①顔が出てタップで首が動く ②会話中(STT→Hermes 待ち)に首が勝手に動かない（設計原則①、今回の本丸）。
- flash 後 `sudo systemctl start ModemManager` を確認（今回切り分けで一時停止 → 戻し済み。ただし EPROTO の原因ではなかった）。

### 1. 部屋スケール（1〜2m）の視線追従 — ToF Unit (VL53L0X) 購入待ち

C1 近接視線追従は「手かざしリフレックス（〜10-15cm）」までは LTR-553 で実機稼働済み（archive: Phase C 本体 / 2026-06-13 Phase E仕上げ + LTR-553 を参照）。本来の目標「近づくと向く」(1〜2m) には別ハードが必要で、購入待ちで継続。

- 仮にシェルを開口しても有効距離 ~10cm（手かざし専用）。本来の目標「近づくと向く」(1〜2m) には **M5Stack ToF Unit (VL53L0X, Grove Port A, ~¥1,000)** が必要 → 購入はユーザー判断待ち（外出中）
- 結論: **手かざし（〜10-15cm）は十分実用**。前回（6/11）の「前面シェルが光路を完全閉塞」は誤りだった（理由不明。前回はカメラ付近に手をかざしたがセンサー窓の実位置が違った可能性）。部屋スケール（1〜2m）は引き続き ToF Unit 待ち

### 2. 遠い将来の TODO（Phase D 由来）

- 外部クライアント(Claude Code 等)から `/v1/chat/completions` で `terminal` が必要になったら、案 C(`HERMES_HOME` プロファイル分離)に切替。詳細は `docs/phase-d-report.md` §4.2

### 3. ウェイクワード（②）の将来候補 — 別フェーズ

② 「スタックちゃん」は MultiNet 中国語ピンインで日本語語を認識する方式の限界が確定し、タップ/背面なで運用でクローズ済み（設計原則①と整合）。将来やるなら別アプローチ。

- 将来は microWakeWord（TFLite・日本語学習可）が候補（別フェーズ）

### 4. 将来検討（ロードマップ上の未着手項目）

CLAUDE.md のロードマップより、まだ着手していない将来項目:

- **Phase D（自律性・任意/将来）**: heartbeat の発話あり第2段階（`STACKCHAN_HEARTBEAT_SPEAK=1` で Hermes 文脈 → 一言生成、クワイエットアワー必須のまま。archive: Phase D「将来（第2段階、今回はやらない）」参照）/ LFM2.5 ローカル LLM 統合の本格検討（VRAM 余裕次第）
- センサー拡張（memory `project_future_sensors.md` 参照）: TMOS PIR + PaHUB2 + ジェスチャー → heartbeat 在室ゲート（部品到着待ち）

---

## 直近の作業文脈（2026-06-14）

Phase F フォローを完了（詳細な完了記録は archive の 2026-06-14 セッション群 + `docs/phase-f-report.md` + `docs/worklog/2026-06-14-*.md`）:

- ② ウェイクワード: Feed の RMS を直接測定して根因を確定（音声経路は健全、MultiNet ピンインの認識限界）→ タップ/背面なで運用でクローズ。副産物で mic_gain 12→30dB（STT にも有効）。
- ① 顔ステータス遅延: gateway 根因 → `on_listen_started` で録音開始時に即送出（即時化）。
- ③④ 首中立姿勢: ダッシュボードのジョイスティックで実行時調整・NVS 保存できる機能を新規実装（firmware `self.robot.set_neutral_pose` / NVS `stackchan_pose` / gateway `/control/head`・`/control/neutral_pose`）。ユーザー実機検証「全ていい感じ」✅。
- CC 発話通知を gateway 経由で復活 + ダッシュボードに「🔔 CC発話通知」トグル新設。ユーザー E2E 確認済み。

### 既知の軽微点・積み残し（生きている注記）

- ジョイスティック初期ドット位置が pitch45（firmware 既定 38）。`/control/status` に neutral 未露出のため。動作には無影響（保存は正しい）。気になれば後で status に neutral 追加。
- dashboard は `~/razer-dashboard/`（git 管理外＝ファイル編集が即デプロイ・コミット不要）。
