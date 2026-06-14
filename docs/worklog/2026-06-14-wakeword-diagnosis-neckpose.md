# 2026-06-14 ②ウェイクワード決定的診断 + ①③④フォロー + 首角度ダッシュボード調整

ブランチ: `feature/phase-f-dashboard`。このセッションのゴールは「ウェイクモードの問題を解決する」。

## このセッションの3成果（要約）

1. **② ウェイクワード不発の根本原因を「直接測定」で確定** → インフラは完全に無実で、「スタックちゃん」を中国語ピンインで MultiNet に載せる方式そのものの限界。→ タップ/背面なで運用でクローズ（設計原則①と整合）。副産物として **mic_gain=12dB が痩せすぎ**と判明し 30dB へ。
2. **① 顔ステータス「きいてるよ」のワンテンポ遅れの根本原因を特定 → gateway で修正**（firmware 無罪）。
3. **③④ 首の中立姿勢をダッシュボードのジョイスティックで実行時調整・NVS保存できる機能を新規実装**（firmware / gateway / dashboard の三層）。以後この手の「見た目の好み」調整は再ビルド不要。

---

## 1. ② ウェイクワード — なぜ「測定」が決め手だったか

### 問題の構造
これまでの調査でインフラは正常と分かっていた（MultiNet `mn7_cn` ロード・ピンイン12候補登録・閾値0.10）。にもかかわらず実発話で検知ゼロ。前セッションは「アイドル時にマイク音声が検出器に届いていないのでは（入力ゼロ説）」まで絞り込んだが、**既存の `[DIAG]` prob ログでは確定できなかった**。理由: MultiNet は TIMEOUT 時に `get_results()` が空（num=0）を返すので、prob は入力に関係なく常に 0.000 になる。つまり prob ログは原理的に「入力が来ているか」を判別できない。

### 決め手 = Feed に渡る音の RMS を測る
`custom_wake_word.cc` の `Feed()` 内、`multinet_->detect()` に渡る **chunk そのもの**の RMS（音の大きさ）を ~1秒間隔でログ出力した。同じ行に `codec input_enabled` と `running` 状態も併記。これで「MultiNet が実際に見ている音」を直接観測できる。

### 測定結果（実機・ユーザー発話）

| 状態 | peakRMS | input_enabled | MultiNet 処理 | 検知 |
|---|---|---|---|---|
| 静音 | 10〜20 | 1 | detecting 31 chunks/秒 | — |
| 発話 @ gain 12dB | ~110（-49dBFS） | 1 | 同上 | ゼロ |
| 発話 @ gain 30dB | 608〜1164（-29dBFS） | 1 | 同上 | **ゼロ** |

### 結論（3分岐の確定）
- **(C) 入力ゼロ説 → 否定**。`input_enabled=1`・MultiNet は毎チャンク処理（取りこぼし0）。音声経路は完全に健全。
- **副産物**: gain 12dB は発話が RMS~110 しか出ず痩せすぎ。これは STT（タップ会話）の精度にも悪影響。`set_mic_gain 30`（実行時・再ビルド不要）で健全レベル RMS~1000 に。
- **(A) MultiNet ピンイン認識の限界 → 確定**。健全な音量・12候補・最敏感閾値0.1 でも検知ゼロ。「スタックちゃん（日本語）」を中国語ピンイン近似で MultiNet に載せる方式そのものが力不足。
- **ユーザー方針決定**: タップ/背面なで運用でクローズ（両方正常動作・設計原則①「明示トリガー優先・ウェイクワードは後回し」と整合）。将来は microWakeWord（TFLite・日本語学習可）が別フェーズの候補。

### 学びの核
「prob=0.000」を「入力ゼロ」と早合点しなかったのが正解だった。**診断ログは"何を測れて何を測れないか"を理解して設計する**。今回は「認識器の出力(prob)」ではなく「入力の物理量(RMS)」を測ることで、認識器とマイク経路を完全に切り分けられた。

---

## 2. ① 顔ステータス「きいてるよ」のワンテンポ遅れ

### 根本原因（gateway 側の構造的遅延）
「きいてるよ」(STATUS_LISTENING) は `hermes_bridge.py:344` で送られていたが、そこに到達するのは **タップ→録音終了→OGG音声をHTTPで受信(:297)→PCMデコード(:321) が全部終わった後**。つまりユーザーが話し終わるまで「きいてるよ」が出ず、本質的に遅れる。firmware の描画（`lv_refr_now`）は無罪。

### 修正
`esp32_client.py` に `on_listen_started` フックを追加し、デバイスが録音を始める最速の信号（`state=="start"`）の直後に `set_device_status_text(STATUS_LISTENING)` を即送出（`gateway.py` で配線）。既存の `hermes_bridge.py:344` は冪等なので残置。テスト +18（749 passed）・ruff クリーン。

---

## 3. ③④ 首の中立姿勢をダッシュボードで調整（ジョイスティック）

