# 在室状態マシン + heartbeat 在室ゲート（Phase D 序盤・gateway 側）

**日付**: 2026-06-16 着手 〜 2026-06-17
**ブランチ**: develop（コミット前）
**スコープ**: ステップ1（在室ゲートまで）。SwitchBot 自動制御・BLE 個人識別・室温連動は次ステップ以降（ユーザー合意）。

---

## なぜ作ったか（背景）

ケンジさんの最終ビジョン（朝の挨拶／室温連動エアコン／在室・睡眠・不在のモード自動切替／生活支援AI）の**共通基盤**として、まず「部屋に人がいるか」を判定する層が要る。これがあると：

- heartbeat（自発提案）が**人がいる時だけ**発火する（設計原則①「会話に割り込まない」の延長＝誰もいない部屋に話しかけない）
- 後段で「不在→消灯」「朝の挨拶」「睡眠モード」を載せられる

TMOS PIR 検証で在室ゲートは「強い GO」判定済み（`docs/worklog/2026-06-16-tmos-verify.md`：静止在室を 211.7 秒連続検知・無人誤発火 0）。その成果を実コードに落とした。

---

## 全体の流れ（構成図）

```
[TMOS PIR  STHS34PF80]  ← Port A / PaHUB2 mux ch3 / I2C 0x5A
        ▲ 10秒ごとに読む
        │ sensors.read_tmos(dispatch)  ← _i2c_lock で atomic
        │
[PresenceMonitor (presence.py)]   ← 新規
   状態 = [在室か](TMOS present) × [活動/就寝時間帯](時刻)
     UNKNOWN  未読/エラー/未接続       → 許可(fail-open)
     ABSENT   不在がデバウンス分継続    → 抑制
     ACTIVE   在室×活動時間帯          → 許可
     QUIET    在室×就寝時間帯          → 許可(就寝抑制は heartbeat 側)
        │ allows_heartbeat()
        ▼
[HeartbeatRunner._skip_reason()]  ← 1条件追加
   既存ゲート(未接続/会話中/音声中/quiet) に「room empty」を追加
        │
        ▼  発火 or スキップ
[自発ジェスチャー / 通知発話]

[dashboard] ──GET /control/presence──▶ 状態表示
            ──POST /control/presence/config──▶ 閾値(就寝時間帯/不在デバウンス)変更＋永続化
```

---

## 触ったファイルと役割

| ファイル | 変更 | 中身 |
|---|---|---|
| `gateway/stackchan_mcp/presence.py` | 🆕 新規 | 状態マシン本体。ポーリング・状態遷移・永続化・ゲート判定 |
| `gateway/stackchan_mcp/sensors.py` | ✏️ | `_i2c_lock` 追加。単体 read/init を atomic 化（mux 競合＝読み裂け防止） |
| `gateway/stackchan_mcp/heartbeat.py` | ✏️ | `_skip_reason()` に在室ゲート1条件（"room empty"） |
| `gateway/stackchan_mcp/gateway.py` | ✏️ | `PresenceMonitor` の生成・start/stop 紐付け |
| `gateway/stackchan_mcp/http_server.py` | ✏️ | `GET /control/presence`・`POST /control/presence/config`・status 同梱 |
| `gateway/tests/test_presence.py` | 🆕 | 状態遷移/デバウンス/fail-open/config/ゲート（25件） |
| `gateway/tests/test_http_server.py` | ✏️ | presence エンドポイント（9件） |

---

## 設計判断（なぜそうしたか）

### 1. 状態は「在室 × 時間帯」の素直な2軸（過剰設計を避けた）
TMOS は熱（presence）の有無しか分からず、**静止した在室と就寝を区別できない**。なので「睡眠」を厳密にセンサーで検知せず、`在室か(TMOS)` × `活動/就寝時間帯(時刻)` で4状態を導出。睡眠の精緻化は将来 ToF/活動量で。

### 2. fail-open（最重要）
ゲートは「**確信を持って不在(ABSENT)の時だけ**抑制」する。起動直後・センサー故障・デバイス未接続（=UNKNOWN）は**許可**。理由：在室が分からない時に黙らせると、センサーが死んだら StackChan が永久に沈黙する。「抑制には確証を要求、許可には要求しない」。

- 不在判定にはデバウンス（既定120秒）＝一瞬 FoV を外れただけで開閉しない
- 連続読みエラー3回で UNKNOWN にフォールバック（それまでは最後の状態を保持）
- `motion` フラグは無人でも誤発火するため使わず、`presence` を在室信号にする（TMOS 検証の結論を踏襲）

### 3. opt-in
`STACKCHAN_PRESENCE_POLL_SEC` が未設定なら `from_env` が None を返し、monitor 自体が無く、ゲートは no-op（既存挙動）。heartbeat と同じ流儀。TMOS が刺さっていない環境（mac 機等）で誤動作しない。

