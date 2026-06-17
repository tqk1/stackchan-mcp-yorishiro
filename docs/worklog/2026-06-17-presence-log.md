# 2026-06-17 — TMOS 在室データの時系列ログ記録

## 背景（なぜ）

ケンジさんが本日 TMOS のリアルタイム値を観察し、**家にいてもセンサーが反応しない谷間が約120秒ある**ことに気づいた。在室判定（`presence.py`）は反応が一定時間途切れると不在（ABSENT）に落とすため、「家にいるのに不在判定される」リスクがある。

だが従来、**センサー値の履歴は一切保存していなかった**（`_last_snapshot` はメモリ上の最新1件のみ）。谷間が実際どれくらい続くかが分からないと、不在判定のしきい値（`absent_after_s`）を根拠を持って決められない。

→ **まず生データをログに残して実態を把握する。** 後日、谷間の最大長などを集計してしきい値を調整する。

## 実装（最小・既存パターン再利用）

`presence.py` の既存の在室監視ループ（`PresenceMonitor._poll_once()`、`poll_sec` ごとに TMOS を読む）に JSONL 追記を1行足しただけ。新規クラスは作っていない。

- **追記**: `_append_log()` — `event_log.log_event` と同じ atomic append（`open("a")`+`flush`、`OSError/PermissionError` を握りつぶし WARNING、fire-and-forget）。
- **ローテーション**: `start()` 冒頭で `event_log.rotate_old_entries(path=...)` を再利用（7日保持・`ts_unix` キー・起動時1回）。`cli.py` は無改変。
- **読み取り成功時のみ記録**: `SensorError` でスキップした tick は書かない。`present=false`（無人）の行は記録する（谷間の検証に必須）。
- **on/off**: 既定で `~/.stackchan/presence_log.jsonl` に有効。`STACKCHAN_PRESENCE_LOG` でパス変更、`off`/空で無効。在室監視自体が opt-in（`STACKCHAN_PRESENCE_POLL_SEC`）なので、監視が動いていれば自動でログも残る。

変更ファイル: `gateway/stackchan_mcp/presence.py`、`gateway/tests/test_presence.py`。`event_log.py` は再利用のみ（無変更）。

## 稼働実機で判明した事実（プラン前提の訂正）

再起動前の稼働 gateway を `GET /control/presence`（:8080 経由）で確認したところ:

- 在室監視は**既に有効**（`enabled=true`）。
- **`absent_after_s` は既に `450`秒（7.5分）** に調整済みだった（プランが前提にした「120秒」ではない）。`poll_sec` も `5`秒。過去にダッシュボードで延ばされていた。
  → 「家にいるのに不在」リスクは**既にある程度緩和されている**。ログ収集は「450秒で本当に足りるか（谷間の最大長は?）」の検証という位置づけになる。
- 観察時まさに谷間が出ていた: `present=false, presence=-19, last_seen_s_ago=310.4`。ケンジさんの気づきが実データで裏付けられた。

## 検証

- `pytest tests/test_presence.py` → **33 passed**（既存25＋新規8）。
- 全体 `pytest` → **971 passed**、`ruff` → **clean**。回帰なし。
- E2E（実機）: `sudo systemctl restart stackchan-gateway` 後、
  - サービス `active`、`enabled=true / poll_sec=5 / absent_after_s=450`
  - `~/.stackchan/presence_log.jsonl` が生成され、**約5秒ごとに1行追加**（再起動後40秒で8行）
  - 各行に TMOS 全フィールド＋`state`＋`last_seen_s_ago`＋`ts_unix` を確認。無人 baseline は `presence ≈ 4〜-15`、室温 `ambient_c = 28.0`。

## 記録フォーマット（1行 = 1 poll）

```json
{"ts_unix": 1781706586.31, "state": "unknown", "last_seen_s_ago": null,
 "present": false, "presence": -15, "motion": -15, "pres_flag": false,
 "mot_flag": false, "shk_flag": false, "object_raw": -7365, "ambient_c": 28.0}
```

- `state`: `active`(在室・覚醒) / `quiet`(在室・就寝帯) / `absent`(不在確定) / `unknown`(未検知・fail-open)
- `last_seen_s_ago`: 最後に在室検知してからの経過秒（`null`=まだ一度も検知なし）
- `present`: `pres_flag` または `presence > 200` で在室。`presence`/`motion` は符号付き生値（無人時は 0 付近〜負）。
- `ambient_c`: 室温（℃）。将来の朝の挨拶／エアコン連動にも活用可。

## データの流れ

```
TMOS PIR (PaHUB2 mux ch3, 0x5A)
   │  read_tmos()  ← 5秒ごと
   ▼
PresenceMonitor._poll_once()
   ├─ 状態導出 (_derive_state: active/quiet/absent/unknown)
   ├─ heartbeat 在室ゲート (allows_heartbeat)
   └─ _append_log() ─► ~/.stackchan/presence_log.jsonl  (JSONL追記, 7日保持)
                          │
                          ▼  翌朝オフライン集計
                       谷間の最大長 / ABSENT 発生時刻・回数 / 室温推移
```

## 次のステップ

1. このまま翌朝まで回してデータを蓄積。
2. **明日ケンジさんの依頼で集計**（集計スクリプトは今回作らない方針）: 在室の谷間の最大長、ABSENT への遷移時刻・回数、就寝帯と覚醒帯の差、室温推移。
3. 集計結果を基に `absent_after_s`（現 450秒）の妥当性を判断。時間帯別しきい値や motion 併用の是非も検討。
4. **コミット先のブランチは E2E green 後にケンジさんと相談**（現在 `feature/multiturn`、未コミットは presence の2ファイルのみ）。
5. しきい値調整がまとまった段階で、生活支援ビジョンの一部として learning-report 化。

## 用語

- **TMOS (STHS34PF80)**: STMicro の赤外線サーモパイル人感センサー。熱式なので静止していても在室を検知できる（PIR と違い動きに依存しない）。
- **デバウンス (`absent_after_s`)**: 在室検知が途切れてから不在と断定するまでの猶予。短すぎると一瞬の谷間で不在に flap する。
- **fail-open**: センサー故障・未検知（UNKNOWN）では heartbeat を止めない設計。沈黙したロボットを避けるため、不在断定は「確信があるときだけ」。
