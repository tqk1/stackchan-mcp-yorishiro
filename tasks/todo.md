# tasks/todo.md — 現役タスク

> 完了済みの Phase 0〜F 作業記録は `tasks/todo-archive-2026Q2.md` に移動した（原文のまま）。
> 各 Phase の詳細な振り返りは `docs/phase-a〜f-report.md` / `docs/worklog/` を参照。
> このファイルには **まだ生きている未完了項目** と **直近の作業文脈** だけを残す。

最終整理: 2026-06-15。直前ステータス: **ダッシュボード機能拡張プロジェクト 全5フェーズ実装完了**（フェーズ1〜3 `bf161b3`、フェーズ4 両方スキップ決着、フェーズ5 人間工学的仕上げ＝フル案・HTML-only を実装＋Playwright自動検証パス 2026-06-15）。残: ①ユーザー実機1往復チェック（=フェーズ2(h)積み残し同時クローズ）②learning-report（全5フェーズ）。それ以前: Phase F フォロー完了 + ② ウェイクワードはクローズ（タップ/背面なで運用）、Phase A〜E + C1 クローズ済み。詳細は `docs/phase-f-report.md` および archive の 2026-06-14 セッション群を参照。

---

## 現役タスク（まだやるべき生きた未完了項目）

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
