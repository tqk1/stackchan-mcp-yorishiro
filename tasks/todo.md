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
- [ ] B3: STT 単体 — 画面タップ → `listen.start` → 転写確認【実機操作が必要・ユーザー帰宅後】
- [x] B4: Hermes APIServerAdapter 有効化 (2026-06-10) — drop-in (`hermes-gateway.service.d/api-server.conf`) で `API_SERVER_ENABLED=true` → 127.0.0.1:8642 で /health OK、実モデルと 1 ターン疎通成功
- [x] B5: voice-turn receiver 実装 (2026-06-10) — `stackchan_mcp/hermes_bridge.py` 新規 + `capture_server.py` に `/voice_turn` ルート 1 箇所追加（fork 独自・upstream 非送付）。`STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8766/voice_turn` で自分自身に向ける構成
- [x] B6 (シミュレート版): E2E 成功 (2026-06-10) — VOICEVOX 合成音声を firmware と同形式の Ogg/Opus で `/voice_turn` に POST（`scratch/test_voice_turn.py`）→ STT「好きな食べ物はある?」→ Hermes「ラーメンかな」→ TTS 164 frames 実機送信。**ウォーム時 18.3s（STT 0.7s / Hermes 3.1s / TTS 14.6s ※音声 9.8s のリアルタイム送出込み、合成自体 ~5s）。発話終了→声出し ~8.6s**
- [ ] B6 (実機版): タップ→会話成立の確認【ユーザー帰宅後】
- [x] B7: Hermes→gateway MCP 接続 (2026-06-10、**方式(b) 常駐+HTTP MCP で稼働確認済み**) — Streamable HTTP サーバーは upstream 実装済み (`stackchan-mcp serve --transport streamable-http`、:8767) で追加コード不要。`stackchan-gateway.service` 稼働中（`docs/deploy/` に unit、enable 済み）、`~/.hermes/config.yaml` に mcp_servers.stackchan 登録（バックアップ: config.yaml.bak-20260610）。**Hermes が say ツールを MCP 経由で呼び出し、結果を報告するところまで確認済み**（ESP32 未接続のため発話自体は未達）
- [x] B8 (API_SERVER_KEY): 生成・適用済み — gateway 側 `~/.yorishiro/secrets.env` (HERMES_API_KEY)、Hermes 側 drop-in。キー認証 + `X-Hermes-Session-Id: stackchan-voice` でセッション継続通信を確認。**STACKCHAN_TOKEN は実機確認後に別途**
- [x] **ESP32 オフラインの原因特定** (2026-06-10) — firmware の `PowerSaveTimer(-1, 60, 300)` が「WS 切断のまま 5 分」で AXP2101 PowerOff を発動（USB 給電でも切れる）。gateway 入れ替え時の切断 >5 分で発動した。タッチでは復帰不可、**電源ボタン（長押し）で起動**
- [x] **firmware 修正: 自動電源オフ無効化** (2026-06-10, コミット 8490088) — `boards/stackchan/stackchan.cc` を `PowerSaveTimer(-1, 60, -1)` に変更（画面減光は維持、shutdown のみ無効）。**ビルド成功済み**（`build/xiaozhi.bin` 13:47、v2.2.6、mDNS 設定マージ確認済み）。**残: 実機への flash**

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
