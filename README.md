# stackchan-mcp

[StackChan](https://github.com/mongonta0716/stack-chan) を任意の LLM クライアントから操作するための MCP (Model Context Protocol) ブリッジ。

```
┌─────────────┐     stdio MCP      ┌──────────────┐    WebSocket MCP    ┌──────────────┐
│ MCP client  │ ─────────────────▶ │   gateway    │ ──────────────────▶ │ ESP32 (CoreS3│
│ (Claude等)  │ ◀───────────────── │  (Python)    │ ◀────────────────── │  +StackChan) │
└─────────────┘                    │              │                     └──────────────┘
                                   │  /capture    │ ◀── HTTP POST (JPEG) ──┘
                                   └──────────────┘
```

任意の MCP クライアント (Claude Code / Claude Desktop / 他) から、首振り・カメラ撮影・タッチセンサ・アバター表情切替などの StackChan 操作を呼び出せる。

## 構成

このリポジトリはモノレポ。

| ディレクトリ | 内容 |
|---|---|
| `firmware/` | [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) フォーク全体（git subtree）。StackChan 用カスタムボードは `firmware/main/boards/stackchan/` に配置 |
| `gateway/` | Python MCP ゲートウェイ。stdio MCP サーバー (LLM側) + WebSocket MCP クライアント (ESP32側) + HTTP capture サーバー |
| `docs/` | [`architecture.md`](docs/architecture.md): 全体構成図・ツール名マッピング・写真フロー・認証・Phase ロードマップ |

## ハードウェア

- **本体**: M5Stack CoreS3 (ESP32-S3, 16MB Flash, 8MB PSRAM)
- **首サーボ**: SCS0009 ×2 (yaw + pitch、シリアルバス、TX=GPIO6, RX=GPIO7)
- **カメラ**: GC0308 (DVP, 320×240 YUV422)
- **タッチ**: FT6336 / Si12T
- **ディスプレイ**: ILI9342 (SPI, 320×240)

## ツール一覧 (gateway 経由で MCP クライアントが呼べる)

| ツール | 説明 | 状態 |
|---|---|---|
| `get_status` | ゲートウェイ接続状態 | ✅ |
| `get_device_info` | ESP32 デバイス状態 (バッテリー/音量/WiFi 等) | ✅ |
| `take_photo(question?)` | カメラ撮影 → JPEG 保存 → パス返す | ✅ |
| `set_volume(volume)` | スピーカー音量 (0-100) | ✅ |
| `set_brightness(brightness)` | 画面明るさ (0-100) | ✅ |
| `move_head(yaw, pitch, speed?)` | 首を動かす (サーボ) | ✅ |
| `get_touch_state` | タッチセンサ状態 (press/release/stroke 等) | ✅ |
| `set_avatar(face)` | アバター表情切替 (neutral/happy/sad 等 6種) | ✅ |
| `set_blink(state)` | 瞬き ON/OFF | ✅ |
| `set_mouth(state)` | 口開閉 | ✅ |
| `check_vm_en` | サーボ電源 (VM EN HIGH) 状態確認 | ✅ |

詳細スキーマは `gateway/README.md` 参照。

## クイックスタート

### 1. ファームウェア書き込み (CoreS3)

```bash
cd firmware
docker run --rm -v $PWD:/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
# → releases/v2.2.6_stackchan.zip

# フラッシュ (CoreS3 を USB 接続後)
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x0 build/merged-binary.bin
```

WiFi 設定は ESP32 が起動後にスマホで設定 UI に接続して行う (xiaozhi-esp32 標準フロー)。

### 2. ゲートウェイ起動

```bash
cd gateway
cp .env.example .env       # STACKCHAN_TOKEN / VISION_HOST を設定
uv sync
uv run python -m stackchan_mcp
```

### 3. MCP クライアント登録 (Claude Code 例)

`~/.claude.json` に追加:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/stackchan-mcp/gateway",
        "python", "-m", "stackchan_mcp"
      ]
    }
  }
}
```

詳細は `gateway/README.md` 参照。

## アバター画像について

`firmware/main/boards/stackchan/avatar_images.cc` は **真っ黒 RGB565 のプレースホルダ** です。ビルドは通りますが、画面には何も表示されません。実際にアバターを表示するには、自分の PNG 画像 (160×120) から `avatar_images.cc` を再生成してください。

シンボル一覧 (`avatar_images.h` 参照):
- 表情系 (6): `avatar_idle`, `avatar_happy`, `avatar_thinking`, `avatar_sad`, `avatar_surprised`, `avatar_embarrassed`
- 目 (3): `avatar_eyes_open`, `avatar_eyes_half`, `avatar_eyes_closed`
- 口 (5): `avatar_mouth_closed`, `avatar_mouth_half`, `avatar_mouth_open`, `avatar_mouth_e`, `avatar_mouth_u`

PNG → RGB565 配列の変換スクリプトは LVGL 公式の [Online Image Converter](https://lvgl.io/tools/imageconverter) などが使えます。

## 既知の課題

- 大角度急逆転 (±60° → -60° 等) でサーボハングする場合あり (Motion::update_task の補間移植で改善予定)
- タッチセンサ (Si12T) のぽん判定取りこぼし (感度レジスタ調整余地)

## ライセンス

このリポジトリはデュアルライセンス構成です。

| 範囲 | ライセンス |
|---|---|
| 全体 (`gateway/`, トップレベル, `firmware/` の大部分) | **MIT License** (`LICENSE` 参照) |
| `firmware/main/boards/stackchan/` 内の **SCServo_lib 由来ファイル** (SCS.{cc,h}, SCSCL.{cc,h}, SCSerial.{cc,h}, INST.h, SCServo.h) | **GNU GPL-3.0** (`firmware/main/boards/stackchan/SCServo_lib_LICENSE.txt` 参照) |

これは Feetech の SCServo SDK が GPL-3.0 で配布されているための制約です。SCServo_lib を静的リンクする **firmware バイナリ全体は実質 GPL-3.0** として配布されることになります。

一方、`gateway/` は独立した Python プロセスで、ESP32 とはネットワーク経由 (WebSocket) でしか通信しないため、**MIT License** のまま利用・派生できます。

### upstream

`firmware/` は [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) のフォーク ([kisaragi-mochi/xiaozhi-esp32](https://github.com/kisaragi-mochi/xiaozhi-esp32)) を git subtree で取り込んでいます。SCServo_lib は公式 [stack-chan](https://github.com/mongonta0716/stack-chan) (タカヲさん) から移植したファームウェアコンポーネントです。

## 関連プロジェクト

- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — ベースとなる ESP32 LLM クライアントファームウェア
- [stack-chan](https://github.com/mongonta0716/stack-chan) — オリジナルの StackChan プロジェクト (タカヲさん)
- [Model Context Protocol](https://modelcontextprotocol.io) — MCP プロトコル仕様

## コントリビューション

Issue / PR 歓迎です。StackChan コミュニティで使える形を目指しています。