### 4. mux 競合 = sensors 側のロックで解決
gateway の control 系（`/control/sensors`・presence poll）は single-flight デバイスキューを**バイパス**して `_dispatch_mcp_tool` を直接呼ぶ。なので presence poll と dashboard センサータブが同時に mux を触ると、ch 選択が割り込んで「ch2 のデータを ch3 として読む」読み裂けが起きうる。`sensors.py` に `_i2c_lock` を1本置き、単体 read/init（mux 選択＋レジスタ読みのまとまり）を atomic 化。両者が同ロックを共有して直列化される。dispatch 経路の直列性に依存しない明示的な対処。

### 5. 閾値は実行時調整＋永続化（値を固定しない）
就寝時間帯・不在デバウンスは `~/.stackchan/presence_state.json`（control.py と同じ atomic write）に保存し、`POST /control/presence/config` で変更。`set_neutral_pose` 等と同じ「置き場所・生活リズム依存の値はダッシュボードで調整」という本プロジェクトの流儀。

---

## 用語

- **TMOS PIR (STHS34PF80)**: 熱（赤外線）で人の在/不在を検知する ST 製センサー。焦電 PIR と違い静止した人も検知できる。FoV 80°・最大4m。壁/扉は透過しない。
- **PaHUB2 / PCA9548A**: I2C マルチプレクサ。1本のバスに同アドレスの複数センサーをぶら下げ、ch を選択して切り替える。
- **fail-open / fail-closed**: 異常時に「通す（open）」か「閉じる（closed）」か。ここでは安全側＝通す（黙りっぱなしより誤発火寄り、ただし誤発火は他ゲートで二重に防がれる）。
- **デバウンス**: 信号のチャタリング（一瞬の途切れ）を無視するための猶予時間。
- **heartbeat**: 会話の合間に StackChan が自発的に動く/喋る仕組み（Phase D/E）。

---

## 検証

- **pytest 897 passed**（前回 864 + 今回 33）/ **ruff clean**。既存回帰なし。
- 実機 E2E は未（ケンジさん）。前提：`STACKCHAN_PRESENCE_POLL_SEC=10` を env に設定し gateway 再起動。確認項目＝在室→自発提案が出る／不在(デバウンス後)→沈黙／就寝時間帯→沈黙。

## 追記: get_presence MCP ツール（reactive 版・ケンジ案）

セッション後半、ケンジさんから設計提案：「heartbeat（自発の発話判断）は本来 Hermes（思考体）の責務。観測（gateway・機械的）と判断（Hermes・文脈的）を分けるべき」。強く同意し、方針を確定：

- **観測＝gateway（高頻度・機械的）/ 判断＝Hermes（状態遷移イベント駆動）**。高頻度ポーリングで Hermes に毎回問い合わせるとトークン/割り込みが爆発（1分間隔=1日1440回）するので、Hermes 問い合わせは「意味ある状態変化」の時だけ。
- 自発判断層の前に、**reactive 版を先行実装**（ケンジ案）：Hermes が Discord で「今どんな状態?」と聞かれたら在室状態を読んで答える。reactive なので原則①④に沿い（聞かれたら答える・自発でない）、数日運用の精度検証と自発判断層への橋渡しを兼ねる。

実装＝`get_presence` MCP ツール（新設ファイルなし）：
- `stdio_server.py`: `list_tools` に Tool 定義 + `_dispatch_mcp_tool` に処理（`gateway._presence.snapshot()` を返す。monitor 無しは `{"enabled": false}`）
- `http_server.py`: `BYPASS_TOOLS` に追加（デバイス非接触＝ESP32 キュー非経由）
- `tests/test_presence.py` + `test_http_server.py`: +3 → **pytest 899 / ruff clean**

調査（investigator）で確認した自発判断層の設計（次フェーズ・着手は数日運用後）：
- Hermes 呼び出しは `hermes_bridge.ask_hermes(text)` が `/v1/chat/completions` に HTTP POST するだけ → **原則④（Hermes 改造なし）を守れる**。
- 触る予定：`presence.py`（`on_state_change` コールバック）/ `heartbeat.py`（`trigger_proactive` で安全装置流用 + ask_hermes）/ `gateway.py`（`_on_presence_changed` で有意遷移フィルタ + 配線）。新設なし。

## 残タスク

- ✅ (7) dashboard UI（センサータブに在室判定カード）実装済
- (9) 実機 E2E ＝ **数日運用フェーズ**（env 設定済。dashboard + Discord で精度確認）。**get_presence 有効化に gateway 再起動が必要**
- (10) learning-report（フェーズ完了時）
- 次フェーズ：Hermes 自発判断層（数日運用で勘所を掴んでから着手）
