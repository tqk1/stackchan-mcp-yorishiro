# 2026-06-16 センサータブ追加 — TMOS PIR + ジェスチャー リアルタイム可視化

## 概要

ダッシュボードに「📡 センサー」タブを新設し、PaHUB2(I2C mux) 経由の 2 センサー
— **TMOS PIR (STHS34PF80)** と **ジェスチャー (PAJ7620U2)** — の検知値をリアルタイム表示
する観察ツールを実装した。狙いは「ケンジさんが家中を歩いて、どこで・何をしている時に
どんな数値が出るかを目で見て、StackChan の挙動（在室ゲート/ジェスチャー反射）を設計する
土台」にすること。

両センサーとも実機 E2E で動作確認済み（gateway pytest 864 passed / ruff clean）。
実地観察フェーズ（ダッシュボードでのウォークスルー → スクショ → 挙動設計）と
learning-report は次フェーズ。

## 構成図

```
[ブラウザ]
  │ http://192.168.0.19:8080
  ▼
[status_api.py :8080]  (~/razer-dashboard, 非git)
  │  GET /control/sensors  /  POST /control/sensors/init  をトークン付きで素通し
  ▼
[gateway http_server.py :8767]
  │  control_sensors / control_sensors_init  →  sensors.read_all / init_all
  │  dispatch = _dispatch_mcp_tool(name, args, gateway)
  ▼  (WebSocket MCP, i2c_write / i2c_write_read)
[firmware Port A I2C bus, 400kHz]
  ▼
[PaHUB2 mux  (PCA9548A @0x70, DIP)]
  ├─ ch3 → TMOS PIR  (STHS34PF80 @0x5A)   presence/motion/温度
  └─ ch2 → ジェスチャー (PAJ7620U2 @0x73)  9種ジェスチャー
```

## 実装内容

### gateway（コミット対象）
- **`gateway/stackchan_mcp/sensors.py`（新規）**: 純ロジック層。`dispatch(name, args)` を注入
  する形（http_server を import しない＝循環回避）。`scratch/tmos_probe.py` のレジスタ定義・
  `_s16`・mux_select を移植。
  - TMOS: `read_tmos` = mux ch3 → FUNC_STATUS(0x25) → presence(0x3A)/motion(0x3C)/obj(0x26)/
    ambient(0x28) を各 write_read 2byte（1トランザクション＝読み裂け回避）。在室判定は
    `pres_flag or presence>200`（motion は無人誤発火で在室に使わない）。
  - ジェスチャー: `init_gesture` = 二度読みで起こし part_id(0x7620) 確認 → `GESTURE_INIT_REGISTERS`
    (RevEng_PAJ7620 の init array 55ペア) を順次 write。`read_gesture` = mux ch2 → bank0 →
    0x43/0x44 read（読むとクリア）→ ビット→種別。
  - `read_all` / `init_all` = 両センサーを順次、片方 NACK でも部分結果を返す。
- **`http_server.py`**: `GET /control/sensors`（読み取り）+ `POST /control/sensors/init`（初期化）。
  control_i2c と同作法（device 未接続 503、prefix トークンガード）。
- **テスト**: `test_sensors.py`（+13, fake dispatch で純ロジック）+ `test_http_server.py`（+5, HTTP層）。

### dashboard（非git・即反映、`~/razer-dashboard/`）
- `dashboard.html`: 「📡 センサー」タブ。ポーリングトグル（マスター）＋ TMOSカード（在室ランプ・
  presence/motionバー・PRES/MOT/SHKランプ・室温）＋ ジェスチャーカード（最終種別特大＋履歴）。
  独立タイマー ≈2.5Hz、**トグルON ∧ タブ表示中 ∧ 画面表示中** の AND でのみポーリング。
  **重複ポーリング禁止ガード（`sensorBusy`）= mux チャンネル競合回避**。
- `status_api.py`: `do_GET` の GET allowlist に `/control/sensors` 追加（POST は prefix 転送で無改修）。

