# 作業記録: Phase E 通知型 heartbeat — 価値があるときだけ話す（2026-06-12）

> このファイルは「後から読んで、何をして・何が動いていて・どういう構成なのかを学べる」ことを目的とした記録です。
> 疑問が出たら、このファイルごと Claude や Gemini に貼り付けて「ここを詳しく」と聞ける粒度で書いています。

---

## 1. 今日のゴールと結果

**ゴール**: heartbeat 第2段階（発話あり）。ただし計画段階でコンセプトを相談し、**「ランダムな一言」から「通知型」に転換**した。

**事前のユーザー決定**:
- 無意味な一言発話や動きは不要。**沈黙がデフォルト、伝える価値がある情報があるときだけ一言**
- 情報源 v1 は ①メモリマインド（夕方）②天気の急変・注意報（朝、大阪府守口市）
- クワイエットアワーを **22:00-06:30** に変更（旧 22:00-08:00）
- 言い方は v1 テンプレート固定。LLM による言い回し生成は将来の拡張点
- 将来 SwitchBot 人感センサーPro / CO2センサーを購入予定 → 情報源を後から足せる設計に

**結果**:
- ✅ `weather.py` 新規 — 気象庁 bosai API（キー不要）から警報・注意報＋降水確率を取得、判定は pure 関数
- ✅ `heartbeat.py` 拡張 — SPEAK opt-in、抑制5層、checker 巡回、state 永続化
- ✅ 対話タイムスタンプ配線 — voice_turn 冒頭＋タッチイベント → `gateway.note_human_interaction()`
- ✅ テスト **607 件全パス**（うち Phase E 新規 42 件: heartbeat 31 + weather 11）、ruff クリーン
- ✅ 実 API 確認 — 当日の守口市 降水確率20% → 閾値50で沈黙 / 閾値0で「今日は雨が降りそうだよ、降水確率20%。傘を忘れずにね」
- 実機 E2E: 加速 drop-in 投入済み。聴感確認はユーザー不在のため夜に持ち越し（§5）

---

## 2. 何を作ったか（構成図）

```
            heartbeat tick（30分±25%、Phase D の乱択タイマーを「ポーリングクロック」に流用）
                 │
   ┌─────────────┴─────────────────────────────────────────────┐
   │ ガード（どれかに当たると黙る）                              │
   │  ①クワイエットアワー 22:00-06:30   ②TTS/listen 中(tts_lock)│
   │  ③録音スロット使用中(is_recording) ④直近対話20分以内       │
   │  ⑤1日の発話上限(デフォルト3回)                              │
   └─────────────┬─────────────────────────────────────────────┘
                 │ 通過
   ┌─────────────┴──────────────┐
   │ checker 巡回（優先順）       │     言うことが無ければ
   │  1. weather（朝 06:30-09:30）│──→  沈黙（仕草のみ or 何もしない）
   │  2. memo（夕 18:00-21:00）   │
   └─────────────┬──────────────┘
                 │ 発話文あり
                 ▼
   synthesize_and_send()（VOICEVOX→ESP32、Phase B の既存経路を再利用）
                 │
   ~/.stackchan/heartbeat_state.json に「言った事実」を永続化
   （weather_done/memo_done の日付、リマインド済みメモの (name, mtime)、日次カウンタ）
```

### weather checker（`gateway/stackchan_mcp/weather.py` 新規）

- 気象庁の **bosai API**（`jma.go.jp/bosai/...`、API キー不要・無料）を利用
  - `warning/data/warning/270000.json` → 大阪府の警報・注意報。守口市（class20 コード `2720900`）の分だけ抽出。**status「解除」は除外**（フィードに残り続けるため）
  - `forecast/data/forecast/270000.json` → 当日の降水確率（pops）。今日の時間帯の最大値を採用
- 発話条件: 警報・注意報が発表中（優先）、または降水確率 ≥ 閾値（デフォルト50%）。**平常時は None = 沈黙**（「今日は晴れ」とは言わない）
- 取得失敗時は例外を投げる設計 — heartbeat 側が「チェック済み（今日はもう黙る）」と「チェックできなかった（次の tick で再試行）」を区別できる
- エリアコードは `bosai/common/const/area.json` で実データ検証済み（守口市 = class20 `2720900`、大阪府 office = `270000`）

### memo checker（`heartbeat.py` 内、`notes.py` を再利用）

- 夕方ウィンドウ内の tick で `list_notes()` → **mtime が今日**のメモを抽出 → 先頭行を60字までクランプして「今日のメモに『…』ってあるよ」（最大2件列挙）
- 二重リマインド防止が2層: ①`memo_done`（その日1回だけ発話）②リマインド済み `(name, mtime)` を state に永続化（**再起動しても同じ内容を二度言わない**）
- `memo_done` は**実際に発話したときだけ**立てる — ウィンドウ前半に何も無くても、後半に書かれたメモは後の tick が拾える

