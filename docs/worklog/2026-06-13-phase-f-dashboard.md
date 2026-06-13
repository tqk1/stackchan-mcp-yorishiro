# 2026-06-13 Phase F — ダッシュボード操作・顔ステータス・仕草OFF・ウェイクワード

## 1. 概要（何をしたか）

日常使いの操作性を上げる4機能を1日で実装（コミット `549ef72`、ブランチ `feature/phase-f-dashboard`）。

- ✅ **razer-dashboard に「🤖 スタックちゃん」操作カード**: 音量・ミュート・聞き取り開始・近接閾値・heartbeat 仕草トグル・テスト発話・表情切替
- ✅ **顔ステータス表示**: 「きいてるよ」→「考え中」→（検索時「調べ中」）を avatar の上に小さなラベルで表示。聞こえた/聞こえてないが一目でわかる
- ✅ **定期的な顔振りの正体特定と停止**: heartbeat の仕草フォールバックだった。デフォルト OFF + ダッシュボードから実行時トグル（通知発話は維持）
- ✅ **ウェイクワード「スタックちゃん」**: MultiNet カスタムウェイクワード（ピンイン近似 `su ta ke qiang`）。ビルド済み、実機実験は flash 後
- 開発スタイル: Fable=指揮官、実装は Opus エージェント3並行（firmware / gateway / dashboard）。gateway テスト 660 件パス（+53）、ruff クリーン

## 2. 構成図

```
[スマホ/PC ブラウザ (Tailscale)]
    │ HTTP GET/POST /control/*
    ▼
[razer-dashboard status_api.py :8080]   ← STACKCHAN_TOKEN はここ（drop-in 環境変数）
    │ プロキシ（Bearer + Host 付与、127.0.0.1 宛て）
    ▼
[gateway http_server.py :8767 /control/*]  ← トークンガード必須（/mcp /status と同じ）
    │ in-process（同一プロセスに Gateway シングルトン）
    ├─ control.py … 音量state(~/.stackchan/control_state.json)・ミュート・listen発火
    ├─ HeartbeatRunner.set_gestures() … 仕草トグル（再起動不要）
    ▼ WebSocket :8765
[StackChan firmware]
    ├─ self.display.set_status_text（新規）… avatar 前面の半透明ラベル
    ├─ self.audio_speaker.set_volume / self.touch.set_proximity_config（既存）
    └─ カスタムウェイクワード（MultiNet, assets パーティション）→ StartListening
                                  → タップと同一の listen start echo → voice_turn
```

会話フェーズの顔ステータス（gateway 側 hermes_bridge.py が駆動）:

```
録音終了 → STT 開始     「きいてるよ」
STT 完了 → Hermes 呼出  「考え中」
Hermes が web_search    「調べ中」（voice_turn_active フラグでゲート）
TTS 完了 / エラー       消去（finally で必ず）
```

## 3. 重要な発見・判断

1. **フォント差し替えは不要だった**（計画変更）。実行時テキストフォントは既に assets の `font_puhui_common_20_4.bin`（日本語グリフ入り）で、CMakeLists の `font_puhui_basic_20_4` は app に焼く小さなフォールバックに過ぎない。計画どおり common に差し替えていたら app が +2MB でパーティション溢れ＋assets から common が外れて逆に日本語が壊れる二重の罠だった。**status label は画面のフォントを継承するだけで日本語が出る**
2. **sdkconfig 直編集は消える**: release.py が `idf.py set-target`（fullclean）で sdkconfig を再生成するため、ウェイクワード設定は board の `config.json` の `sdkconfig_append` に置く（servo/camera 設定と同じ流儀）
3. **app だけの flash ではウェイクワードが効かない**: この fork はパーティションテーブル v2/16m.csv に model パーティションがなく、MultiNet モデルは `generated_assets.bin`（assets @ 0x800000）に入る。**app(0x20000) + assets(0x800000) の両方を flash する**こと
4. **ウェイクワード→gateway は無改修**: 検知後の経路はタップと同一（listen start echo）。`state:"detect"` メッセージは gateway が無視するので無害
5. ダッシュボード→gateway はブラウザ直叩き不可（:8767 は 127.0.0.1 bind + Host/Origin 検証）→ status_api.py がプロキシし、トークンはサーバー側に保持

## 4. 実機反映手順（未実施、ユーザー承認待ち）

### 4.1 flash（app + assets、~3分。NVS 保持なので WiFi 設定は残る）

```bash
cd ~/dev/yorishiro-workspace/stackchan-mcp-yorishiro/firmware
~/.venvs/esptool/bin/esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash 0x20000 build/xiaozhi.bin 0x800000 build/generated_assets.bin
```