## ジェスチャー bring-up の顛末（重要な教訓）

init は成功（part_id 0x7620）するのにジェスチャー検出がゼロ。`scratch/gesture_probe.py`
（ch2固定・muxスラッシュなしで高速読み）で切り分けた結果:
1. 設定は正しい — dump で `0x41=0xFF`(全ジェスチャー割込有効)/`0x42=0x01`(wave)/`0x72=0x01`(エンジン有効)
   等が書き込んだ通り。
2. **M5 純正ドライバ (`m5stack/M5Unit-GESTURE` の `register_for_initialize[]`) と配列が完全一致**
   ＝ソフトは正しかった（`0x4C` の 0x20/0x22 差は `USING_REGISTER_VALUE_15` フラグ違いのみ）。
3. 極接近(2-5cm)でも物体信号(`0x58/0x59`)がノイズ寸前(max 2)→ **レンズが光学的に手を捉えていない**。
4. ケンジさんがレンズの物理（向き/遮蔽/距離）を調整 → **up/down/left/right を検出**。
   本番 `/control/sensors`（mux切替経路）でも ~9.7Hz で検出OK・TMOS 取りこぼし0。

→ **教訓: PAJ7620 が無反応な時は init より先にレンズの物理を疑う**（極接近で物体信号≒0 は
遮蔽/向きの典型）。詳細は memory `project_future_sensors`。

## 認識距離（設計上の住み分け）

| 用途 | 距離 | センサー |
|---|---|---|
| 手かざしコマンド（払う/波） | **〜15cm** | ジェスチャー PAJ7620（今回） |
| 在室/動き（在室ゲート） | **〜2m+** | TMOS PIR（presence） |
| 接近で視線追従 | 1〜2m | ToF Unit VL53L0X（購入待ち） |

PAJ7620 は内蔵 IR LED の反射光方式で、反射は距離の二乗で減衰 → **5〜15cm 専用、1m は不可**。
「離れた距離で何かさせる」は TMOS / ToF に寄せる。

## 用語

- **PaHUB2 / PCA9548A**: 1本の I2C を 8ch に分岐する mux。`write [1<<ch]` でチャンネル選択。
  本fork では ch3=TMOS / ch2=ジェスチャー。各読みの周回でチャンネルを再選択して堅牢化。
- **STHS34PF80 (TMOS)**: 焦電と違い**静止在室も検知**する赤外線存在センサー。presence/motion を
  内蔵アルゴリズムが算出（閾値デフォルト200）。
- **PAJ7620U2**: 近距離ジェスチャーセンサー。バンク切替式レジスタ、結果は 0x43/0x44 に
  **ラッチ→読むとクリア**。I2C 活動が無いとスリープ（初回 NACK→二度読みで起きる）。
- **読み裂け**: 16bit 値の L/H を別トランザクションで読むと ODR 更新を跨いで混ざる。
  必ず 1 トランザクション 2byte 読み（auto-increment）。

## 検証

- gateway: `pytest 864 passed` / `ruff clean`。
- バックエンド E2E: `GET /control/sensors`=TMOS実値(presence 1943/温度27.86℃)、
  `POST /control/sensors/init`=who_am_i 0xD3 / part_id 0x7620。
- ジェスチャー: 物理調整後 up/down/left/right を本番経路で検出（~9.7Hz, TMOS 193/193）。

## 残（次フェーズ）

- 実地観察（ケンジさん）: センサータブで家中ウォークスルー → スクショ → 挙動設計。
- learning-report（観察結果も含めて作成）。
- 在室ゲート閾値・ジェスチャー反射の挙動設計。
- ②IR LED のカメラ確認結果・②何が物理的に効いたか（向き/フィルム/距離）の記録は追って。

## 非git の変更（既にデプロイ済み）

- `~/razer-dashboard/dashboard.html`（センサータブ UI）
- `~/razer-dashboard/status_api.py`（allowlist）
- `scratch/gesture_probe.py`（bring-up プローブ、使い捨て）
