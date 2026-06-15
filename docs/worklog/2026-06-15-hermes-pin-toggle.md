# 2026-06-15 応答ルーティング「Hermes 固定」トグル

ブランチ: `feature/review-cleanup`（ダッシュボード拡張系の継続）

## 背景・目的

音声ターンの応答は **自動ルーティング** されている:

- env `STACKCHAN_LOCAL_LLM_MODEL` が設定されていて、かつ発話が「短文（≦30文字・マーカー語なし）」なら → ローカル LFM2.5（~0.5s、速いが品質はHermesに劣る）
- それ以外・ローカル失敗時 → Hermes Agent

「いまは賢さ優先で全部 Hermes に任せたい」という運用を、**ダッシュボードのトグル一つ**で切り替えられるようにした。これまではランタイムで切り替える手段がなく、env の有無でしか制御できなかった。

## やったこと

トグル ON = **Hermes 固定**（ローカル高速パスを完全バイパス）／ OFF = 従来の自動ルーティング（デフォルト OFF）。設定は **永続化**（gateway 再起動後も維持）。

### データフロー

```
[ダッシュボード sc-route-hermes トグル]
    │ POST /control/routing { "force_hermes": true }
    ▼
[status_api.py :8080]  ← /control/* を素通しプロキシ（変更不要）
    │
    ▼
[gateway http_server.py :8767]
    control_routing → control.set_routing_force_hermes(True)
    │
    ▼
[~/.stackchan/control_state.json]  ← "force_hermes": true をアトミック保存
    ▲
    │ 毎ターン load_state() で読む
[hermes_bridge._run_voice_turn]
    force_hermes = control.routing_force_hermes()
    ├─ LED ヒント: force_hermes なら即 Hermes 色（紫）を点灯
    └─ generate_reply(transcript, force_hermes=True)
           └─ not force_hermes and is_enabled() and decide_route()==LOCAL
              が False になり、ローカルを飛ばして必ず ask_hermes()
```

GET `/control/status` は `routing: { force_hermes, local_enabled }` を返す。ダッシュボードは起動・ポーリング時にこれでトグル状態を同期し、`local_enabled === false`（env 未設定）のときは「ローカル高速応答は無効。現状すべて Hermes」と注記する。

### 変更ファイル

| ファイル | 変更 |
|---|---|
| `gateway/stackchan_mcp/control.py` | `DEFAULT_FORCE_HERMES`、`load_state`/`save_state` に `force_hermes` フィールド、`routing_force_hermes()`/`set_routing_force_hermes()` アクセサ |
| `gateway/stackchan_mcp/hermes_bridge.py` | `generate_reply(text, *, force_hermes=False)`、`_run_voice_turn` で1ターン1回 `routing_force_hermes()` を読み LED ヒント＋`generate_reply` に渡す |
| `gateway/stackchan_mcp/http_server.py` | `local_llm` import、`control_routing` ハンドラ＋ `/control/routing` route、`_build_control_status` に `routing` ブロック |
| `razer-dashboard/dashboard.html` | 「よく使う」グループに「🧠 応答モード」カード（`sc-route-hermes` トグル＋ `sc-route-hint`）・change リスナー・status 同期 |
| `gateway/tests/test_{control,hermes_bridge,http_server}.py` | 既存修正＋新規ケース |

### 設計判断（なぜこの形か）

- **`generate_reply` に引数で渡す**: `decide_route`/`ask_local` と同じ「副作用源（永続状態）は呼び出し側＝オーケストレーション層 `_run_voice_turn` が読んで注入」流儀。`local_llm` は `control` を知らないまま、分岐テストが env 非依存で書ける。
- **`control_state.json` に相乗り永続化**: 音量/LED と同じ「運用方針の選択」。再起動で勝手に自動ルーティングへ戻ると、気づかぬまま短文がローカルに流れてしまう（ユーザーの意図に反する）。
- **`status_api.py` 不変**: `/control/*` を素通しプロキシしているので、新エンドポイントも自動で通る。

## 検証

- `ruff check`（対象6ファイル）: All checks passed
- `pytest`（gateway 全体）: **824 passed**（既存 816 + 新規 8）。回帰なし
  - HTTP は `build_app` を実起動して httpx で叩く形（`test_control_routing_sets_force_hermes` が POST → 200 → GET status の `routing` 同梱まで確認）＝ E2E に近い裏取り
- 実機での発話 E2E（トグル ON → 短文発話 → ローカルでなく Hermes へ・LED 紫・"H" バッジ）は razer-server + 実機が必要なため、ユーザー環境で実施予定

## 用語

- **応答ルーティング**: 1発話ごとに「ローカル LLM か Hermes か」を振り分ける層（`local_llm.decide_route` ＋ `hermes_bridge.generate_reply`）
- **force_hermes**: 今回追加した永続フラグ。True で全ターンを Hermes に固定
- **local_enabled**: `local_llm.is_enabled()`＝ env `STACKCHAN_LOCAL_LLM_MODEL` が設定されているか。False ならトグルに関係なく常に Hermes

## 残課題

- 実機での発話 E2E 確認（ユーザー環境）
- **マルチターン会話**（発話後に自動で聞き取りへ）は別途要望があったが、録音の停止機構が不明確（firmware に VAD 自停止コードが無いと判明）なため**次セッションで徹底調査してから実装**。詳細は memory / プラン `local-llm-hermes-agent-...md` の「次セッションへの申し送り」参照
