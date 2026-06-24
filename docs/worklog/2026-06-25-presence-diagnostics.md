# 在室判定エンジン 自己診断レポート（Phase 2）— worklog

日付: 2026-06-25
ブランチ: `feature/presence-diagnostics`（develop `12be904` から分岐）
計画: `~/.claude/plans/phase2-cozy-perlis.md`

## 目的

在室判定エンジン v2（`presence.py`）は実機稼働中で `~/.stackchan/presence_log.jsonl` に毎ポーリング全TMOSフィールドを追記している（約121k行/7.2日）。これまで健全性確認は手元の `scratch/analyze_presence.py` を手動実行していた。本フェーズはこの集計を gateway に常設化し、在室判定の健全性（誤ABSENT・在室中の谷間・しきい値の妥当性）をいつでも自己診断できるようにする。Phase 3（生活リズム学習・自動較正）の観測レイヤーでもある。

確定要件: ①出力＝エンドポイント+ダッシュボードタブ+日次自動保存の両方 ②しきい値は診断＋推奨提示まで（自動適用しない＝human-in-the-loop） ③learning-report 作成。

## 用語

- **谷間（valley）**: 在室（present）区間内で presence が連続して失われる時間。`absent_after_s`（不在判定までの猶予）が跨ぐべきギャップに対応する。
- **resolved / censored 谷間**: ★本フェーズの肝。ACTIVE 中に presence が `absent_after_s` だけ途切れると状態が ABSENT に遷移し ACTIVE 区間が終わる。よって **ACTIVE 谷間は構造的にしきい値で右側打ち切り（censored）される**。しきい値前に在室へ戻った谷間が resolved。
- **sleep latch**: 就寝帯に一度在室確認したら覚醒まで QUIET を保持する仕組み。就寝帯の長い谷間（静止した寝姿）を埋める。

## 構成（何を作ったか）

```
presence_log.jsonl (raw)
   │  _read_recent_records(days=) … ts_unix>=cutoff フィルタ（event_log 流儀）
   ▼
presence_report.build_report(records, now, current_absent_after_s) … 純粋関数・I/Oゼロ
   │   基本/間隔/state滞在/分離度/谷間(全体+ACTIVE限定)/推奨/時間帯別 を dict 化
   ├─ render_markdown(report) … 人間可読 MD
   ▼
   ├─ GET /control/presence/report?days=N  (http_server, asyncio.to_thread)
   ├─ 日次自動保存 _maybe_write_daily_report (poll 相乗り・冪等・atomic)
   └─ dashboard 「📊診断」タブ (status-api :8080 proxy 経由)
```

- 新規 `gateway/stackchan_mcp/presence_report.py`（集計の純粋関数＋MD整形）。`presence` を import しない（循環回避、clamp 定数はミラー）。
- `presence.py`: `_read_recent_records` / `build_report(days=)` / 日次保存（`_today`=JST・`_maybe_write_daily_report`・`_resolve_report_dir`・`_atomic_write_text`・env `STACKCHAN_PRESENCE_REPORT`）。
- `http_server.py`: `control_presence_report` ハンドラ＋`_parse_days`（1..28 クランプ）＋ルート。重い集計は `asyncio.to_thread`。
- `~/razer-dashboard/status_api.py`（git外）: GET allowlist に `/control/presence/report` 追加＋**フルパス転送に変更**（`?days=` を gateway へ届ける。従来は `split("?")[0]` で query が落ちていた）。
- `~/razer-dashboard/dashboard.html`（git外）: `#tab-diagnostics` タブ＋ナビ＋`showTab`＋`loadDiagnostics`/`renderDiagnostics`。オンデマンド（ポーリング無し）。時間帯別は直近48時間まで表示・全量は日次レポートに記録と明示。

## ★実データ検証で見つけた方法論バグ（censoring）

実ログ121k行で `build_report` を直接実行（0.62s）して気づいた: ACTIVE 谷間の `p99=1079s`・`max=1079.4s` が現 `absent_after_s`（1080s）にぴったり張り付く。= ACTIVE 谷間はしきい値で右側打ち切りされる。初版の「ACTIVE 谷間 p99 × 1.25」は実質「現状値 × 1.25」を返し、適用するたび上方へラチェットする欠陥。

→ **修正**: 推奨値は **resolved 谷間（しきい値前に在室へ戻ったもの）の p99 × 1.25** を母数にする。打ち切られた谷間数（censored）は「ACTIVE 中にしきい値到達＝不在判定された回数（真の離席かセンサー死角か要観察）」として別指標で提示。

実データ結果: ACTIVE 谷間 315 個中 resolved 286 / censored 29（1075〜1079s に密集）。resolved p99=925s → 推奨 **1170s**（現状 1080s と「ほぼ整合」）。これは現状値の単純倍ではなく実際の在室中ドロップアウトに基づく意味のある値。`scratch/analyze_presence.py` は数値羅列で人間が目視していたため、この打ち切りは顕在化していなかった。

教訓: 「しきい値で状態が切り替わる系のログから、そのしきい値を推定する」と必ず打ち切りが入る。母数を打ち切り前（resolved）に限定し、打ち切り数を別指標で出す。

## 検証

- gateway pytest **1062 passed** / ruff clean（新規 `test_presence_report.py` 39本＋`test_presence.py`/`test_http_server.py` 追記）。
- 実ログ121k行で `build_report` 0.62s（`to_thread`＋`?days` で event loop を塞がない）。
- dashboard 埋め込み JS は `node --check` で構文 OK。
- **M5（実機 E2E）はサービス再起動が必要**: `sudo systemctl restart stackchan-gateway && sudo systemctl restart status-api` → `curl :8080/control/presence/report?days=7` ＋ ダッシュボード「📊診断」タブ ＋ `~/.stackchan/presence_reports/<今日>.md` 生成確認（再起動後の初回 poll で当日分を即時書き出す）。

## 次

- M5 実機 E2E（ユーザー sudo 後）。
- M6 learning-report（`docs/presence-diagnostics-report.md`）。
- Phase 3（生活リズム学習＋自動較正＝推奨値の承認 UI）。`recommended_absent_after_s` を機械可読で持たせ済み。
