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
