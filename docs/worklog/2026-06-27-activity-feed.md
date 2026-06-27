# 自動実行フィードタブ + センサータブ追従修正（2026-06-27）

ブランチ: `feature/activity-feed`（`feature/static-presence` @ `4cefb84` の上にスタック）

## やったこと（概要）

ダッシュボード（`https://razer-server.tailc0a7ab.ts.net`）に「📜 フィード」タブを追加し、Razer Server 上で**自動で動いているもの**の実行記録を時系列で振り返れるようにした。あわせて、直近のセンサー改修（静止在室検知・就寝ラッチ v2）に**未追従だったセンサータブ**を追いつかせた。

> **追記（実機確認後の方針変更）**: 当初 cron（毎時 `fetch_usage.py` の CC使用率）もフィードに載せたが、サーバータブの CC利用率と重複し、毎時エントリがフィードを占有して肝心の自律アクションが埋もれるため、**ケンジ判断で cron をフィードから完全除外**（バックエンドの cron マージ・UI フィルタ・cron 用テストを削除）。フィードは「スタックちゃんが自律的にやったこと」＋日次レポートリンクに純化。

### 1. フィードのデータソースを新設（バックエンド）

調査で判明した核心: heartbeat 発話 / proactive 挨拶 / 家電操作は **journald の `logger.info` だけ**で構造化ログが無く、フィードが作れなかった。そこで JSONL のアクティビティログを新設し、自律アクションの発火点に計装した。

- **`gateway/stackchan_mcp/activity_log.py`（新規）** — `event_log.py` と同じ流儀の fire-and-forget JSONL アペンダ。
  - `append(source, kind, *, subtype, status, text, detail, duration_ms)` / `read_recent(limit, source)` / `rotate_old_entries()`
  - パス: `~/.stackchan/activity_log.jsonl`（env `STACKCHAN_ACTIVITY_LOG`、`off` で無効化）
  - 保持: 14日（env `STACKCHAN_ACTIVITY_RETENTION_DAYS`）、起動時に1回ローテーション
  - 書き込み失敗は WARNING で握りつぶす（本線を絶対に止めない）
- **計装した発火点**:
  - `heartbeat.py` `_tick_speak`（天気/メモ発話・subtype + duration）/ `_perform_gesture`（idle ジェスチャー）
  - `proactive.py` `_perform_speak`（自発挨拶・subtype=遷移キー）
  - `stdio_server.py` switchbot ディスパッチ（`switchbot_send_command` のみ・成功/失敗・statusCode 反映。Eufy も将来同様にフック予定とコメント）
  - `gateway.py` 在室遷移リスナー `_record_presence_transition`（`register_on_state_change` に proactive と並べて登録。**list ベースなので presence 側の変更は不要だった**）

### 2. マージ用エンドポイント

- **`GET /control/activity?limit=N&source=…`**（`http_server.py`）
  - `activity_log.read_recent()` ＋ **cron**（`fetch_usage.log` を tail-parse）＋ **日次レポート**（`presence_reports/*.json` を列挙）を `ts_unix` 降順マージして返す
  - 重い I/O は `asyncio.to_thread`（`control_presence_report` と同じ）
  - `_read_cron_usage` / `_list_presence_reports` / `_gather_activity` / `_parse_limit` は純関数で単体テスト
- **status_api.py（非git）** GET allowlist に `/control/activity` 追加（POST は元々ワイルドカード転送）

### 3. ダッシュボード（非git `~/razer-dashboard/dashboard.html`）

- 5つ目のタブ「📜 フィード」: 件数セレクト + ソースフィルタ（すべて/🤖自律/💡家電/🏠在室/⏱️cron）+ 🔄再取得。オンデマンド取得（連続ポーリングなし）。フィルタはクライアント側。
- アイコン: 🗣️発話 / 👀gesture / 👋自発挨拶 / 🏠在室遷移 / 💡家電 / ⏱️cron / 📊日次レポート。日次レポート項目は「▶診断タブで見る」へ誘導。
- spoken text は `escapeHtml` でエスケープ。

### 4. センサータブ追従修正（同 dashboard.html）

- 「🏠 在室判定」に **就寝ラッチ（asleep）** と **静止在室（static_present / obj_armed / baseline・raw）** の行を追加し、`renderPresence` / `clearSensorUI` を更新。`/control/presence` は既に `asleep` と `occupancy{}` を返していたのに表示していなかった盲点を解消。
- 不在猶予スライダー上限 `600 → 1200`（presence v2 は 1080 を使う実績があり、推奨値が 600 超でも適用可能に）。
- 診断タブは最新 report エンドポイントに追従済みで変更不要と確認。

## アーキテクチャ

```
[heartbeat / proactive / presence遷移 / switchbot]  ← activity_log.append()
                     ↓
        ~/.stackchan/activity_log.jsonl
                     ↓ read_recent()
[gateway] GET /control/activity ──┬─ + fetch_usage.log（cron）
                                  └─ + presence_reports/*.json（日次リンク）
                     ↓ ts_unix 降順マージ
[status-api :8080] GET allowlist → :8767 転送
                     ↓
[dashboard.html] 📜 フィードタブ（オンデマンド + 🔄）
```

## ハマり / 学び

- **テスト汚染**: 計装が**既存テスト**（test_proactive / test_switchbot 等）の実行時に発火し、env 未設定だと実 `~/.stackchan/activity_log.jsonl` に書き込んでしまった（本番旧コードは未生成なので 100% テスト由来と判定し削除）。→ `conftest.py` に **autouse フィクスチャ** `_isolate_activity_log` を追加し、全テストで `STACKCHAN_ACTIVITY_LOG` を tmp に向けて隔離。教訓: 既定パスに副作用書き込みを持つモジュールは、conftest で一括隔離する。
- **`register_on_state_change` は既に list 対応済み**（`self._on_change.append`）。計画では「単一→複数化が必要」と見ていたが不要で、リスナー追加だけで済んだ。
- **cron の ts はローカル naive ISO**（`[2026-06-27T16:00:02]`）。`datetime.fromisoformat().timestamp()` がローカル時刻として解釈するので JST のまま正しい epoch になる。

## 検証

- gateway: **pytest 1093 passed / ruff clean**（新規 `test_activity_log.py` 13 + `test_activity_feed.py` 9）。隔離後、実 `~/.stackchan` が汚染されないことを確認。
- 構文: status_api.py / dashboard.html（ID整合・5タブ・括弧バランス）チェック済み。
- デプロイ: `feature/static-presence` 由来 editable install。**要 `sudo systemctl restart stackchan-gateway status-api`**（sudo=ケンジ）。本番 env は presence(5s)/heartbeat-speak/proactive いずれも有効 → restart 後すぐ cron＋日次レポート＋在室遷移でフィードが埋まる。
- 実機 E2E: 後続（フィードタブ表示・cron/レポート/遷移の出現・センサータブの就寝ラッチ/静止在室ライブ表示）。

## 残

- 実機 E2E green 後に commit（feature ブランチ）→ learning-report（`docs/activity-feed-report.md`）。
- 注意: 作業ツリーには別作業の未コミット（`tasks/todo.md` の static-presence 分・未追跡 `2026-06-27-presence-calibration.md`）が同居。**自分のコミットには含めない**（明示パスのみ add）。
