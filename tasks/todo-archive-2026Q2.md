# tasks/todo.md アーカイブ（2026 Q2）

これは tasks/todo.md から移動した完了済み Phase 記録のアーカイブ。各 Phase の詳細な振り返りは docs/phase-*-report.md（Phase A〜F 全て存在）/ docs/worklog/ を参照。現役の未完了タスクは tasks/todo.md 側に分離した。以下の見出しブロックは原文を一切改変せず、移動時点（newest-first）の並びのまま丸ごと移している。

---

# 2026-06-14 セッション3 — CC発話通知を gateway 経由で復活 + ダッシュボードにトグル（方針承認済み）

ユーザー報告: ダッシュボードに「Claude Code の実行状況を話す機能」の ON/OFF トグルが無い。
調査で判明=この通知連携は三重に停止中: ①settings.json が参照する `stackchan_cc_notify.sh` が不在(hook空振り) ②フラグ `~/.claude/hooks/stackchan_notify.off` が ON(6/13に OFF 化) ③発話先 yuno-chan-api :5050 がダウン。
ユーザー方針(AskUserQuestion): **gateway 経由で復活**。経路= hook → status_api(:8080)/control/say → gateway(:8767) → VOICEVOX → StackChan（hook はトークン非保持、status_api が付与）。

## 確定事実
- gateway /control/say: POST `{"text":...}`、`synthesize_and_send`（http_server.py:394-456）。token_protected。
- status_api(:8080): `/control/*` 汎用プロキシ（Bearer 付与）。:8080・gateway・device 接続すべて稼働中・esp32_connected:true 確認済み。
- ON/OFF フラグ: `~/.claude/hooks/stackchan_notify.off` 有=OFF。stackchan_toggle.sh と共通。
- dashboard 雛形: heartbeat トグル（HTML 236-242 / loadStackchan 468 / JS 635-640）。scPost(path,payload) は汎用 fetch で /cc_notify にも使える。

## チェックリスト
- [x] 1. hook 新規作成 `~/.claude/hooks/stackchan_cc_notify.sh`（gateway経由・2台分岐 Linux=localhost:8080/その他=192.168.0.19:8080・トークンレス・フラグ尊重・$1=attention|done）+ chmod +x。settings.json は無改変
- [x] 2. hook 単体 E2E → StackChan が「リナックスのクロードコード、お仕事終わったよ！」発話、ユーザー確認 ✅
- [x] 3. status_api.py に独立エンドポイント `/cc_notify`(GET/POST) 追加。CC_NOTIFY_FLAG 定数。汎用プロキシは無改変。バックアップ *.bak-20260614cc
- [x] 4. dashboard.html に「🔔 CC発話通知」トグル追加（HTML=heartbeat の隣 / loadStackchan に /cc_notify GET 反映 / change で scPost('/cc_notify',{enabled})）。バックアップ
- [x] 5. status_api 再起動（ユーザーが sudo 実施）→ /cc_notify 有効化確認 ✅
- [x] 6. E2E: ON→発話 ✅ / OFF→沈黙 ✅ / GET・POST 200 ✅ / dashboard トグル目視・操作 ✅（ユーザー確認）
- [x] 7. 記録: worklog 新規（2026-06-14-cc-notify-dashboard-toggle.md）/ memory（reference_cc_notify + project 更新）✅。残=コミット提案

---

# 2026-06-14 セッション2 — ②ウェイクワード不発の決定的診断（計画承認済み）

計画: `~/.claude/plans/shimmying-yawning-ripple.md`。ゴール=②の根本原因を**確定**する。

## 今セッションのコード追跡結論（idle で WW を殺す経路は無い）
- `EnableWakeWordDetection` 呼出元3つ（状態機械 / power_save_timer / sleep_timer）。stackchan は power_save_timer のみ使用、`PowerSaveTimer(-1,60,-1)`（cpu_max_freq=-1）で減光時 WW 無効化ブロック（power_save_timer.cc:78-98）を丸ごとスキップ。OnEnterSleepMode は画面減光のみ。→ **WW に無実**
- 状態機械は idle で `EnableWakeWordDetection(true)`（application.cc:952）。`AudioInputTask` が `ReadAudioData`→`Feed` 連続呼出、input 無効なら自動再有効化（audio_service.cc:185-189）。15s 電源タイムアウトは last_input_time_ 毎更新で WW 稼働中は発火しない。→ **構造上 idle で WW 経路は生きている**
- 残る唯一の未測定点＝「Feed に渡る音が実信号かゼロか」。既存 [DIAG] prob は TIMEOUT 時 get_results 空で原理的に不能。

## チェックリスト
- [x] S2-1: `custom_wake_word.cc` Feed に RMS 計装追加（detect 直前 chunk の peakRMS + input_enabled + running + detecting/timeout）。純追加ログ
- [x] S2-2: 診断ビルド成功（exit0、xiaozhi.bin 3,599,728B、generated_assets.bin 3,983,046B=不変）
- [x] S2-3: app-only flash（0x20000、Hash verified）+ シリアルキャプチャ（scratch/ww_serial_capture.py → ww_serial.log）
- [x] **S2-4: 原因確定 = (A) MultiNet ピンイン認識限界**。直接測定結果:
  - 静音 peakRMS ~12 / **input_enabled=1・detecting=31chunks/秒**（音声経路は完全健全、取りこぼし0）→ (C)入力ゼロは**否定**
  - gain12dB 時の発話 peakRMS ~110（-49dBFS・弱すぎ）→ **mic_gain=12 は下げすぎ**と判明（STT精度にも悪影響のはず）
  - `set_mic_gain 30` に実行時変更（再ビルド不要）→ 発話 peakRMS **608〜1164**（-29dBFS・健全）
  - **健全レベルでも 12候補・閾値0.1 で検知ゼロ・prob全て0.000** → 「スタックちゃん」を中国語ピンインで MultiNet に載せる方式そのものの限界が確定
- [x] **S2-5: 方針決定（ユーザー 2026-06-14）= ②はタップ/背面なで運用でクローズ**（両方正常動作・設計原則①と整合）。診断ログは custom_wake_word.cc を HEAD に revert して除去済み。将来は microWakeWord（TFLite・日本語学習可）が候補（別フェーズ）
- [x] S2-6a: ①③④ ユーザー1次検証 — ①まだワンテンポ遅い / ③改善も中立が上向きすぎ→角度下げ要望 / ④未検証
- 付記: mic_gain は現在 NVS=30（STT にも有効）。ダッシュボードのマイク感度スライダーで実行時調整可

## S3: ①③④ フォロー + 首角度ダッシュボード調整機能（ユーザー提案、2026-06-14）
ユーザー提案: 首の中立角度をダッシュボードのジョイスティックで実行時調整・保存（再ビルド不要に）。set_proximity_config と同じ NVS 流儀。UI=ジョイスティックパッド採用。
- [x] S3-1: firmware（agent a05059）— 中立姿勢を NVS 永続化（namespace `stackchan_pose`、既定 yaw0/pitch38）。新ツール `self.robot.set_neutral_pose{yaw,pitch}`（clamp+NVS+即move）。boot/③TouchRevert/④idle settle の3経路を neutral_yaw_/pitch_ に差し替え（seed==target 不変条件維持）。get_head_angles に neutral 追加。**diff 精査OK**（ScheduleIdleSettle/Settings API/WriteHeadAngles 2引数 すべて存在確認）
- [x] S3-2: gateway（agent a5b34d）— `POST /control/head`（ライブ=set_head_angles）+ `POST /control/neutral_pose`（保存=set_neutral_pose）+ stdio tool_map/宣言。**① 即時化**: esp32_client に on_listen_started フック→録音開始(state==start)の最速点で「きいてるよ」送出（hermes_bridge:344 の遅延送出が根因、既存は冪等で残置）。**749 passed(+18)・ruff OK**
- [x] S3-3: dashboard（agent a6b314）— `~/razer-dashboard/dashboard.html`（git管理外・別repo）に「🕹 首の角度調整」ジョイスティックパッド。x→yaw/y→pitch、120ms throttle で /control/head ライブ、「デフォルトに保存」→/control/neutral_pose。status_api は汎用転送で改変不要。node/py チェックOK。bak-20260614
- [x] S3-4: 最終 firmware ビルド成功（exit0、xiaozhi.bin 3,601,616B、assets 不変）。③38化+中立NVS化+診断ログ除去込み
- [x] S3-5: カットオーバー: app-only flash（Hash verified）→ 再接続・mic_gain30 再適用確認 → ユーザーが gateway 再起動・ダッシュボード再読込
- [x] S3-6: ユーザー検証「全ていい感じ」— ①即時化 / ジョイスティックでライブ+保存+③④が保存値に復帰 / ④60秒アイドル すべてOK
- [~] S3-7: コミット（firmware+gateway = 本repo。dashboard = ~/razer-dashboard は**非git**＝ファイル編集が即デプロイ・コミット不要）+ worklog/CLAUDE.md/memory 確定（実施中）
- 既知の軽微点: ジョイスティック初期ドット位置が pitch45（firmware既定38）。/control/status に neutral 未露出のため。動作には無影響（保存は正しい）。気になれば後で status に neutral 追加
- 積み残し: **docs/phase-f-report.md（Phase F 全体の learning-report）未作成**

---

# ★次セッション最優先（2026-06-14 context clear 前メモ）