### 抑制5層の意味（設計原則1「夫婦の会話に割り込まない」の実装）

| 層 | 守る状況 |
|---|---|
| クワイエットアワー | 夜間・早朝 |
| tts_lock | StackChan 自身が話している/聞いている最中 |
| is_recording() | ユーザーがタップして録音している最中 |
| 直近対話クールダウン | **会話の余韻**。重要: タイムスタンプは voice_turn の**処理冒頭**で記録する。STT→Hermes 応答待ちの間（最大120秒）は tts_lock も録音スロットも空くので、ここを塞がないと「話しかけた直後の思考中に通知が割り込む」事故が起きる |
| 日次上限 | バグや異常時の安全弁（うるさくなる事故の上限を切る） |

### デフォルト OFF の二重 opt-in

`STACKCHAN_HEARTBEAT_INTERVAL_MIN`（Phase D から）と `STACKCHAN_HEARTBEAT_SPEAK=1`（Phase E 新規）の**両方**を設定して初めて話す。SPEAK 未設定なら Phase D と完全に同一動作（回帰テストで保証）。

---

## 3. 用語解説

- **通知型 vs 自律型**: 通知型は「言うべきことの検出」を gateway の決定的ルールで行う（テスト可能・暴走しない）。自律型は LLM に判断を委ねる（柔軟だが空振り発話の制御がプロンプト頼み）。今回は通知型を選び、「言い方」だけ将来 LLM に委譲できる拡張点を残した
- **気象庁 bosai API**: 気象庁の防災情報 JSON フィード。認証・キー不要で警報・予報が取れる。公式ドキュメントは無い（非公式に広く使われている）ため、構造変化に備えて判定部を pure 関数に分離してテストを厚めにした
- **checker パターン**: 「ウィンドウ判定＋検出＋発話文生成」を1ユニットにした構造。将来の SwitchBot 人感センサー/CO2センサーも `_check_*` メソッド1つの追加で組み込める
- **state ファイル**: 「今日もう言ったか」をプロセス外（JSON ファイル）に持つ。systemd 再起動でリマインドが二重発火しないため
- **monotonic clock**: `time.monotonic()`。壁時計と違い NTP 補正や手動変更で巻き戻らないので、「N分前」の計測に使う

---

## 4. 変更ファイル

| ファイル | 内容 |
|---|---|
| `gateway/stackchan_mcp/weather.py` | 新規。JMA 取得＋判定 pure 関数 |
| `gateway/stackchan_mcp/heartbeat.py` | SPEAK 設定、抑制、checker、state。DEFAULT_QUIET 22:00-06:30 へ |
| `gateway/stackchan_mcp/gateway.py` | `note_human_interaction()` ＋ esp32 コールバック結線 |
| `gateway/stackchan_mcp/esp32_client.py` | `on_human_interaction` コールバック（タッチ検証通過後に発火） |
| `gateway/stackchan_mcp/hermes_bridge.py` | voice_turn 冒頭でタイムスタンプ記録 |
| `gateway/tests/test_heartbeat.py` | Phase E 31 件追記 |
| `gateway/tests/test_weather.py` | 新規 11 件 |
| `docs/deploy/stackchan-gateway.service.d/heartbeat.conf` | Phase E 環境変数を追記 |

---

## 5. 実機 E2E（夜の確認手順）

朝の加速 drop-in（`scratch/heartbeat-e2e.conf`）で天気・メモの発話はログ上の発火を確認予定。**聴感確認と抑制テストが残り**:

1. 本番設定へ差し替え:
   ```
   sudo install -m 644 ~/dev/yorishiro-workspace/scratch/heartbeat-prod.conf /etc/systemd/system/stackchan-gateway.service.d/heartbeat.conf
   sudo systemctl daemon-reload && sudo systemctl restart stackchan-gateway
   ```
2. 夜に発話を再現するには（朝の発火で当日フラグが立っているため）:
   ```
   rm ~/.stackchan/heartbeat_state.json   # 「今日言った」記録をリセット
   echo "- E2Eテスト" >> ~/.stackchan/notes/メモ.md   # 今日のメモを更新
   ```
   18:00-21:00 のウィンドウ内なら次の tick（最大30分）でメモリマインドが話す
3. 抑制テスト: 画面タップで一言話しかける → 直後20分は `journalctl -fu stackchan-gateway | grep heartbeat` に `speak suppressed (recent interaction)` が出ることを確認
4. 22:00 以降に tick が `quiet hours` でスキップされることを確認

---

最終更新: 2026-06-12 朝
