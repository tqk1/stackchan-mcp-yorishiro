# worklog 2026-06-16 — TMOS PIR (STHS34PF80) 活用検証（bring-up）

## このセッションでやったこと（概要）

届いた **M5Stack Unit TMOS PIR (U185 / STHS34PF80)** を、firmware 改造ゼロ（memory「道A」方針）で
実機評価する土台を作り、疎通と基本反応まで確認した。本番の静止在室／距離／FOV 実演は
ケンジさんの都合で後日（「あとで試す」）。

成果:
1. gateway に再利用可能なデバッグ経路 `POST /control/i2c` を追加（道A enabler）。
2. 検証用ポーリングスクリプト `scratch/tmos_probe.py` を作成。
3. 実機で **疎通（0x5A / WHO_AM_I=0xD3）** と **人体への強反応** を確認。
4. 致命的な落とし穴（16bit 読み裂け）を発見・修正。

## データの流れ（構成図）

```
scratch/tmos_probe.py  (urllib, トークン不要)
   │  POST /control/i2c  {"op":"write_read","addr":0x5A,...}
   ▼
razer-dashboard/status_api.py  :8080   ← 無認証ローカルproxy。/control/* を汎用転送＋Bearer注入
   │  POST /control/i2c  (+ Authorization: Bearer $STACKCHAN_TOKEN)
   ▼
stackchan-gateway (:8767, systemd) http_server.py
   │  control_i2c → _dispatch_mcp_tool("i2c_write_read", …) → gateway.esp32.call_tool("self.i2c.write_read", …)
   ▼  WebSocket MCP
StackChan firmware → Grove **Port.A** I2C バス → STHS34PF80 (0x5A)
```

- 既存の `i2c_scan/read/write/write_read` は **MCP(stdio)層** にしか無く、HTTP には汎用 I2C 口が無かった。
  そこで `/control/i2c` を1本足し、ダッシュボード proxy が `POST /control/*` を素通しするので
  `:8080/control/i2c` を叩けば `.env`・トークンに触れず検証できる。
- gateway は **editable install**（`.venv` の `_editable_impl_stackchan_mcp.pth`）なので、
  ソース編集 → `sudo systemctl restart stackchan-gateway`（system サービス＝sudo 必須）で反映。

## 実装（gateway）

`gateway/stackchan_mcp/http_server.py`:
- ヘルパー `_control_i2c_result`（デバイス payload を verbatim で surface）/ `_i2c_byte_list` / `_i2c_n_bytes`。
- ハンドラ `control_i2c`（`op` で scan/read/write/write_read を呼び分け、addr 0x08..0x77・bytes 0..255・n 1..256 を検証）。
- ルート `Route("/control/i2c", …, methods=["POST"])`（`/control` プレフィックスで自動的にトークン保護下）。
- テスト `tests/test_http_server.py` に +24（疎通idiom・4 op・bytes surface・デバイスエラー502・各種バリデーション・未接続503）。
- **gateway 全体 848 passed / ruff clean（回帰なし）**。※ まだ git コミットはしていない（editable で実機反映済み）。

## 実機で分かったこと

- **疎通**: `scan` → `0x5A` のみ検出 ✓ / `WHO_AM_I(0x0F)=0xD3` ✓。
- **無人ベースライン（15分連続）**: presence/motion は ±30 のノイズ帯、PRES/MOT フラグは **15分間 0 発火**（誤検出ゼロ＝在室ゲートに好適）、ambient 23.4°C 安定。
- **人体への反応（至近）**: `presence=10052 / motion=6001 / obj_raw=13482 / PRES●MOT●`。デフォルト presence 閾値 200 を桁違いに超える。証拠ログ `scratch/tmos_first_response_2026-06-16.log`。
- レイテンシ: 暖機後 ~17ms/read、フルイテレーション ~0.15s。実効ポーリングは ~0.7Hz（1サンプル5リード）。

## ★ 用語・落とし穴（学習ポイント）

- **TMOS（Thermal MOS）**: STMicro の赤外線サーモパイル。Planck の黒体放射則で物体の IR を測る。
  焦電 PIR が「動き」しか見ないのに対し、**静止した人の在室（presence）も出力**できるのが本質的な差。
  → だから heartbeat 在室ゲート（会話に割り込まない発話タイミング制御）の有力候補。
- **読み裂け（torn read）＝今回の最重要教訓**: 16bit レジスタ（presence/motion 等）の L バイトと H バイトを
  **別々の I2C トランザクションで読む**と、その合間に ODR（4Hz＝250ms毎）で新サンプルが書き込まれ、
  古い L と新しい H（またはその逆）が混ざって `+247→-248` のような偽スパイクになる。
  - 初版 `read_s16` がこれをやっていて、presence が常に **±256 内に張り付く**症状を出した
    （H バイトが 0x00/0xFF の境界しか跨がない見え方）。一方 `obj_raw` は >255 を返せていた＝read 自体は健全、と切り分け。
  - **対策**: L+H を **1トランザクションの2バイト読み（auto-increment）** にする。STHS34PF80 は
    連番レジスタを自動インクリメントで返すことを実機確認（CTRL3=0x00 でも 2/4 バイト一括が一致）。
  - 一般則: **マルチバイトのセンサーレジスタは必ず1トランザクションで読む**（BDU はあくまで同一読み取り内の保証）。