## いまの状態（clear 後はここから）
- **ブランチ feature/phase-f-dashboard。firmware 変更は未コミット（working tree on disk・git status で見える）**: `stackchan.cc`(①③④修正) と `custom_wake_word.cc`([DIAG]ログ)。意図的に未コミット（②調査中＋①③④ユーザー検証前）。
- **flash 済み firmware = この未コミット版**（app-only 0x20000、3,599,312B、Hash verified）。assets 不変。gateway/status-api は最新コミット反映済みで再起動済み。device WS接続・36ツール。
- 退避バイナリ: /tmp/xiaozhi.bin.flashed-0720（旧）。診断ログ: /tmp/boot_serial2.log（コマンド登録確認）、/tmp/greet_serial2.log。

## やること（優先順）
1. **① ③ ④ のユーザー実機検証**（flash 済み・未確認）:
   - ① タップ→その場で「きいてるよ」/「考え中」が即出るか（1ターン遅延解消。`lv_refr_now` 修正）
   - ③ 近接で上向き→**約3秒で正面復帰**するか（TouchRevertCb に WriteHeadAngles(0,45) 追加）
   - ④ 60秒放置→首正面・表情idle・LED消灯に自動復帰するか（idle_settle_timer_ 新設）
2. **② ウェイクワード不発の最終確定**（最有力＝アイドル時にマイク音声が WW 検出器に届いていない／ユーザー仮説）:
   - 到達点: インフラ正常確定（mn7_cn・12ピンイン登録・閾値0.10、OOVシロ）。実発話でも検知ゼロ・[DIAG] prob=0.000。ただし prob=0.000 は「TIMEOUT時 get_results が空」=入力ゼロとは限らない（診断の限界）。
   - 候補機構（investigator a95b07ed、未確定）: コーデック入力電源管理 `audio_service.cc:686`(AUDIO_POWER_TIMEOUT_MS=15000) が input 閉じる→`cores3_audio_codec.cc:263-267` `Read` は input_enabled_=false で dest 未書込=ゼロ。**論点**: アイドルでWW稼働中は ReadAudioData 連続呼び出しのはずで15sタイムアウトは発火しないはず→機構の発火条件が未確定。
   - **次の一手（最優先・確定用）**: 次ビルドで「`wake_word_->Feed` に渡る data の RMS/エネルギー」+「codec `input_enabled_` 状態」を ESP_LOGI → アイドル時に MultiNet が実音声を受けているか/ゼロかを**直接**確認。
   - 修正方向候補: `audio_service.cc:686` の無効化条件に `&& !IsWakeWordRunning()`(:119既存) を追加し、WW稼働中はマイク常時有効。warm-upフレーム破棄も検討。**確定してから直す**（場当たり禁止）。
   - 方針判断（ユーザー未決）: 直すか / タップ・背面なで運用で②を区切るか。タップ/なでは正常動作。
3. ①③④ をユーザー検証 OK なら**コミット**（[DIAG]ログは②継続のため残すか判断）→ worklog/phase-f-report.md（未作成）/CLAUDE.md/memory 確定。
4. 記録の積み残し: **docs/phase-f-report.md は未作成**（bb31fa8 のメッセージは「新規」と書くが実体なし）。F/F-2/F-3 全体の learning-report。

---

# 2026-06-14 セッション再開 — Phase F 実機 flash + E2E + 記録確定

前セッションが中断。状態を突き合わせて整理した結果（working tree クリーン・全コードコミット済み `bb31fa8`）:

## 実態の補正（todo マーカーが実態より古かった）
- **F / F-2 / F-3 の firmware・gateway コードは全てコミット済み**（`bb31fa8` がコード+worklog をまとめてコミット。コミットメッセージは "docs..." だが実体はコード多数）
  - 下記 `[ ]`/`[~]` のうち **FB3（gateway 字幕/route_badge/led_indicator/audio_level）= コミット済み**、**F3-3（firmware set_mic_gain / SetInputGain）= コミット済み**。マーカーは「コード完了・実機反映未了」が正
- **device は F4b（Phase F バグ修正版）までしか焼かれていない** → F-2/F-3 の firmware 追加（subtitle/route_badge/led_indicator/set_mic_gain）は**未 flash**。実機を最新コミットに追いつかせる flash が必須
- **`docs/phase-f-report.md` は存在しない**（`bb31fa8` メッセージは「新規」と書くが実ファイル無し＝中断の痕跡）。learning-report は未作成

## 今セッションのチェックリスト
- [x] R1: firmware clean rebuild ✅（`release.py stackchan`、exit0。xiaozhi.bin 3,598,336B / 13% free、generated_assets.bin 3,983,046B でF-2と完全同一サイズ＝assets不変確定。WW設定 USE_CUSTOM=y/"su ta ke qiang"/閾値20/MN7 反映）
- [x] R2: flash ✅（app-only 0x20000、3,598,336B、Hash verified、ハードリセット。assets/NVS 保持）
- [x] R3: sudo restart ✅（07:23:45。gateway クリーン起動・ツール36個=新firmware・volume100/mic_gain18 再適用・gestures=off。status-api も再起動）。プロキシ /control/status → ok:true 確認
- [ ] R4: 実機 E2E（下の集約チェックリスト R4-list）
- [ ] R5: ウェイクワード/マイクゲイン チューニング（検知率・誤検知。不足ならピンイン qiang→chang / 閾値 0.2→0.12 再ビルド）
- [ ] R6: 記録 — phase-f-report.md 新規作成（実機検証章は E2E 後）/ worklog 追記 / CLAUDE.md ステータス / memory

## 実機FB（2026-06-14 E2E で4件、③以外は good）— firmware 1回の再ビルドに集約
根本原因は調査3本で裏取り済み（debugger + investigator×2）。
- [ ] FBa ①顔ステータス1ターン遅延: **firmware レンダリング起動漏れが主因**（gateway無罪・テスト済）。`SetStatusText`/`SetSubtitleText`/`SetRouteBadge`（stackchan.cc 4681/4747/4806）が LVGL ラベル更新後に再描画を強制しない。CoreS3 はタッチが LVGL indev 非登録・blink既定OFF・refreshタイマー自己pauseで listening 中に周期再描画なし → 次タップまで出ない。**修正=3setterに update_layout+invalidate+lv_refr_now**
- [ ] FBb ③近接で上向きっぱなし: `TouchRevertCb`(stackchan.cc:4065)が表情だけidle復帰しサーボ角を戻さない。**修正=同関数に WriteHeadAngles(0,45)（中立 BOOT_INIT_YAW/PITCH）追加**。近接/なで共用タイマーで両方直る
- [ ] FBc ④アイドル自動復帰（60秒）: 既存機構なし→新設。one-shot `idle_settle_timer_`、活動(近接/なで/タップ/move_head/set_indicator)で再武装、最後の操作から60s後に「首→中立・表情→idle・LED→消灯」を1回実行（one-shot＝連続buzz防止、中立なら動かさない）
- [ ] FBd ②ウェイクワード診断ログ: オンデバイス学習は不可確定。現ファーム閾値=0.1（既に低い）。**custom_wake_word.cc:229 の ESP_MN_STATE_TIMEOUT に [DIAG] best_cmd/string/prob ログ追加**（ESP_LOGI）。焼いた後「スタックちゃん」と言って prob 実測 → 次の1回でピンイン/閾値を当て直す
- [ ] FBe rebuild（release.py stackchan、~25分・app-only flash）→ シリアルで boot init（REJECTED/active commands）+ [DIAG] prob 観測 → ①③④再テスト

### R4-list（F・F-2・F-3 統合 E2E）
- [x] プロキシ疎通 `curl localhost:8080/control/status` → ok:true / esp32_connected ✅
- [x] gateway 再起動後の音量再適用 ✅（re-applied volume=100）/ mic_gain 再適用・NVS永続 ✅（re-applied mic_gain=18）
- [x] 仕草トグル OFF で顔振り停止（heartbeat gestures=off 起動ログ確認。ON 復活は実機で要確認）
- [ ] ダッシュボード: スタックちゃんカード表示・タブ順序（左サーバー/右スタックちゃん、初期スタックちゃん）
- [ ] 音量スライダー→実機音量 / ミュート→無音→解除で復帰
- [ ] 顔ステータス: 聞き取り→「きいてるよ」→「考え中」→（検索で「調べ中」）→消去。タップ起動でも同遷移
- [ ] 字幕（reply 下部2-3行）/ route バッジ "H" / Hermes 時 H+青LED
- [ ] 会話ログ欄（route バッジ H=青/ローカル=緑）/ マイクメーター（録音中バー）
- [ ] マイク感度スライダー→実機 mic_gain 反映（スライダー操作で即変化）
- [ ] 緑LED 3経路統一（タップ/ウェイクワード/聞き取りボタンで点灯）← F4b で未確認
- [ ] 近接トグル/閾値→手かざしリフレックス即反映
- [ ] **ウェイクワード「スタックちゃん」検知・夫婦会話での誤検知を数時間観察**
- [ ] 日本語ステータスが□にならない / 日付復唱バグ（短文に日付混入しない・「何曜日？」で即答）

---

# Phase F-3 — マイク感度調整・会話ログ・タブ順序 + ウェイクワード不反応の真因対策（2026-06-13、計画承認済み）

F-2 実機 E2E でユーザー確認: 字幕 ✅ / H+青LED ✅ / マイクメーター ✅ / 日付バグ修正 ✅ / 緑LED ✅。**タブ順序は入れ替え要望**。加えて追加4要望。

## 調査結論（Explore×2、file:line 裏取り済み）
- **「スタックちゃんと呼んでも反応しない」真因 = 語の間違いではない**。generated_assets.bin 実検査で mn7_cn / "su ta ke qiang" / 閾値0.2 は正しく焼けている。真因は構造的: (1) ウェイクワード検知経路に NS/AEC が無く生マイク音声を MultiNet 直送（audio_service.cc:268-281）、(2) マイクゲイン +30dB 過大（cores3_audio_codec.cc:17、ほぼ上限37.5）でテレビ雑音を増幅し SNR 崩壊。マイクゲインは現状コード固定で実行時変更不可
- → **マイク感度調整（要望B）の実装そのものがウェイクワード対策を兼ねる**（一石二鳥）

