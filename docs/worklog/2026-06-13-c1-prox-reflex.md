# 2026-06-13 作業ログ — C1 手かざしリフレックス復活 + Phase E 仕上げ

## 1. 今日やったこと（サマリ）

- ✅ **Phase E 仕上げ**: heartbeat の本番 conf 適用（30分間隔・quiet 22:00-06:30）。天気発話の聴感確認・抑制ログ確認もクリア。残りはメモリマインドの聴感のみ（今夜 18:00-21:00）
- ✅ **LTR-553 再計測で前回の結論が覆った**: 「前面シェルが光路を塞ぎ物理的に不可」(6/11) → 実際は手かざしに明確に反応（10cm で最大 ps_raw 1277）
- ✅ **C1 手かざしリフレックスを有効化**: 閾値を NVS 永続のランタイム設定にし、MCP ツールで再 flash なしに変更できる形で実装 → ビルド → flash → 実機 E2E 完了
- ⚠️ **flash 直後の WiFi 接続不能インシデント**: 原因はルーターの 2.4GHz 帯停止（firmware 無実）。切り分け手順が学びになったので §5 に記録

## 2. システム図（今日の変更点）

```
[Hermes / Claude Code 等 MCP クライアント]
    │ MCP (:8767, Bearer)
    ▼
[gateway stdio_server.py]
    │ tool_map に追加: set_proximity_config ──┐
    │ WS (:8765)                              │
    ▼                                         ▼
[StackChan firmware (stackchan.cc)]   self.touch.set_proximity_config
    │                                  (enabled, threshold を NVS 保存
    ├─ ProximityPollTick (100ms)        + 即時反映)
    │    ps >= prox_ps_threshold_ ×3連続 → NEAR
    │    rising edge + cooldown 5s → HandleProximity()
    │         首を正面・上向き60° + happy 表情
    └─ 起動時: Settings("stackchan_prox") から
         enabled (default true) / threshold (default 600) を読み込み
```

## 3. 実測値（LTR-553 距離特性、稼働中 gateway 経由サンプリング）

| 状態 | ps_raw |
|---|---|
| ベースライン（手なし） | 368〜388（揺らぎ±10） |
| 手 1〜2cm | ~820〜1090 |
| 手 10cm | ~1035〜1277（**最強**） |
| 手 20〜30cm | 393〜448（実用不可） |

- **10cm が至近より強い理由**: IR LED と受光フォトダイオードは数mm 離れて並んでいる。至近距離では反射スポットが受光部の視野から外れ、少し離れた方が幾何的に反射光が乗る。近接センサー一般の特性
- **閾値 600 の根拠**: ベースライン上限 448 から +150、最弱の手信号 ~820 から −220 のマージン
- E2E 発火実績: ps_raw 608 / 2012 / 848 の 3 回。クールダウン抑制・FAR 復帰も正常

## 4. 実装のポイント

1. **constexpr → ランタイム設定**: `PROX_REFLEX_ENABLED` / `PROX_PS_THRESHOLD` をインスタンスメンバ化し、起動時に `Settings("stackchan_prox")`（NVS）から読む。デフォルトは enabled=true / threshold=600
2. **MCP ツール `self.touch.set_proximity_config`**: enabled と threshold を**両方必須**にした。片方 optional にするとスキーマのデフォルト値で他方が黙って上書きされる事故が起きるため
3. **スレッド境界**: 設定値は MCP タスクから書き、100ms ポーリング（ESP_TIMER_TASK）から読む。`last_ps_raw_` と同じ「int 書き込みはアトミック、torn-read は無害」のトレードオフを踏襲
4. **計測ヘルパー**: `scratch/mcp_call.py`（稼働中 gateway :8767 に単発 tools/call）と `scratch/ltr553_sample.py`（連続サンプリング）。従来の mcp_repl.py は gateway を子プロセスとして二重起動するため、稼働中の systemd サービスとは併用不可

## 5. WiFi インシデントの切り分け手順（再利用価値あり）

症状: flash 後 65 秒 `No AP found` → 設定モード（白い設定アイコン + AP `Xiaozhi-E79D`）へ。

| 手順 | 確認したこと | 結果 |
|---|---|---|
| シリアルでブートログ | panic していないか | 正常起動、WiFi スキャンのみ失敗 |
| `esptool read-flash 0x8000`（パーティションテーブル） | NVS / OTA の配置 | nvs@0x9000、ota_0@0x20000（書込先と一致） |
| NVS ダンプ | wifi namespace と ssid キーの残存 | **残存 → 認証情報は無事** |
| otadata ダンプ | どのスロットから起動するか | seq=1 → ota_0（新 firmware が起動中） |
| サーバーの `wpa_cli scan_results` | 周辺の電波 | **自宅ルーターの 2.4GHz (-g) が消滅、5GHz (-a) のみ** |

→ ルーター再起動で復旧。**CoreS3 は 2.4GHz 専用**なので、「flash 直後に繋がらない」はコードを疑う前に環境を疑う。「デバイス側無実」を esptool ダンプで先に確定させると迷走しない。

## 6. 用語解説

| 用語 | 意味 |
|---|---|
| **NVS (Non-Volatile Storage)** | ESP32 のフラッシュ上の key-value ストア。WiFi 設定や今回の閾値など、再起動・再 flash（app領域のみ）を跨いで残したい値を置く |
| **OTA パーティション (ota_0/ota_1)** | firmware を2面持ちして無線更新時に切り替える仕組み。otadata がどちらから起動するかを指す。今回はシリアル flash だが書込先は ota_0 |
| **クロストーク（近接センサー）** | 手が無くても前面パネル内面の反射で出る ps_raw の床値（~380）。検知はこの床値との差分で行う |
| **rising edge トリガー** | NEAR 状態への遷移瞬間だけ発火させる方式。手をかざし続けても連続発火しない |
| **Streamable HTTP MCP** | MCP の HTTP トランスポート。initialize でセッション ID を取得し、以後のリクエストに `mcp-session-id` ヘッダーを付ける |

## 7. 残タスク

- 今夜: メモリマインド聴感確認（`rm ~/.stackchan/heartbeat_state.json` + 当日メモ書き足し → 18:00-21:00）
- ToF Unit (VL53L0X) 到着後: 部屋スケール視線追従（C1 本来の目標）
- 将来: 天気テンプレ文言の改善（降水確率10%で「雨が降りそう」と言う問題は閾値50%の本番 conf では起きにくいが、文言を実値に応じて変える余地あり）