## 中断点と再開手順（次回）

状態: gateway は `/control/i2c` 込みで稼働中・device 接続維持。本番実演のみ保留。

再開:
1. ポーリング起動（バックグラウンド）:
   `PYTHONUNBUFFERED=1 .venv/bin/python -u scratch/tmos_probe.py poll --interval 0.4 > scratch/tmos_scenario.log 2>&1`
2. **先にセンサーのレンズ面が人を向いているか確認**（机/天井向きだと検知不能）。
3. 実演: ①無人15s→②正面50cmで**静止30s**（★静止在室を保持できるか）→③手振り8s→④離席15s→⑤距離1m/2m→⑥横ずれFOV。
4. ログ解析 → 在室ゲート Go/No-Go＋推奨閾値・ポーリング周期。PaHUB2／道B(firmwareドライバ)の要否所感。
5. 仕上げ: learning-report（docs/）＋ memory `project_future_sensors` 更新。`/control/i2c` を commit するか判断。

未解決の論点（→ 同日午後に解決。下記「本番実演」参照）:
- **静止在室の保持**: 初回の反応は減衰挙動を見せた（presence が時定数で 0 へドリフト）。
  デフォルト LPF/閾値のままで「静止した人」を保持できるか、保持できないなら AN5867 の
  presence LPF/閾値チューニングが要るか、を静止30sの実演で見極める（これが在室ゲート採否の核心）。

---

## 追記: ハブ構成への変更と本番実演（2026-06-16・同日午後）

### 構成変更（ケンジさんが配線変更）
「正しい検証のため」TMOS 直挿しから **PaHUB2 系 I2C マルチプレクサ（PCA9548A, M5Stack Port A I2C 拡張ハブ v2.1, DIP）経由**に変更し、ジェスチャーユニット(PAJ7620U2)も同ハブに接続。
- scan すると **0x70 のみ**（mux 自身）→ 下流デバイスはチャネル裏に隔離。
- 全8ch をチャネル選択（mux に `1<<ch` を1バイト write）して scan → **ch3=TMOS(0x5A) / ch2=ジェスチャー(PAJ7620U2 0x73)**。
  - ジェスチャーは初回スキャンで未検出だったが、**PAJ7620U2 はスリープ中だと最初の I2C アクセスに ACK を返さない**だけ（I2C 活動で起きる）。各 ch を二度読み（起こす→読む）＋ part id 直叩き（bank0 select `[0xEF,0x00]`→read 0x00 n2）で **ch2 に 0x73・part_id=0x7620 を確認**。両ユニットとも生存・到達OK。ジェスチャー9種の動作検証自体は別タスク。
- `scratch/tmos_probe.py` に `--mux/--ch` を追加（PCA9548A は「チャネル bitmask を mux アドレスに1バイト書く」だけ。ポーリングは**各周回でチャネル再選択**して堅牢化）。
- **教訓**: M5Stack「I2C 拡張ハブ」は製品名が似ていても **mux(PCA9548A)** と **パッシブ分岐** の2系統がある。判別は「scan して 0x70 系のみか/下流アドレスが直接見えるか」が一発。今回は mux。異アドレスのみ（0x5A/0x73）ならパッシブで十分だが、届いたのは mux だった。

### 本番実演の結果（8分/961サンプル、ハブ ch3、ODR4Hz、閾値=デフォルト200）
ケンジさんがレンズを自分へ向け、無人→接近→正面50cm静止→手振り→離脱を実演。

| 区間 | presence | PRES フラグ | motion |
|---|---|---|---|
| 無人ベースライン（473サンプル≒5分） | 平均 -1（±130 ノイズ） | **誤発火 0/473** | 時々発火 46/473 |
| 在室（PRES ON 451サンプル） | 平均 767（134〜1187） | **100% 点灯** | peak 958（手振り） |

- **★静止在室の保持＝合格**: 在室1ブロックで **211.7 秒連続 PRES 100% 点灯**（平均774）。静止しても減衰せず保持。初回の「減衰」は通過物の過渡で steady-state ではなかった。
- **分離**: 無人 平均-1 vs 在室 平均767 = **50倍超のクリーン分離**。閾値200がちょうど中間。
- **離脱レイテンシ**: フレームアウトで presence が即（~1サンプル/<1-2s）<200 へ復帰（84→49→31→…→8）。ゲート解除が速い。

### 結論: heartbeat 在室ゲート採用 = **強い GO**
- 在室信号は **PRES フラグ（FUNC_STATUS bit2）or presence>200**。**motion フラグは無人でも誤発火するので使わない**。
- ポーリングは 1-2Hz / 数秒間隔で十分（presence は安定保持）。堅牢化に「2サンプル連続」or 軽いヒステリシス。
- **設置時に実環境でノイズ床を再確認**（今回は発熱ノートPC上で±130と広め。StackChan 設置位置で要再計測）。
- **レンズ向き必須**: 人/部屋の方へ向ける（天井/机向きだと検知不可。無人15分の無反応はこれが一因だった）。
- 残（任意）: 距離1m/2m・横ずれFOVの定量化、ジェスチャー(ch2/PAJ7620)の9種動作検証、PaHUB2 道B(firmware)化の要否。

証拠ログ: `scratch/tmos_session_2026-06-16.log`（本番）／`scratch/tmos_first_response_2026-06-16.log`（初回・直挿し）。