## ユーザー決定（2026-06-13 AskUserQuestion）
- ウェイクワード最終方針: **「スタックちゃん」単体で粘る**（マイク感度・ピンイン・閾値チューニング。ダメなら再相談）

## 共通確定仕様
- firmware MCP: `self.audio_speaker.set_mic_gain {"gain": int}`（dB 0-36, NVS永続, 既定30維持）
- `POST /control/mic_gain {"gain":int}` / `GET /control/conversation`→`{ok,turns:[{ts,transcript,reply,route,timings_ms}]}` / `GET /control/status` に `mic_gain` 追加

## チェックリスト
- [x] F3-0: 調査2本（firmware mic/wakeword・gateway会話ログ/dashboard、Explore opus 並行）
- [x] F3-1: gateway（Opus agent a9b9609）— control.py に会話ログ deque(maxlen30) + record_conversation_turn/get_conversation、マイクゲイン配線（_send/set/apply_persisted_mic_gain、save/load_state に mic_gain）、hermes_bridge にフック（成立した1往復のみ）、http_server に /control/conversation・/control/mic_gain・status に mic_gain、gateway.py 再接続フックに apply_persisted_mic_gain、stdio_server に set_mic_gain。**706 passed（+23）・ruff OK**
- [x] F3-2: dashboard（Opus agent af4b1f9）— タブ順序入替（左サーバー/右スタックちゃん、初期表示はスタックちゃん維持）+ 会話ログ欄（🗨、route バッジ H=青/ローカル=緑）+ マイク感度スライダー（0-36 step3）+ status_api に /control/conversation 中継。py_compile/node --check OK。バックアップ *.bak-20260613c
- [~] F3-3: firmware set_mic_gain ツール + app ビルド（Opus agent a370f71、バックグラウンド ~25分）— CoreS3AudioCodec::SetInputGain override + MCP登録 + NVS永続 + boot復元 + get_device_status に mic_gain
- [ ] F3-4: app のみ flash（assets 不変）→ シリアルでウェイクワード起動ログ確認（Command/モデルロード）+ ユーザー発話で検知ログ切り分け → gateway/dashboard 再起動（sudo）
- [ ] F3-5: 実機チューニング（マイクゲイン下げてウェイクワード検証。改善せねばピンイン qiang→chang / 閾値20→12 再ビルド）+ 全機能 E2E（会話ログ・感度スライダー・タブ）
- [ ] F3-6: 記録（worklog 追記 / phase-f-report.md / CLAUDE.md / memory）

---

# Phase F — ダッシュボード操作・顔ステータス・仕草OFF・ウェイクワード（2026-06-13 着手、計画承認済み）

計画: ~/.claude/plans/iterative-twirling-bengio.md / ブランチ: feature/phase-f-dashboard（c1-prox-reflex から分岐）

## ユーザー決定事項（2026-06-13 AskUserQuestion）
- ステータス表示は**表情+テキスト両方**（firmware に status label 追加 + フォント差し替え）
- heartbeat 仕草は**デフォルト OFF + ダッシュボードでトグル**（通知発話は維持）
- ウェイクワードは**「スタックちゃん」単体**で反応（「ハイ、スタックちゃん」は不可）→ カスタムウェイクワード（MultiNet ピンイン近似）で実験
- worklog + learning-report（docs/phase-f-report.md）両方作成
- 開発スタイル: Fable=指揮官、実装は Opus エージェント委任（memory 記録済み）

## チェックリスト
- [x] F0: ブランチ作成・todo・memory 記録
- [x] F1: firmware 変更 + release ビルド完了（コミット 549ef72）
  - [x] 変更1: status_label_ + EnsureStatusLabel/SetStatusText + self.display.set_status_text MCP ツール（stackchan.cc）。ラベルは active screen の text font（assets の common puhui、日本語グリフ入り）を継承するので追加フォント不要
  - [!] 変更2: フォント basic→common は **見送り**。調査の結果、実行時フォントは既に assets の `font_puhui_common_20_4.bin`（日本語入り）で、basic は app に焼く小サブセットのフォールバックに過ぎない。CMakeLists を `font_puhui_20_4` にすると (a) app バイナリが ~2MB 増（現 3.42MB / partition 3.94MB → 確実に溢れる）、(b) build_default_assets.py の get_text_font_path が "basic" を要求するため common フォントが assets から外れて逆に日本語が壊れる。→ 指揮官判断待ち（report 参照）
  - [x] 変更3: カスタムウェイクワード "su ta ke qiang"（スタックちゃん）。**sdkconfig 直編集は set-target(fullclean) で消える**ため board config.json の sdkconfig_append に投入（USE_AFE off / USE_CUSTOM on / 3設定 / SR_MN_CN_MULTINET7_QUANT=y）。assets ビルドログで mn7_cn+fst パック・スタックちゃん threshold 0.2 確認 ✅
  - [x] release ビルド完走（BUILD_EXIT_CODE=0、~25分）。**app サイズ 0x36cab0 (3.43MB) / partition 0x3f0000 (3.94MB) → 0x83550 (538KB, 13%) free**。MultiNet モデルは assets 行き（3.8MB、8MB partition に収まる）なので app はほぼ増えず。フォントは basic 据え置きのため肥大なし。**変更2 を入れていたら確実に溢れていた**
  - 変更ファイル: `firmware/main/boards/stackchan/stackchan.cc`(+95)、`firmware/main/boards/stackchan/config.json`。sdkconfig は無変更（ビルド時 config.json append から適用）
  - **flash は app だけでは不足**: ウェイクワードの MultiNet は generated_assets.bin（assets partition 0x800000）に入る。新ウェイクワードを効かせるには assets も flash 必須（詳細は report）