ユーザー提案: 首の角度のような「見た目の好み」を25分の再ビルドで合わせるのは無駄 → ダッシュボードでライブ調整＋保存したい。既存の `set_proximity_config`（NVS永続・実行時変更）が完璧な雛形だった。

### 構成図
```
[Dashboard :8080  ~/razer-dashboard/dashboard.html（非git）]
   🕹 ジョイスティックパッド（ドラッグ→x:yaw / y:pitch）
   │   ライブ: 120ms throttle で POST /control/head
   │   保存:   POST /control/neutral_pose
   ▼ （status_api.py が /control/* を汎用転送）
[Gateway :8767  control.py / http_server.py / stdio_server.py]
   set_head_angle   → self.robot.set_head_angles  （既存・ライブ移動）
   set_neutral_pose → self.robot.set_neutral_pose  （新規・保存）
   ▼ WebSocket MCP
[Firmware  stackchan.cc]
   self.robot.set_neutral_pose{yaw,pitch}
     → clamp → NVS namespace "stackchan_pose" 書込 → 即 WriteHeadAngles
   neutral_yaw_ / neutral_pitch_（NVS解決、既定 0 / 38）
     ↑ boot-init・③TouchRevertCb・④idle settle の3経路がこれを参照
```

### 設計のポイント
- **中立は単一の出所**: firmware で中立を参照していた3経路（boot / 近接・タッチ復帰 / アイドル復帰）はすべて定数 `BOOT_INIT_PITCH_DEG` を見ていた。これをメンバ `neutral_yaw_`/`neutral_pitch_`（boot で NVS から解決、既定 0/38）に一本化。これで「一度保存すれば全経路が一貫して新しい中立に戻る」。
- **③の角度修正もこれで解決**: ユーザーが「中立が上向きすぎ」と言った 45° を、既定 38° に下げつつ、最終的にはジョイスティックで好きな値に保存できる。
- **boot のサーボ不変条件を維持**: 起動時の seed（current_deg）と target（WriteHeadAngles）を同じ neutral 値にすることで `start==target`（#138/#115 の安全シーケンス）を壊さない。
- **ライブと保存を分離**: ドラッグ中は `set_head_angles`（移動だけ・揮発）、「保存」で初めて `set_neutral_pose`（NVS永続）。throttle 120ms（≒8Hz、既存マイクメーターと同じ）で連投を抑制。

---

## 4. 用語解説

- **MultiNet / WakeNet（esp-sr）**: WakeNet は固定ウェイクワード（「Hi 小智」等）の専用学習済み音響モデルで高精度。MultiNet は「ウェイク後のコマンド認識」用で、中国語ピンインでコマンドを登録できる。本fork はカスタムウェイクワードを MultiNet のコマンドとして常時走らせていたが、日本語「スタックちゃん」のピンイン近似では認識に届かなかった（今回の確定）。
- **RMS / dBFS**: RMS は信号の実効的な大きさ（二乗平均平方根）。16bit音声のフルスケールは 32767。RMS≈1000 は約 -29dBFS で ASR に適した健全レベル、RMS≈110 は約 -49dBFS で痩せすぎ。
- **NVS（Non-Volatile Storage）**: ESP32 のキーバリュー不揮発ストレージ。再 flash しても消えない設定置き場。`set_proximity_config`・`set_mic_gain`・今回の `set_neutral_pose` がここに保存。
- **MCP tool（`self.robot.*` 等）**: firmware が公開する操作。gateway が WebSocket 越しに呼び、Hermes やダッシュボードから利用される。
- **throttle**: 連続イベント（ドラッグ）で送信頻度を間引くこと。ここでは末尾送信込み 120ms。

---

## 5. デプロイ・コミット状態

- **firmware**: 中立NVS化 + `set_neutral_pose` + ③38化 + 診断ログ除去込みで clean rebuild（assets 不変）→ app-only flash（0x20000、Hash verified）。デバイス再接続・mic_gain30 再適用確認。
- **gateway**: 本repo に変更（restart 後に①即時化・新ルート有効化）。749 passed・ruff OK。
- **dashboard**: `~/razer-dashboard/dashboard.html`（**非git**・バックアップは `.bak-20260614`）。ファイル編集が即デプロイ。
- **mic_gain**: NVS=30（STT にも有効）。ダッシュボードのマイク感度スライダーで随時調整可。
- **②の診断ログ**: `custom_wake_word.cc` を HEAD に revert して除去済み（commit には乗らない）。
- **未作成の積み残し**: `docs/phase-f-report.md`（Phase F 全体の learning-report）。

## 学び
1. 診断ログは「測れること/測れないこと」を理解して設計する（prob ではなく RMS）。
2. プロンプト/設定より**構造で切り分ける**（入力の物理量 vs 認識器の出力）。
3. 「見た目の好み」調整は実行時 NVS 化して再ビルドの輪から外す（set_proximity_config パターンの再利用）。
4. 過剰補正に注意（mic_gain を下げ続けてウェイクワードを直そうとした結果、STT まで痩せていた）。
