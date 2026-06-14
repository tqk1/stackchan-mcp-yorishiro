# tasks/todo.md — 現役タスク

> 完了済みの Phase 0〜F 作業記録は `tasks/todo-archive-2026Q2.md` に移動した（原文のまま）。
> 各 Phase の詳細な振り返りは `docs/phase-a〜f-report.md` / `docs/worklog/` を参照。
> このファイルには **まだ生きている未完了項目** と **直近の作業文脈** だけを残す。

最終整理: 2026-06-14（feature/review-cleanup）。直前ステータス: **Phase F フォロー完了 + ② ウェイクワードはクローズ（タップ/背面なで運用）**。Phase A〜E + C1 クローズ済み。詳細は `docs/phase-f-report.md` および archive の 2026-06-14 セッション群を参照。

---

## 現役タスク（まだやるべき生きた未完了項目）

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
  - [ ] (h実機検証) **gateway 再起動 → 3スロット色・試ボタン・会話時のフェーズ遷移・60秒問題解消 を実機確認**（ユーザー）。
- [~] **フェーズ3（改訂）: 近接listen mode 切替** — firmware変更・**flash 1回**。計画: `~/.claude/plans/drifting-finding-wind.md`
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
  - [ ] firmware Docker build（再ビルド中＝トグル反映）
  - [x] (D) dashboard: 近接 select 3択 + **LED 3列横並び**（led-cols）+ **LED明るさスライダー**（sc-led-bright）
  - [ ] 実機 flash + E2E（migration/reflex/listen**トグル**/off/NVS保持/cooldown/LED明るさ/LED横並び/回帰）
  - [ ] worklog + commit/push
- [ ] **フェーズ4: サーバタブに Codex利用率 + Gemini API利用額** — dashboard。まず取得手段を調査（無ければ相談）。
- [ ] **フェーズ5: dashboard 人間工学的仕上げ** — 全項目出揃い後に整理。完了後 **learning-report 1本**作成。

確定方針: ウェイクワード「ハイ スタックちゃん」は今回スコープ外（microWakeWord は後日）。音量はデジタル増幅しない（安全側）。近接listen は手かざし(~10-15cm)=明示トリガー扱い。

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