- [x] F2: gateway 完了（Opus agent） — control.py 新規（音量state/ミュート/trigger_listen/set_device_status_text）+ /control/* 8ルート + 認証ガード拡張 + heartbeat set_gestures + esp32_client に on_device_ready フック（接続時の音量再適用、1.5s遅延+リトライ1回）+ hermes_bridge「きいてるよ→考え中→finally消去」+ voice_turn_active フラグ + web_search「調べ中」フック + set_status_text ツール宣言。**テスト 660 件パス（+53）、ruff クリーン**。REST 契約逸脱なし
- [x] F3: dashboard 完了（Opus agent） — status_api.py に _proxy_control（Bearer+Host 付与、未設定503/不達502）+ do_POST 新設。dashboard.html に「🤖 スタックちゃん」カード（接続バッジ/音量/ミュート/聞き取り/近接トグル+閾値/仕草トグル/テスト発話/表情select、ドラッグ中の自動更新上書き抑止・連打防止）。py_compile OK・モック gateway E2E パス。バックアップ: *.bak-20260613。**sudo 手順は status-api drop-in に STACKCHAN_TOKEN（worklog に転記予定）**
- [x] F4: flash 完了 ✅（app + assets、ハッシュ検証 OK）。シリアルで set_status_text ツール / CustomWakeWord「su ta ke qiang→スタックちゃん」+ mn7_cn / WiFi + WS / 近接 NVS(600) 確認。sudo 作業も完了。**プロキシ疎通 ✅**: /control/status が ok:true / esp32_connected:true / volume:50 / heartbeat gestures:false(仕草OFF効) speak:true / proximity:600。**事故: status-api drop-in に日本語トークン混入 → EnvironmentFile=secrets.env 参照に修正して解決**（worklog §4.2 更新済み・再発防止）
- [~] F4b: ユーザー実機 E2E。**2バグ発見→修正中**（ビルド中）:
  - バグ1: 緑LED がタップ時のみ点灯。根本原因 = タップ release のハードコード SetAllRgbLeds(stackchan.cc:2577)、state 連動 LED は GetLed() 未override で死んでいる。修正 = listening 突入エッジ(~2466)に点灯移植で3経路統一
  - バグ2: 顔ステータス「きいてるよ/考え中」が出ない。根本原因 = gateway/firmware とも正常だが RenderAvatarLocked(4450) の move_foreground(avatar) が blink/lip-sync 毎に全画面 avatar を最前面化し label を覆う。修正 = avatar 前面化直後に status_label_ を再昇格
  - 両方 stackchan.cc のみ・gateway 無罪。1ビルドにまとめ、今回は assets 不変＝app のみ flash
  - 調査: investigator(rca-agent) / 修正・ビルド: fw-agent 継続
  - [x] 修正実装（緑LED=listening 突入エッジに点灯移植 / ステータス=avatar 前面化直後に label 再昇格、RenderAvatarLocked+EnsureAvatarObject）→ ビルド成功（app 3.43MB/残13%）→ **app のみ flash 完了・再接続確認 ✅**
  - [x] ステータステキスト表示は実機 OK（ユーザー確認「検索中・調べ中でるようになった」）。緑LED 3経路統一は未確認

# Phase F-2 — 字幕・LLM視覚区別・マイクメーター・タブUI + バグ2件（2026-06-13、設計確定）
ユーザー設計判断: 字幕=下部2-3行折返し / LLM区別=画面Hマーク+LED両方（Hermes=青0,0,32） / Safari=Tailscale HTTPS化 / 進め方=全部まとめて
- [x] FB1: 日付復唱バグ修正（datefix-agent）— local_llm.py _today_line() を断定文→「参考情報・聞かれた時だけ答えろ」枠付け。test 1行更新。660 passed・ruff OK。**実機聴感は要確認**（短文で日付混入しない/「何曜日？」で即答）
- [~] FB2: firmware 3ツール（fw-agent、1ビルド）: self.display.set_subtitle{text}（下部2-3行 LONG_WRAP）/ self.display.set_route_badge{text}（"H"、上部角）/ self.led.set_indicator{r,g,b}（SetAllRgbLeds 公開、0,0,0消灯）。全て status_label_ パターン複製＋RenderAvatarLocked/EnsureAvatarObject 再昇格
  - [x] 実装完了（stackchan.cc +250行）: subtitle_label_（LV_ALIGN_BOTTOM_MID, LONG_MODE_WRAP, width300, max_height78≈3行, text_align_center, 半透明黒）/ route_badge_（LV_ALIGN_TOP_RIGHT, status と非衝突）/ set_indicator（SetAllRgbLeds 公開ラッパ, clamp, 0,0,0消灯）。再昇格は PromoteOverlaysLocked() ヘルパに集約（status+subtitle+route 3つ、RenderAvatarLocked/EnsureAvatarObject から呼ぶ）
  - [x] release ビルド完走（BUILD_EXIT_CODE=0、~25分）。**app 0x36dde0 (3.43MB) / partition 0x3f0000 (3.94MB) → 0x82220 (533KB, 13%) free**（前回比 +3.8KB のみ）。generated_assets.bin は 3,983,046 bytes で前回とバイト同一＝assets 不変 → **app のみ 0x20000 flash で OK**。stackchan.cc のみ変更（+250行）。コミット・flash 未実施
- [ ] FB3: gateway（gw-agent）: control.set_device_subtitle/route_badge/led_indicator + hermes_bridge で reply 字幕・route=hermes時 H+青LED（finally消去）+ audio_stream RMS + GET /control/audio_level{ok,recording,level}。local_llm は触らない。テスト+ruff
- [x] FB4: dashboard 完了（dash-agent）— status_api.py に /control/audio_level 中継。dashboard.html: マイクメーター（録音中のみ120ms≈8Hz ポーリング、level→バー幅%、緑→黄→橙→赤、失敗で静かに停止）+ 2タブ化（#tab-stackchan 初期表示 / #tab-server に温度・CC利用率・フッター）+ ボトムナビ（fixed bottom, safe-area, --green ハイライト）+ loadStackchan はスタックちゃんタブ表示時のみ実行（通信削減）。py_compile/node --check/モックE2E OK。バックアップ *.bak-20260613b
- [ ] FB5: flash（app のみ、assets 不変）+ 統合 E2E
- [ ] FB6: Safari HTTPS（ユーザー作業）: 管理コンソールで HTTPS 有効化 → `sudo tailscale serve --bg --https=443 http://127.0.0.1:8080` → https://razer-server.tailc0a7ab.ts.net/ をブックマーク
- [ ] FB7: 記録（worklog/phase-f-report.md/CLAUDE.md）
- [ ] F5: ウェイクワード実機実験（検知率・誤検知。不足ならピンイン/閾値変えて再試行）
- [ ] F6: worklog + phase-f-report.md + CLAUDE.md ステータス更新

---

# 2026-06-13 — Phase E 仕上げ + LTR-553 再テスト

## 計画（ユーザー承認済み 2026-06-13 朝）
- [x] T1: 本番 conf 差し替え — ユーザーが sudo 実行、`Environment` で 30 分間隔・SPEAK=1・検証用上書き消滅を確認 ✅
- [x] T2: LTR-553 再テスト — **前回結論が覆った**。手かざしに明確に反応（詳細下記）
- [x] T3: ブランチ push — `feature/phase-d`（同期済み）・`feature/phase-e`（新規 push）✅
- [ ] T4: 今夜の聴感②準備 — `rm ~/.stackchan/heartbeat_state.json` + 当日メモ書き足し → 18:00-21:00 ウィンドウでメモリマインド聴感確認（ユーザー実施）

## T2 計測結果（2026-06-13 朝、稼働中 gateway :8767 経由で get_touch_state 連続サンプリング）
| 状態 | ps_raw |
|---|---|
| ベースライン | 368〜388（揺らぎ±10） |
| 手 1〜2cm | ~820〜1090 |
| 手 10cm | ~1035〜**1277**（最強。至近では光軸ズレで反射光が受光部を外れる近接センサー特有の特性） |
| 手 20〜30cm | 393〜448（+50 程度、ノイズと区別しづらい） |

- 結論: **手かざし（〜10-15cm）は十分実用**。前回（6/11）の「前面シェルが光路を完全閉塞」は誤りだった（理由不明。前回はカメラ付近に手をかざしたがセンサー窓の実位置が違った可能性）。部屋スケール（1〜2m）は引き続き ToF Unit 待ち
- 計測ヘルパー: `scratch/mcp_call.py`（単発 tools/call）、`scratch/ltr553_sample.py`（連続サンプリング）。mcp_repl.py は gateway を二重起動するため稼働中サービスとは併用不可 → :8767 直叩きに切替

## ✅ C1 リフレックス有効化 完了（2026-06-13、ブランチ feature/c1-prox-reflex、ユーザー承認: 閾値600・後から変更可能に）
- [x] firmware: PROX_REFLEX_ENABLED / PROX_PS_THRESHOLD を constexpr → NVS 読み込みのランタイム設定に変更（namespace `stackchan_prox`、デフォルト enabled=true / threshold=600）
- [x] firmware: MCP ツール `self.touch.set_proximity_config(enabled, threshold)` 新設（NVS 永続化、再 flash 不要で変更可）。get_touch_state に prox_reflex_enabled / prox_threshold を追加
- [x] gateway: stdio_server.py の tool_map + Tool 宣言に `set_proximity_config` を追加。テスト 607 件パス・ruff クリーン ✅
- [x] ビルド（Docker espressif/idf:v5.5.2、フルビルド ~25分）→ flash（app のみ 0x20000 = ota_0、19秒、NVS 保持）
- [x] 実機 E2E: シリアルログで 3 回発火確認（ps_raw 608 / 2012 / 848 → HAND → look up）、クールダウン抑制ログ・FAR 復帰も正常。gateway 再起動後 `set_proximity_config` の全経路疎通 ✅

## ⚠️ flash 直後の「WiFi に繋がらない」インシデント（原因はルーター、firmware 無実）
- 症状: flash 後 65 秒 `No AP found` → 設定モード（AP Xiaozhi-E79D、白い設定アイコン画面）に落ちた
- 切り分け: NVS ダンプで wifi namespace 残存確認、otadata で ota_0 起動確認 → デバイス側は健全。razer-server の `wpa_cli scan_results` で**自宅ルーター (eoRT) の 2.4GHz 帯 (-g SSID) が電波ごと消えている**ことを発見（5GHz の -a のみ生存）
- 解決: ルーター再起動で 2.4GHz 復活 → デバイス自動再接続（09:03、tools=31）
- 教訓: CoreS3 は 2.4GHz 専用。「flash 直後に繋がらない」はまず環境（ルーターの 2.4GHz 停止・ch12/13 移動）を疑う。NVS/otadata の esptool ダンプで「デバイス側無実」を先に確定させると早い

---

# Phase E — 通知型 heartbeat: 価値があるときだけ話す（2026-06-12 着手）

## ユーザー決定事項（2026-06-12 AskUserQuestion で確認済み）
- **コンセプト転換**: 「ランダムな一言発話」ではなく**通知型** — 沈黙がデフォルト、伝える価値がある情報があるときだけ一言話す（無意味な発話・仕草は不要、とユーザー明言）
- **情報源 v1**: ①メモリマインド（notes.py、夕方ウィンドウ）②天気の急変・注意報（気象庁 bosai API、朝ウィンドウ）
- **クワイエットアワーは 22:00-06:30 に変更**（旧デフォルト 22:00-08:00）
- **天気の対象地域**: 大阪府守口市 — office `270000` / class20 `2720900`（area.json で検証済み 2026-06-12）
- **言い方は v1 テンプレート固定**（決定的・テスト可能）。LLM 委譲は将来の拡張点
- **将来拡張（設計考慮のみ）**: SwitchBot 人感センサーPro / CO2センサー購入予定 → checker は情報源を後から1ユニットで追加できる形を保つ
- 記録: worklog + 学習レポート（phase-e-report.md）両方作成

## チェックリスト
- [x] E1: 対話タイムスタンプの土台 — gateway.note_human_interaction() + voice_turn 冒頭・タッチイベントで記録
- [x] E2: weather.py 新規 — 気象庁 API 取得 + judge_weather() pure 関数。エリアコードは area.json 実データで検証（守口市 2720900 / 大阪府 270000）
- [x] E3: heartbeat.py 通知型拡張 — SPEAK opt-in、抑制5層、checker 巡回、state 永続化。DEFAULT_QUIET を 22:00-06:30 へ
- [x] E4: ユニットテスト — 全 607 件パス（Phase E 新規 42 件: heartbeat 31 + weather 11）、変更ファイルの ruff クリーン
- [~] E5: 実機 E2E — **ログ検証 ✅**（2026-06-12 朝、加速 drop-in で 07:53 天気発話 → 07:54 メモリマインド → 07:55 ネタ切れで沈黙(仕草のみ)、state 永続化確認、TTS エラーなし）。**聴感確認・抑制テスト・本番 conf 差し替えは夜に持ち越し**（手順: worklog §5、conf: scratch/heartbeat-prod.conf）
- [x] E6: 記録 — worklog / phase-e-report.md / CLAUDE.md ステータス / memory（将来センサー拡張）

## 完了条件
- 朝、雨や注意報のときだけ天気を一言教えてくれる — ログ検証 ✅ / 聴感は夜
- 夕方、その日のメモがあるときだけリマインドしてくれる — ログ検証 ✅（加速ウィンドウ）/ 実運用ウィンドウは夜
- 話しかけた直後・録音中・夜間（22:00-06:30）に自発発話が絶対に起きない — ユニットテスト ✅ / 実機抑制ログは夜
- 伝える情報がない日は終日沈黙する — tick 3 で仕草フォールバック確認 ✅

## 夜にやること（5分）
1. `sudo install -m 644 ~/dev/yorishiro-workspace/scratch/heartbeat-prod.conf /etc/systemd/system/stackchan-gateway.service.d/heartbeat.conf && sudo systemctl daemon-reload && sudo systemctl restart stackchan-gateway`
2. `rm ~/.stackchan/heartbeat_state.json` + メモを書き足し → 18:00-21:00 のウィンドウでメモリマインド聴感確認
3. タップ会話の直後に `journalctl -fu stackchan-gateway | grep heartbeat` で `speak suppressed (recent interaction)` を確認

---

# ✅ Phase D 完全クローズ(2026-06-12 朝、E2E 完了)

**D4「Hermes が gateway の MCP ツールを呼ばない問題」は経路分離で解決**:

- 採用: 案 B(`~/.hermes/config.yaml` の `platform_toolsets.api_server` 追記、Hermes 本体無改造)
- 効果: 再起動後の `[stackchan-voice]` で `terminal`/`browser_*`/`write_file`/`read_file` の CallToolRequest **0 件**。家電(`mcp_stackchan_switchbot_send_command`)実灯 ✅、Discord 経路の `terminal` 無回帰 ✅、clarify による聞き返しも実観測 ✅
- 詳細: `docs/phase-d-report.md`(レポート)、`docs/worklog/2026-06-11-phase-d-autonomy.md` §6(作業ログ)
- バックアップ: `/home/kenji/.hermes/config.yaml.bak-20260611-phaseD`

## 宿題 E2E 結果(2026-06-12 朝、ユーザー実機検証)
1. ✅ **メモ単体 E2E**: 06:19 `~/.stackchan/notes/メモ.md`(内容「牛乳を買う」)生成、agent.log: `mcp_stackchan_write_note completed (0.01s, 158 chars)`
2. ✅ **検索単体 E2E**: 06:19 agent.log: `mcp_stackchan_web_search completed (3.44s, 5286 chars)` — 速度から Tavily 経路で稼働中
3. ✅ **Tavily API キー登録**: `~/.yorishiro/secrets.env` に `TAVILY_API_KEY=` 設定済み

## 遠い将来の TODO
- 外部クライアント(Claude Code 等)から `/v1/chat/completions` で `terminal` が必要になったら、案 C(`HERMES_HOME` プロファイル分離)に切替。詳細は `docs/phase-d-report.md` §4.2

## ブランチ・コミット状態(Phase D)
- ブランチ: `feature/phase-d`(push 未実施)
- D4 編集差分: `~/.hermes/config.yaml`(git 管理外)、`docs/worklog/2026-06-11-phase-d-autonomy.md` §6、`docs/phase-d-report.md` 新規、`CLAUDE.md` 最終行、`tasks/todo.md` 本ブロック
- 直前の Hermes プロンプト誘導コミット: `0f997d2 fix(gateway): steer Hermes voice turns to MCP tools via system prompt`(D4 でより構造的解決を選んだが、保険として有効なので残す)

---

# Phase D — 自律性: heartbeat + Hermes MCP ツール追加 + LFM2.5 役割拡大（2026-06-11 着手）

## ユーザー決定事項（2026-06-11 AskUserQuestion で確認済み）
- **スコープ**: 全部。D1 heartbeat → D2 MCP ツール追加 → D3 LFM2.5 役割拡大検討の順に段階実施
- **heartbeat 挙動**: 段階導入。**第1段階は非音声のみ**（表情・首・LED、無音）。発話ありは動作が安定してから opt-in で追加（原則1「夫婦の会話に割り込まない」に配慮）
- **検索バックエンド**: **Tavily 主**（無料 1,000 クレジット/月、クレカ不要、要 TAVILY_API_KEY）+ **ddgs フォールバック**（キー未設定・障害時に自動切替）
- **learning-report**: Phase D 完了時に作成する（docs/phase-d-report.md）

## 調査済みの前提（2026-06-11 サブエージェント調査）
- gateway に定期実行の仕組みは皆無 → heartbeat は asyncio 周期タスクの新規実装。`Gateway.start()` 内で `asyncio.create_task()` する形が最短
- gateway → Hermes 方向は `hermes_bridge.py` の `ask_hermes()`（OpenAI 互換 :8642）実装済み → **heartbeat は gateway 側スケジューラに置けば原則4「Hermes は reactive のまま」を守れる**
- push 送信基盤あり: `ESP32Manager._connection` + `ESP32Connection` の WS send。MCP ツール（set_avatar / move_head / set_led 等）は gateway 内から直接呼べる
- 発火ガードが必要: ESP32 接続中か / 音声パイプライン（TTS/listen）非稼働か / クワイエットアワー
- MCP ツールは現在 31 個（stdio_server.py の list_tools）。検索・ファイルツールはここに追加

## D1: heartbeat（非音声・第1段階）
- [x] D1-1: `stackchan_mcp/heartbeat.py` 新規 (2026-06-11) — asyncio 周期タスク、`Gateway.start()` で起動 / `stop()` でキャンセル。**opt-in**: `STACKCHAN_HEARTBEAT_INTERVAL_MIN` 未設定なら完全無効。ジッター ±25%（`STACKCHAN_HEARTBEAT_JITTER`）
- [x] D1-2: 発火ガード実装 — ①device_connected ②tts_lock.locked()（TTS/listen 共用ロック）③クワイエットアワー（跨ぎ対応、デフォルト 22:00-08:00）。スキップは次 tick 待ち
- [x] D1-3: 仕草 3 種（見回し/表情/うなずき、全て無音・Hermes 不使用）。首は get_head_angles で元角度に復帰、読めなければ首は動かさない。仕草失敗でもループは死なない
- [~] D1-4: 単体テスト 22 件 ✅ / **実機 E2E はユーザーの sudo 作業待ち**（手順: docs/worklog/2026-06-11-phase-d-autonomy.md §5。1分間隔 drop-in → 仕草確認 → 本運用 heartbeat.conf へ差し替え）
- 将来（第2段階、今回はやらない）: `STACKCHAN_HEARTBEAT_SPEAK=1` で Hermes に文脈を渡して一言生成 → say。クワイエットアワー必須のまま

## D2: Hermes MCP ツール追加（検索・ファイル）
- [x] D2-1: `stackchan_mcp/web_search.py` (2026-06-11) — Tavily REST 直叩き（aiohttp、新依存なし、Bearer 認証）→ 失敗/未設定時 ddgs 自動フォールバック。extra `search`（ddgs>=9）追加、.venv に sync 済み。**ddgs 実検索スモーク成功**（日本語クエリ OK）
- [ ] D2-2: ユーザー作業 — Tavily にメール登録し `TAVILY_API_KEY` を `~/.yorishiro/secrets.env` へ追記（キー未設定でも ddgs で動く）
- [x] D2-3: `stackchan_mcp/notes.py` (2026-06-11) — `~/.stackchan/notes/` 配下限定（`STACKCHAN_NOTES_DIR` で変更可）の write_note（append 対応）/ read_note / list_notes。パストラバーサル防止・256KB 上限・.md/.txt のみ。stdio/HTTP MCP に計 4 ツール公開（BYPASS_TOOLS 入り、計 35 ツール）
- [~] D2-4: 単体テスト 19 件 ✅ / **Hermes 経由 E2E は gateway 再起動（sudo）後に**（音声で「〜を調べて」→ web_search → 音声報告 / 「メモして」→ write_note）
- ESP32 キューはバイパス（SwitchBot ツールと同パターン）

## D3: LFM2.5 役割拡大検討（検証中心、コードは最小）
- [x] D3-1: VRAM 確認 (2026-06-11) — **競合は実質なし**。GPU 利用者は Ollama (LFM2.5 0.7GB) のみ（faster-whisper=CPU int8、VOICEVOX=CPU、アイドル時 0 MiB / 6144 MiB）。同時稼働の懸念は解消
- [x] D3-2: ルーティング実績評価 (2026-06-11) — journalctl 集計: route=local 2 件 / hermes 6 件。**閾値拡大はデータ不足で時期尚早**（運用データ蓄積後に再評価）。代わりに**コールドスタート問題を発見**: local の llm 実測 2.8〜3.4s（warm 0.5s のはずが、keep_alive=30m 切れで毎回コールドロード）。**推奨: drop-in に `STACKCHAN_LOCAL_LLM_KEEP_ALIVE=24h` 追加**（コード変更不要、0.7GB 常駐は VRAM 的に許容）→ ユーザー承認後に適用
- [x] D3-3: heartbeat 第2段階の所見 — LFM2.5 はツール呼び出し不可だが「一言生成」なら適役。VRAM 余裕も確認済みで実現性あり（実装は将来の opt-in 段階で）

## フェーズ完了時
- [ ] worklog 更新（docs/worklog/2026-06-11-phase-d-autonomy.md、セッション中随時）
- [ ] learning-report 作成（docs/phase-d-report.md、Phase A/B/C と同テイスト）
- [ ] CLAUDE.md の開発ステータス更新

---

# Phase C 本体 — C1 近接視線追従 + C2 SwitchBot + C3 応答ルーティング（2026-06-11 着手、ユーザー承認: 3 本並行）

## 調査済みの前提（2026-06-11 サブエージェント調査）
- **C1**: firmware に近接センサードライバは無い（Si12T は背面タッチ専用、近接モード未使用）。Grove Port A の外部 I2C は MCP ツール `self.i2c.*` 実装済み（`stackchan.cc:5837`）。サーボは `WriteHeadAngles(yaw, pitch, ms)`（`stackchan.cc:3679`）、表情は `SetAvatarExpressionIfActive()`（`stackchan.cc:4381`）、周期反射の実装パターンは Si12T タッチポーリング（100ms esp_timer、`stackchan.cc:4017`）を踏襲可能
- **C2**: HA はこのマシンにも LAN にも存在しない → **SwitchBot API v1.1 直結の薄い MCP サーバー自作**が現実的。前提は Hub Mini/Hub 2 + 開発者トークン。Hermes には HA REST 直叩きツール（`tools/homeassistant_tool.py`、HASS_TOKEN で有効化）も眠っており、将来 HA を立てれば流用可
- **Hermes MCP 登録形式**: `~/.hermes/config.yaml` の `mcp_servers.<name>` に url/headers（`${VAR}` 環境変数展開対応、`mcp_tool.py:2234`）

## C-fix: Hermes→gateway MCP 認証修復（回帰・最優先）
今朝の STACKCHAN_TOKEN 有効化で /mcp が 401 になり、Hermes が 5 回リトライ後 giving up（05:02 ログ確認済み）。
- [x] Cfix-1: `~/.hermes/config.yaml` バックアップ (config.yaml.bak-20260611) + `mcp_servers.stackchan.headers` に `Authorization: Bearer ${STACKCHAN_TOKEN}` 追加（Hermes は `${VAR}` 展開対応 mcp_tool.py:2234、`~/.hermes/.env` を起動時ロード）
- [x] Cfix-2: ユーザーが token を `~/.hermes/.env` へ追記（grep で値非表示のまま転記）
- [x] Cfix-3: 再起動後 /mcp 200 OK、Hermes が MCP ツール呼び出し成功を E2E で確認 (2026-06-11 06:45)

## C1-0: 近接センサー内蔵有無の確認（C1 の分岐点）
- [x] `mcp_repl.py` で `i2c_scan`（外部 Grove バス）→ 空。**ただしユーザー指摘で CoreS3 内蔵の LTR-553ALS-WA（内部バス 0x23、近接+照度）を発見**（公式 docs.m5stack.com で確認。firmware は未ドライブだった）
- [x] 方針決定（ユーザー承認）: LTR-553 で「手かざしリフレックス」として実装（有効距離 数cm〜10cm。部屋スケールの視線追従は将来 ToF Unit で拡張可）

## C1: 近接リフレックス（firmware、LTR-553 手かざし）→ **物理的に不可と判明、ToF Unit 待ち**
- [x] C1-1/C1-2: LTR-553 ドライバ + リフレックス実装（stackchan.cc、Si12T パターン踏襲、コミット c306f8a）
- [x] C1-3: ビルド → flash → 実機キャリブレーション (2026-06-11) — **結論: 前面シェルに LTR-553 用開口がなく外界の IR が届かない**。感度最大（gain x64・15 パルス・LED 100mA）でも ps_raw はパネル内面クロストークの ~380 で固定、手かざしで全く変化せず
  - 副所見: 当初の閾値 200 < ベースライン 380 で**毎起動誤発火**していた → PROX_REFLEX_ENABLED=false + 閾値 700 に修正して再 flash 済み（ドライバと get_touch_state の ps_raw 診断は温存）
  - 仮にシェルを開口しても有効距離 ~10cm（手かざし専用）。本来の目標「近づくと向く」(1〜2m) には **M5Stack ToF Unit (VL53L0X, Grove Port A, ~¥1,000)** が必要 → 購入はユーザー判断待ち（外出中）
- 学び: `i2c_scan` ツールは外部 Grove バスのみ。内蔵チップは config.h の一覧 + 公式 docs で確認する

## C2: SwitchBot 家電操作（gateway 内 fork 専用モジュール）
前提: Hub Mini ×2（リビング・寝室）確認済み、トークンは `~/.yorishiro/secrets.env` に設定済み
- [x] C2-1: `stackchan_mcp/switchbot.py`（API v1.1、HMAC 署名）+ stdio/HTTP MCP に 3 ツール公開（list_devices / get_status / send_command）。ESP32 キューはバイパス。テスト 19 件
- [x] C2-2: 新サービス不要 — 既存 :8767 MCP HTTP に同居（Hermes 再登録も不要）。secrets.env は既存 EnvironmentFile で自動ロード
- [x] C2-3: E2E 成功 (2026-06-11 06:45-06:48) — 音声「デバイスを教えて」→ Hermes が switchbot_list_devices → 実デバイス一覧を音声報告。「リビングの電気をつけて」→ turnOn 実行（実灯確認: ユーザー回答待ち）

## C3: 応答ルーティング層（gateway `hermes_bridge.py` + `local_llm.py`）
- [x] C3-1: モデル選定 — LFM2.5-1.2B-JP Q4_K_M（warm 0.5s、0.7GB、日本語特化）。gemma3:4b は 1.5-2s で次点
- [x] C3-2: `decide_route()` 純関数（マーカー語 or >30 文字 → Hermes、短文 → local）+ Ollama 直行パス（keep_alive 30m）。**追加修正: 家電操作ワード（つけて/消して/電気/エアコン等）を Hermes マーカーに追加**（ローカルモデルはツールを呼べないため。C2 との整合）
- [x] C3-3: フォールバック実装（local 失敗 → Hermes）+ E2E 確認 — route=local で挨拶応答 OK（初回 3.4s はコールドスタート、以降 ~0.5s）。timings キーは hermes → llm に改名、route フィールド追加
- デプロイ: drop-in `docs/deploy/stackchan-gateway.service.d/local-llm.conf` を /etc に配備済み（外せばロールバック）

## フェーズ完了時
- [x] worklog 更新 + learning-report 作成 (2026-06-11) — `docs/phase-c-report.md`（Phase A/B レポートと同テイスト、約370行）。**Phase C クローズ**（C1 のみ ToF Unit 購入待ちで持ち越し）

---

# Phase C-0 — レイテンシ短縮: VAD 無音自動停止（2026-06-10 ユーザー承認: 案1+案3）

## 前提（調査済み）
- 体感遅延の主因は録音の 30s タイムアウト待ち。タップ停止 (`stackchan.cc:2330`) は実装済みだが、無音検出で自動停止できればタップ2回目も不要
- 案3（返答文字数制限）は **既に実装済み** — `hermes_bridge.py` の `DEFAULT_VOICE_SYSTEM_PROMPT`（1〜3文指定）が毎ターン注入されている → 追加作業なし
- AFE VAD は稼働中（`AudioService::IsVoiceDetected()` 公開済み、`application.h:67` に転送 accessor）だが停止には未結線

## 設計（firmware 最小変更、board ローカル）
- `PollTouchpad()` の既存タイムアウト管理と同じ場所に VAD 監視を追加:
  - `VAD_WARMUP_MS = 800` — listening 突入直後はポップアップ音の自己拾音を避けるため VAD 無視
  - 発話を一度でも検知 (`speech_seen`) した後、無音が `VAD_SILENCE_STOP_MS = 1200` 続いたら `StopListening()`
  - 発話未検知のまま無音でも止めない（VAD 不感への保険。30s タイムアウトが backstop）
- gateway 側変更なし。リスクが顕在化したら定数調整のみで済む構造

## チェックリスト
- [x] C0-1: `stackchan.cc` PollTouchpad に VAD 無音自動停止を実装 (2026-06-10) — speech_seen + 無音 1200ms で StopListening、warmup 800ms、未検知時は止めない（30s backstop 維持）
- [x] C0-2: Docker ビルド成功 (22:42、v2.2.6、mDNS 設定マージ確認)
- [x] C0-3: app flash + 再接続確認 (22:43、tools=30)
- [x] C0-4: 実機テスト合格 (2026-06-10 23:03) — 録音が発話分+1.2s で自動停止（38〜57 frames = 2〜3.4s、従来 498）。**STT 精度も回復**（「お元気ですか」を正確に転写。従来の転写崩壊は 30s の無音尾が whisper を劣化させていたのが原因で、マイクは正常）。ターン全体 9.8s（stt 0.6 / hermes 2.6 / tts 6.6 ※再生込み）。ユーザー所感「とても流暢」
  - 注意: 声が小さい/遠いと VAD が発話を検知せず 30s タイムアウト側に落ちる（22:56 のターンがこれ。設計通りの保険動作）。その場合も再タップで即送信可能
- [x] C0-5: コミット済み — firmware 682c38f (VAD auto-stop) / gateway 8c59987 (STACKCHAN_VOICE_DUMP_DIR 診断ダンプ、env 未設定なら no-op。systemd drop-in は未適用のまま=無効)

---

# Phase B — 音声最小往復: タップ → 録音 → STT → Hermes → TTS → 再生

着手日: 2026-06-10

## 設計確定（2026-06-10 ユーザー決定）
- **判断1（音声構成）**: (a) 完全ローカル — STT/TTS ともローカルエンジン、TTS は VOICEVOX
- **判断3（Hermes 接続）**: (b) MCP stdio — Hermes が MCP クライアントとして gateway のツール群を利用
  - 音声会話ターンの注入（gateway → Hermes 方向）は Hermes 内蔵の **APIServerAdapter**（OpenAI 互換 HTTP、port 8642、`API_SERVER_ENABLED=true` で有効化）を使用

## 調査済みの前提（2026-06-10 サブエージェント調査）
- **firmware**: 音声系は全部生きている。Opus enc/dec（esp_audio_codec、16kHz mono 60ms）、AW88298 スピーカー / ES7210 マイク（`boards/stackchan/cores3_audio_codec.cc`）、WS バイナリ音声フレーム送受（`websocket_protocol.cc`）、画面タップ→StartListening（`stackchan.cc` PollTouchpad）→ **自前実装は不要、結線と検証のみ**
- **gateway**: STT orchestrator（faster-whisper / openai-whisper）+ TTS orchestrator（VOICEVOX→Opus→WS 送信）実装済み。タッチ起動録音は `STACKCHAN_AUDIO_HOOK_URL` へ Ogg/Opus を POST する仕組みあり（**現状 URL 未設定でフレーム破棄中**）
- **Hermes**（`~/.hermes/hermes-agent`、systemd `hermes-gateway` で稼働中）: MCP クライアント内蔵（`hermes mcp add` で stdio サーバー登録可、現在 mcp_servers 未設定）。APIServerAdapter は `gateway/platforms/api_server.py`、`POST /api/sessions/{id}/chat` でセッション固定可。プロファイルは API 経由で指定不可＝起動時のアクティブプロファイル固定
- 注意: gateway の WS :8765 は 1 プロセス占有。検証中は standalone 起動、B7 で Hermes spawn に切り替える際は二重起動に注意

## ゴール（Phase B 完了条件）
画面タップ → 話しかける → StackChan が Hermes の応答を VOICEVOX 音声で喋る（応答遅め可）

## チェックリスト
- [x] B1: 足回り確認 (2026-06-10) — gateway に `tts` + `stt-faster-whisper` extras 導入。VOICEVOX は旧 yuno 残骸 `~/trash/` 行きだったエンジン本体 (2.1GB) を `~/apps/voicevox/` へ救出し、unit を drop-in (`voicevox.service.d/override.conf`) でパス修正して復旧 (v0.25.2, :50021)
- [x] B2: TTS 単体 (2026-06-10) — `say` → VOICEVOX → Opus 91 frames 実機送信成功（初回 8.6s、VOICEVOX ウォームアップ込み）。**スピーカーからの実音確認はユーザー帰宅後**
- [x] B3: STT 単体 (2026-06-10 実機確認) — 画面タップ → 録音 → faster-whisper 転写動作確認。※転写品質に課題: 30s 録音中の発話が 'ん'/'うっ' としか転写されないターンあり（録音窓が長すぎる影響の可能性、タップ停止運用で再評価）
- [x] B4: Hermes APIServerAdapter 有効化 (2026-06-10) — drop-in (`hermes-gateway.service.d/api-server.conf`) で `API_SERVER_ENABLED=true` → 127.0.0.1:8642 で /health OK、実モデルと 1 ターン疎通成功
- [x] B5: voice-turn receiver 実装 (2026-06-10) — `stackchan_mcp/hermes_bridge.py` 新規 + `capture_server.py` に `/voice_turn` ルート 1 箇所追加（fork 独自・upstream 非送付）。`STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8766/voice_turn` で自分自身に向ける構成
- [x] B6 (シミュレート版): E2E 成功 (2026-06-10) — VOICEVOX 合成音声を firmware と同形式の Ogg/Opus で `/voice_turn` に POST（`scratch/test_voice_turn.py`）→ STT「好きな食べ物はある?」→ Hermes「ラーメンかな」→ TTS 164 frames 実機送信。**ウォーム時 18.3s（STT 0.7s / Hermes 3.1s / TTS 14.6s ※音声 9.8s のリアルタイム送出込み、合成自体 ~5s）。発話終了→声出し ~8.6s**
- [x] B6 (実機版): タップ→会話成立をユーザー確認 (2026-06-10 夜) ✅ **Phase B 完了条件達成**。実測 timings: total 20.1s / 13.7s（録音 30s タイムアウト分は除く）
- [x] **レイテンシ分析 (2026-06-10)** — 体感 10〜30s の主因は**録音が毎回 30s タイムアウトまで継続**（498 frames）していたこと。タップ停止は firmware 実装済み（`stackchan.cc:2330` listening 中の短タップ → StopListening）だが運用されていなかった。処理自体は STT 1.4〜2.3s + Hermes 2.6〜3.4s + VOICEVOX 合成 2〜4s = 6〜9s。上限定数: firmware `stackchan.cc:2251 LISTEN_TIMEOUT_MS=30000` / gateway `stt/orchestrator.py:63 MAX_DURATION_MS=30000`。AFE VAD は稼働中だが自動停止には未結線（LED 表示のみ、`application.cc:232`）→ 短縮策は Phase C 候補
- [x] B7: Hermes→gateway MCP 接続 (2026-06-10、**方式(b) 常駐+HTTP MCP で稼働確認済み**) — Streamable HTTP サーバーは upstream 実装済み (`stackchan-mcp serve --transport streamable-http`、:8767) で追加コード不要。`stackchan-gateway.service` 稼働中（`docs/deploy/` に unit、enable 済み）、`~/.hermes/config.yaml` に mcp_servers.stackchan 登録（バックアップ: config.yaml.bak-20260610）。**Hermes が say ツールを MCP 経由で呼び出し、結果を報告するところまで確認済み**（ESP32 未接続のため発話自体は未達）
- [x] B8 (API_SERVER_KEY): 生成・適用済み — gateway 側 `~/.yorishiro/secrets.env` (HERMES_API_KEY)、Hermes 側 drop-in。キー認証 + `X-Hermes-Session-Id: stackchan-voice` でセッション継続通信を確認
- [x] B8 残り (STACKCHAN_TOKEN): 完了 (2026-06-11 朝) — firmware 側 `sdkconfig.defaults.local` の `CONFIG_DEFAULT_WEBSOCKET_TOKEN`（NVS 空なので fallback が効く、FORCE 不要）+ gateway 側 `~/.yorishiro/secrets.env` の `STACKCHAN_TOKEN`。リビルド・flash・gateway 再起動後、実機は認証付き接続成功、誤トークンは **HTTP 401 で拒否**を確認。**Phase B 全項目クローズ** 🎉。feature/phase-b-voice は origin に push 済み
- [x] **ESP32 オフラインの原因特定** (2026-06-10) — firmware の `PowerSaveTimer(-1, 60, 300)` が「WS 切断のまま 5 分」で AXP2101 PowerOff を発動（USB 給電でも切れる）。gateway 入れ替え時の切断 >5 分で発動した。タッチでは復帰不可、**電源ボタン（長押し）で起動**
- [x] **firmware 修正: 自動電源オフ無効化** (2026-06-10, コミット 8490088) — `boards/stackchan/stackchan.cc` を `PowerSaveTimer(-1, 60, -1)` に変更（画面減光は維持、shutdown のみ無効）。**ビルド成功済み**（`build/xiaozhi.bin` 13:47、v2.2.6、mDNS 設定マージ確認済み）
- [x] **実機 flash 完了** (2026-06-10 22:04) — app のみ書き込み（`Hash of data verified.`）、リブート後 gateway へ自動再接続確認（tools=30）。直後に MCP HTTP (:8767) 経由で `say` 実行 → 83 frames 送信成功（B2 実機版、**実音はユーザー確認待ち**）

## 次セッション再開手順（2026-06-10 clear 時点）

1. **実機復帰**: ユーザーが電源ボタン（長押し）で起動 → 自動で gateway へ接続（~13 秒）。`journalctl -u stackchan-gateway -f` で確認
2. **flash（ユーザーが USB 接続したら、電源オフ無効化の根治に必要）**: app のみで WiFi 設定は保持される:
   `~/.venvs/esptool/bin/esptool --port /dev/ttyACM0 --baud 460800 write-flash 0x20000 firmware/build/xiaozhi.bin`
   ※シリアルポートを開くだけでリセットされる点に注意（todo の Phase B-0 学び参照）
3. **実機確認（B2/B3/B6 実機版）**: ①`say` で音出し（Hermes API 経由か、service を止めて mcp_repl.py）②画面タップ→話す→タップ→返事 ③Discord で Hermes に「StackChan で喋って」
4. その後: STACKCHAN_TOKEN 設定（B8 残り）、Phase B クローズ → worklog 更新・learning-report 提案

### 現在の常駐構成（全部 systemd、自動起動）
- `voicevox.service` (:50021) / `stackchan-gateway.service` (:8765 WS, :8766 capture+voice_turn, :8767 MCP HTTP) / `hermes-gateway.service` (Discord + :8642 API)
- Hermes→gateway は MCP 登録済み（`~/.hermes/config.yaml` mcp_servers.stackchan）。秘匿値は `~/.yorishiro/secrets.env`
- 開発時に gateway を手で動かす場合: `sudo systemctl stop stackchan-gateway` してから `scratch/mcp_repl.py`
- [x] 追加: mDNS 広告アドレス固定 `STACKCHAN_MDNS_ADVERTISE_ADDR` 実装 (23ef800) — ESP32 接続 ~50s → **13s**
- [x] 追加: 学習用 worklog 開始 — `docs/worklog/2026-06-10-phase-b-voice.md`（毎セッション継続、memory 登録済み）

## Phase B での学び・メモ
- Hermes API はステートレス (`/v1/chat/completions`) なら認証不要だが、**セッション継続 (`X-Hermes-Session-Id`) には `API_SERVER_KEY` 設定が必須**（B8 で対応）
- VOICEVOX 初回リクエストはウォームアップで +数秒。`voicevox.service` は ExecStartPost で /version 待ちするので起動完了 = 即応答可
- faster-whisper 初回 transcribe はモデルロードで ~19s、以降 ~0.7s。gateway 起動時のプリロードは Phase C の最適化候補
- TTS の所要時間は Opus フレームのリアルタイム送出（60ms/frame）が支配的。体感短縮には文分割ストリーミングが Phase C 候補
- 検証ツール: `scratch/mcp_repl.py`（gateway 常駐 + コマンドファイル経由でツール実行）、`scratch/test_voice_turn.py`（実機タップ不要の E2E）

## 着手前確認（2026-06-10 ユーザー回答済み）
1. STT エンジン: **faster-whisper**（gateway 内蔵、CPU int8 で VRAM 温存。遅ければ whisper.cpp に切替可）
2. Hermes のプロファイル: **今のまま（default）** — 起動コマンド変更なし
3. B4 の hermes-gateway.service 変更: **承認済み**（127.0.0.1:8642 bind のみ、environment 1 行）

---

# Phase B-0 — 実機疎通: WiFi 投入 + gateway 起動 + set_avatar 経路確認

着手日: 2026-06-10

## ゴール
配網モード待機中の CoreS3 を自宅 WiFi に接続し、razer-server 上の gateway と WebSocket 疎通させ、MCP 経由で `set_avatar` コマンドが通ることを確認する。

## 前提（調査済み・根拠は firmware/gateway 内コード）
- WiFi 投入は配網 AP (Xiaozhi-E79D) → `http://192.168.4.1` の web UI から（captive portal 方式）
- 現ビルドは **mDNS 無効**のため、web UI の Advanced タブで gateway URL `ws://192.168.0.19:8765/` の**手動入力が必須**
- トークンは firmware 側空 / gateway 側 `.env` 未作成 = 認証なしで整合 → 疎通後に両側へ設定（任意）
- **avatar 画像は placeholder（1×1 黒点）**: `set_avatar` は通るが顔は表示されない。顔表示は B0-7 で別途対応

## チェックリスト
- [x] B0-1: gateway 依存インストール — `uv sync` 完了 (stackchan-mcp v0.10.0)
- [x] B0-2: razer-server のポート開放確認 — ufw は **inactive**（ファイアウォール無効）と確認、ブロック要因なし (2026-06-10)
- [x] B0-3: gateway 起動（認証なしモード、`VISION_HOST=192.168.0.19` 付き）+ 接続検知で自動 set_avatar する probe (`scratch/mcp_probe.py`) 稼働中
- [x] B0-4: WiFi 投入完了（SSID: eoRT-1127969-g、device IP: 192.168.0.10）。ただし Advanced タブの gateway URL は未入力 → `WS_URL not configured` で接続先不明に
- [x] B0-4b: **方針転換（ユーザー承認済み）**: `sdkconfig.defaults.local`（gitignore 済み）に `CONFIG_STACKCHAN_MDNS_DISCOVERY=y` + `CONFIG_DEFAULT_WEBSOCKET_URL="ws://192.168.0.19:8765/"` を設定して再ビルド → **app パーティションのみ書き込み（NVS=WiFi 設定は保持）**
  - mDNS が無効だった原因: set-target 時点ではボード未選択 → default n で確定し、後段のボード append では反映されない
  - sdkconfig 手編集は release.py の set-target で再生成されるため不可。sdkconfig.defaults.local が正規の上書き手段
- [x] B0-5: 再書き込み後、ESP32 が mDNS で gateway を自動発見し WebSocket 接続成功（device_id: 44:1b:f6:e1:e7:9c、tools_count: 30）
- [x] B0-6: `set_avatar idle` → `{"face":"idle","ok":true}` 成功 (2026-06-10)。**Phase B-0 のゴール達成**
- [x] B0-7: avatar アセット — **経路(b) ビルド焼き込みで実装まで完了** (2026-06-10)
  - 既製の顔 PNG はどこにも配布されていない（m5stack-avatar はコード描画ライブラリで画像なし）
  - `scratch/gen_avatar_faces.py`（PIL）で公式風の顔 14 枚を生成 → `~/.stackchan/avatar/` → `convert_avatars.py` → クリーンリビルド → app flash
  - **実機 LCD に顔表示をユーザー確認済み** ✅。顔の調整は gen_avatar_faces.py の数値変更 → 再生成 → リビルドで何度でも可能
  - 経路(a) `load_avatar_set`（PSRAM 揮発・ホットスワップ用）は Phase B 後半の表情差し替えで活用予定

## Phase B-0 での学び・メモ
- **USB-Serial/JTAG はポートを開くだけでデバイスがリセットされる**（`rst:0x15 USB_UART_CHIP_RESET`）。serial_watch.py で dtr/rts=False にしていても発生。WS 接続検証中はシリアルに触らないこと
- **mDNS 候補順の無駄**: gateway が Tailscale IP / docker bridge IP も広告するため、ESP32 は到達不能な候補で各 ~18 秒タイムアウトしてから LAN IP に到達する（起動〜接続まで ~50 秒）。改善候補: gateway の mdns_advertiser に広告アドレスのフィルタを入れる（upstream 改修 or local patch、Phase B で検討）
- sdkconfig は gitignore 済み。ローカル上書きは `sdkconfig.defaults.local`（同じく gitignore 済み）が正規手段で、release.py が set-target 後にマージしてくれる
- app のみ flash（`esptool write-flash 0x20000 build/xiaozhi.bin`）で NVS の WiFi 設定は保持される。確認済み
- gateway + 疎通プローブは `scratch/mcp_probe.py`（gateway を stdio MCP 子プロセスとして起動し、接続検知で set_avatar を自動実行）

## Out of scope（Phase B 本体）
- Opus 音声ストリーム、whisper.cpp / VOICEVOX 連携、Hermes 通信プロトコル（設計判断 1・3）
- STACKCHAN_TOKEN の本設定（疎通確認後に実施）

---

# Phase0 — Hermes Agent 身体化 / ESP-IDF 環境構築〜実機書き込み

Plan: `/home/kenji/.claude/plans/phase0-snappy-haven.md`

## ゴール
razer-server で Docker ベース ESP-IDF v5.5.2 ビルド環境を整え、M5Stack CoreS3 へ本 fork firmware を書き込み、「触ると首が動く」状態まで持っていく。

## 完了条件 (Phase A 完了条件相当) — **2026-06-10 全達成 ✅**
- [x] `docker run espressif/idf:v5.5.2 idf.py --version` 成功
- [x] `python scripts/release.py stackchan` 成功 (`build/merged-binary.bin` 生成)
- [x] CoreS3 へ flash 成功 (`Hash of data verified.`)
- [x] Boot ログで `Servo power ENABLED via PY32 pin 0` 確認
- [x] **首動き目視**: boot-init で pitch 0→45°（ユーザー確認済み。ログでも ReadPos 626→759 の物理移動を裏付け）
- [x] **背面なで反応**: ユーザー確認済み。シリアルに `touch event: STROKE ... duration=1699 ms` 記録

avatar の LCD 表示、画面タップ反応、WS 接続、gateway 起動は **Phase B 以降**。

## チェックリスト

### 環境構築
- [x] T1: Docker engine — 既に install 済み (v29.5.2, 2026-05-25 から稼働)、kenji は docker グループ所属
- [x] T2: esptool/pyserial venv — `~/.venvs/esptool` に esptool v5.3.0 + pyserial 3.5
- [x] T3: submodule (smooth_ui_toolkit v2.12.0) を init
- [x] T4: ESP-IDF v5.5.2 Docker image を pull
- [~] T5: avatar_images.local.cc 生成 — **スキップ**: PNG ソース不在 + Phase0 では avatar 表示しないので placeholder で OK

### ビルド・書き込み
- [x] T6: `python ./scripts/release.py stackchan` を Docker で実行 — `build/merged-binary.bin` (9.6M) 生成、error なし
- [x] T7: M5Stack CoreS3 を USB-C で接続、`/dev/ttyACM0` 出現確認 — USB-Serial/JTAG mode、MAC 44:1b:f6:e1:e7:9c
- [x] T8: 初回 flash — 460800bps で 39 秒、`Hash of data verified.`
- [x] T9: シリアル監視 — `Servo power ENABLED` / `Boot pre-init ReadPos` / `Si12T: init OK` / panic なし。WiFi 未設定のため配網モード (AP: Xiaozhi-E79D) で待機 = Phase0 では正常
- [x] T10: 首動き + 背面なで反応をユーザー目視確認 (2026-06-10)

## Phase0 での学び・メモ
- シリアルログ収集は `scratch/serial_bootlog.py` (リセット付き) / `scratch/serial_watch.py` (監視のみ) を使用
- USB-Serial/JTAG のリセット: DTR(IO0)=False のまま RTS(EN) をパルス。**DTR を True にすると DOWNLOAD モードに入る**ので注意
- 次フェーズ (Phase B) の前提: WiFi SSID/PASS 投入 + STACKCHAN_TOKEN 整合 + gateway 起動 → set_avatar で顔表示確認から
- upstream に v0.10.0 + firmware-v1.10.0 がリリース済み (2026-06-09)。差分は gateway mDNS advertiser 修正 + firmware mdns_gateway_discovery 改善 + Cloudflare Workers relay example。main は v0.9.1 のまま → 同期は要ユーザー判断

## 環境メモ (Phase0 着手時)
- razer-server (= 本機, Ubuntu Server 24.04, x86_64, GTX 1060 6GB)
- `groups`: `kenji adm dialout cdrom sudo dip plugdev lxd docker ollama`
- `python3 --version`: 3.12.3
- `docker --version`: 29.5.2
- ディスク残量: 76GB / 124GB

## Out of scope (Phase B 以降)
- gateway (`stackchan_mcp/`) の uv sync / 起動
- STACKCHAN_TOKEN の sdkconfig <-> .env 整合
- WiFi SSID/PASS の sdkconfig 投入、WebSocket 接続
- avatar の LCD 表示確認 (Issue #77)
- 画面 (FT6336) タップ反応確認
- yuno-chan-api / voice_server / 音声系 (Phase 4)
- Home Assistant 連携 (Phase C)
- LFM2.5 ローカル LLM 統合 (Phase D)
