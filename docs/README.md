# docs/ — 索引

このディレクトリは yorishiro（依代 / Hermes Agent 身体化プラットフォーム）の**設計ドキュメント・各 Phase の振り返りレポート・時系列の作業記録**を収めています。
全体像を掴みたいときは Architecture から、運用手順は Setup & operations、何をどう作ってきたかは Phase reports / Worklog を参照してください。

---

## Architecture & design

- [architecture.md](architecture.md) — gateway と firmware が MCP クライアントと StackChan キットをどう橋渡しするかの全体構成図・コンポーネント概観

## Setup & operations

- [firmware-sync.md](firmware-sync.md) — upstream `xiaozhi-esp32` を追従しつつ board support を再現可能に保つ同期手順（3 リポジトリの役割）
- [remote-access.md](remote-access.md) — gateway を別ネットワークから到達可能にする Tailscale Funnel 経由のリモートアクセス手順
- [178-daemon-setup.md](178-daemon-setup.md) — gateway の 2 つの MCP サーバーモード（stdio / daemon = streamable-http）のセットアップ
- [178-http-transport-spike.md](178-http-transport-spike.md) — Issue #178 Phase A スパイク。daemon transport に Streamable HTTP を選定した検証記録
- [deploy/](deploy/) — gateway を systemd サービスとして常駐させる unit ファイル（`stackchan-gateway.service` + local-llm / heartbeat の drop-in）

## Phase reports（学習用レポート）

- [phase-a-report.md](phase-a-report.md) — Phase 0/A: ビルド環境構築〜実機書き込み〜顔表示まで「器」を起こす
- [phase-b-report.md](phase-b-report.md) — Phase B: 録音 → STT → Hermes → TTS → 再生の音声会話パイプライン
- [phase-c-report.md](phase-c-report.md) — Phase C: 応答ルーティング・SwitchBot 家電連携・近接リフレックスで「速く答えて家電を操る」
- [phase-d-report.md](phase-d-report.md) — Phase D: heartbeat・検索ツール・ノートツールで「ひとりで動き道具を使う」
- [phase-e-report.md](phase-e-report.md) — Phase E: 通知型 heartbeat。価値があるときだけ話す抑制設計
- [phase-f-report.md](phase-f-report.md) — Phase F: ダッシュボード操作・顔ステータス・首の中立姿勢・ウェイクワード診断

## Dashboard reports

- [dashboard-expansion-report.md](dashboard-expansion-report.md) — 操作ダッシュボード（操作盤）を育てた全 5 フェーズの振り返り
- [dashboard-cc-notify-mode-sync-report.md](dashboard-cc-notify-mode-sync-report.md) — モード機能のフォロー 3 件（CC 通知のモード連動 / サーバータブ切替 / 配置調整）

## Worklog（時系列の作業記録）

- [worklog/2026-06-10-phase-b-voice.md](worklog/2026-06-10-phase-b-voice.md) — Phase B 音声会話パイプライン構築
- [worklog/2026-06-11-phase-c.md](worklog/2026-06-11-phase-c.md) — Phase C 認証修復 / SwitchBot / 応答ルーティング / 近接リフレックス
- [worklog/2026-06-11-phase-d-autonomy.md](worklog/2026-06-11-phase-d-autonomy.md) — Phase D 自律性 heartbeat / 検索ツール / ノートツール
- [worklog/2026-06-12-phase-e-notify-heartbeat.md](worklog/2026-06-12-phase-e-notify-heartbeat.md) — Phase E 通知型 heartbeat（価値があるときだけ話す）
- [worklog/2026-06-13-c1-prox-reflex.md](worklog/2026-06-13-c1-prox-reflex.md) — C1 手かざしリフレックス復活（LTR-553 再計測）+ Phase E 仕上げ
- [worklog/2026-06-13-phase-f-dashboard.md](worklog/2026-06-13-phase-f-dashboard.md) — Phase F ダッシュボード操作・顔ステータス・仕草 OFF・ウェイクワードの 4 機能実装
- [worklog/2026-06-14-cc-notify-dashboard-toggle.md](worklog/2026-06-14-cc-notify-dashboard-toggle.md) — Claude Code 発話通知を gateway 経由で復活 + ダッシュボードにトグル新設
- [worklog/2026-06-14-wakeword-diagnosis-neckpose.md](worklog/2026-06-14-wakeword-diagnosis-neckpose.md) — ウェイクワード決定的診断 + 首角度のダッシュボード調整
- [worklog/2026-06-14-phase1-dashboard-sections.md](worklog/2026-06-14-phase1-dashboard-sections.md) — ダッシュボード拡張フェーズ1: 機能カテゴリ別 7 カード構成へ再編
- [worklog/2026-06-14-phase2-brightness-led.md](worklog/2026-06-14-phase2-brightness-led.md) — フェーズ2: 画面明るさ + 台座 LED 制御 UI
- [worklog/2026-06-14-phase3-proximity-led.md](worklog/2026-06-14-phase3-proximity-led.md) — フェーズ3: 近接 listen mode + トグル + LED 明るさ/横並び
- [worklog/2026-06-14-review-cleanup.md](worklog/2026-06-14-review-cleanup.md) — develop 全体レビュー & クリーンアップ（heartbeat 割り込み・ウェイクワード黙殺バグ修正ほか）
- [worklog/2026-06-14-flash-e2e.md](worklog/2026-06-14-flash-e2e.md) — 実機 flash + E2E 検証（feature/review-cleanup クローズ）
- [worklog/2026-06-15-mode-presets.md](worklog/2026-06-15-mode-presets.md) — モード（プリセット）機能 + 初期タブ変更
- [worklog/2026-06-15-cc-notify-mode-sync.md](worklog/2026-06-15-cc-notify-mode-sync.md) — CC 通知のモード連動 + サーバータブ切替 + 配置調整
- [worklog/2026-06-15-hermes-pin-toggle.md](worklog/2026-06-15-hermes-pin-toggle.md) — 応答ルーティング「Hermes 固定」トグル
- [worklog/2026-06-15-phase4-decision.md](worklog/2026-06-15-phase4-decision.md) — フェーズ4: サーバタブの Codex / Gemini 利用率 → 取得手段の障害で両方スキップ
- [worklog/2026-06-15-phase5-dashboard-ergonomics.md](worklog/2026-06-15-phase5-dashboard-ergonomics.md) — ダッシュボード拡張フェーズ5（人間工学的仕上げ）