### 4.2 sudo 作業（ユーザー実施）

```bash
# (1) heartbeat 仕草をデフォルト OFF に（通知発話は維持）
sudo tee -a /etc/systemd/system/stackchan-gateway.service.d/heartbeat.conf <<'EOF'
Environment=STACKCHAN_HEARTBEAT_GESTURES=0
EOF

# (2) status-api に gateway トークンを注入（<トークン>は ~/.yorishiro/secrets.env 等の STACKCHAN_TOKEN 値）
sudo install -d /etc/systemd/system/status-api.service.d
sudo tee /etc/systemd/system/status-api.service.d/token.conf <<'EOF'
[Service]
Environment=STACKCHAN_TOKEN=<トークン>
EOF
sudo chmod 600 /etc/systemd/system/status-api.service.d/token.conf

# (3) 反映
sudo systemctl daemon-reload
sudo systemctl restart stackchan-gateway status-api
```

### 4.3 E2E チェックリスト

- [ ] `curl -s http://localhost:8080/control/status` → `"ok": true` + esp32_connected
- [ ] ダッシュボード（http://100.70.219.79:8080/dashboard）にスタックちゃんカードが出る
- [ ] 音量スライダー → 実機音量変化。ミュート → 無音 → 解除で直前音量
- [ ] gateway 再起動後もデバイス再接続時に音量が再適用される
- [ ] 聞き取りボタン → 顔に「きいてるよ」→ 発話 →「考え中」→（「〜を調べて」で「調べ中」）→ 応答後に消える
- [ ] タップ起動でも同じステータス遷移
- [ ] 近接トグル/閾値スライダー → 手かざしリフレックスに即反映
- [ ] 仕草トグル OFF で顔振り停止（通知発話は維持）、ON で次ティックから復活
- [ ] 「スタックちゃん」と呼びかけ → 聞き取り開始（シリアル or journalctl でウェイクワード検知ログ）。**夫婦の会話での誤検知も数時間観察**
- [ ] 日本語ステータスが□にならない

## 5. ウェイクワード調整ガイド（F5）

- 検知されない → `config.json` の `CONFIG_CUSTOM_WAKE_WORD_THRESHOLD` を 20→10 へ（小さいほど敏感）、またはピンイン候補を変更（`si ta ke qiang` / `su ta ku qian` 等）。**変更は再ビルド + app/assets flash が必要**
- 誤検知が多い → 閾値を 20→40 へ
- どうしてもダメなら代替: 専用学習済みモデル `wn9_histackchan_tts3`（「ハイ、スタックちゃん」、確実に動くがフレーズが長い）

## 6. 用語解説

| 用語 | 説明 |
|---|---|
| **MultiNet** | Espressif の音声コマンド認識モデル。中国語ピンイン列でコマンド語を定義できるため、「スタックちゃん」を近似発音で登録した。WakeNet（専用学習済みウェイクワード）と排他 |
| **ピンイン近似** | 日本語「スタックちゃん」を中国語音素 `su ta ke qiang` で表現する手法。MultiNet が中国語ベースのための回避策。精度は実験次第 |
| **assets パーティション** | フラッシュ 0x800000 からの 8MB SPIFFS 領域。フォント・アバター画像・SR モデル（srmodels.bin）が入る。app と独立して flash 可能 |
| **sdkconfig_append** | board の config.json に書く Kconfig 上書き。ビルド時に sdkconfig 再生成後に適用されるため、fullclean に耐える唯一の置き場 |
| **systemd drop-in** | `/etc/systemd/system/<service>.service.d/*.conf` でサービス定義を上書きする仕組み。本体ファイルを触らず環境変数を足せる |
| **Bearer トークン** | `Authorization: Bearer <token>` ヘッダによる認証。/control/* は /mcp と同じトークンで保護。ブラウザにトークンを渡さないため status_api.py がプロキシする設計 |

## 7. 変更ファイル

- firmware: `main/boards/stackchan/stackchan.cc`(+95)、`main/boards/stackchan/config.json`
- gateway: `control.py`(新規)、`http_server.py`、`hermes_bridge.py`、`stdio_server.py`、`heartbeat.py`、`gateway.py`、`esp32_client.py` + テスト7ファイル（660 件パス）
- dashboard（git 管理外）: `~/razer-dashboard/status_api.py`、`dashboard.html`（バックアップ: `*.bak-20260613`）
- ビルド成果物: `firmware/build/xiaozhi.bin`(3.43MB)、`firmware/build/generated_assets.bin`(3.8MB)、`firmware/releases/v2.2.6_stackchan.zip`
