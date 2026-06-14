# 2026-06-14 Claude Code 発話通知を gateway 経由で復活 + ダッシュボードにトグル

ブランチ: `develop`。ユーザー報告「ダッシュボード（razer-server.tailc0a7ab.ts.net）に Claude Code の
実行状況を話す機能の ON/OFF トグルが無い」を起点に調査・実装。

## 1. 発見：通知連携は「三重に停止」していた

「トグルが無い」だけでなく、機能自体が動かない状態だった（read-only 調査で確定）:

1. **hook 空振り** — `settings.json:33,47` が参照する `~/.claude/hooks/stackchan_cc_notify.sh` が**不在**
   （実在は `stackchan_notify.sh`）。Stop/Notification hook が発火しても何も起きない。
2. **フラグ OFF** — `~/.claude/hooks/stackchan_notify.off` が立っていた（6/13 10:00）。
3. **発話先ダウン** — 旧経路 yuno-chan-api `:5050/speak` が停止（curl 到達不可）。

→ トグルを足すだけでは喋らない。3点を揃える必要があった。

## 2. 方針（ユーザー決定 / AskUserQuestion）

**gateway 経由で復活**。yuno-chan-api :5050 依存を切り、yorishiro gateway の `/control/say`
（VOICEVOX・ダッシュボードのテスト発話と同経路）に一本化。

## 3. 構成図

```
[Claude Code / Codex]
  Stop / Notification hook
    │  ~/.claude/hooks/stackchan_cc_notify.sh <attention|done>
    │     - フラグ ~/.claude/hooks/stackchan_notify.off があれば exit 0（沈黙）
    │     - POST {"text":"…"}（トークン非保持）
    ▼
[status_api.py :8080]  POST /control/say
    │  Authorization: Bearer <STACKCHAN_TOKEN> を付与して中継（秘密はここ 1 箇所）
    ▼
[gateway :8767]  /control/say → tts.orchestrator.synthesize_and_send
    ▼  VOICEVOX → WebSocket
[StackChan]  発話

ON/OFF（共通フラグ stackchan_notify.off の有無）:
  ・/stackchan slash command（stackchan_toggle.sh）
  ・razer-dashboard「🔔 CC発話通知」トグル → status_api POST /cc_notify {enabled}
```

## 4. 実装（3 ファイル・すべて非 git）

- **`~/.claude/hooks/stackchan_cc_notify.sh`（新規）**: gateway 経由・トークン非保持・2 台分岐
  （Linux=localhost:8080 / mac=192.168.0.19:8080、`STACKCHAN_SAY_URL` で上書き可）・フラグ尊重・
  `$1`=attention|done。**settings.json は無改変**（既に当ファイル名を参照しているので作成で空振り解消）。
- **`~/razer-dashboard/status_api.py`**: 独立エンドポイント `GET/POST /cc_notify`（フラグを touch/rm）。
  `CC_NOTIFY_FLAG` 定数・`cc_notify_enabled()`。汎用 `/control/*` プロキシは無改変。バックアップ `.bak-20260614cc`。
- **`~/razer-dashboard/dashboard.html`**: 「🔔 CC発話通知」トグル（heartbeat と同型）。
  `loadStackchan` 末尾で `/cc_notify` を GET 反映、change で `scPost('/cc_notify',{enabled})`。バックアップ済み。

## 5. 設計判断

- **なぜ status_api 経由（hook が gateway を直叩きしない）**: gateway の `/control/*` はトークン保護。
  hook 直叩きだとトークンを 2 台共有 settings.json 圏に配る必要があり秘密の拡散面が増える。
  status_api が既にトークンを保持しているので、hook はトークンレスのまま localhost を叩くだけにした。
- **なぜ settings.json 無改変**: hook 定義は既に `stackchan_cc_notify.sh` を参照済み。
  ファイルを作るだけで「空振りバグ修正」と「経路差し替え」を同時に満たせる（変更を最小に）。
- **なぜ dashboard(status_api) が直接フラグ操作**: フラグは `~/.claude` 配下の Claude 運用物。
  gateway（git 管理・テスト対象の yorishiro 本体）に `~/.claude` を触らせるのは責務越境。
  ダッシュボード（同じく非 git の運用物）が触る方が責務が近い。

## 6. 検証（E2E・実機）

| テスト | 結果 |
|---|---|
| `/cc_notify` GET/POST | 200・状態が正しく反転 ✅ |
| トグル ON → hook 実行 | StackChan が「リナックスのクロードコード、お仕事終わったよ！」発話 ✅（ユーザー確認） |
| トグル OFF → hook 実行 | 無音（フラグでブロック・exit 0）✅ |
| dashboard 目視 | 「🔔 CC発話通知」トグル表示・操作 ✅（ユーザー確認） |

静的検証: status_api.py `py_compile` 通過 / dashboard インライン JS `node --check` 通過 / id 整合。

## 7. 用語

| 用語 | 説明 |
|---|---|
| Claude Code hook | Stop / Notification 等のイベントで外部コマンドを実行する仕組み（`settings.json` の `hooks`） |
| フラグファイル方式 | ファイルの有無で ON/OFF を表す。プロセス再起動不要で即反映（`stackchan_notify.off`） |
| プロキシ / Bearer トークン | status_api が `Authorization: Bearer` を付けて gateway に中継。ブラウザ/ hook に秘密を渡さない |
| `/control/say` | gateway の即時 TTS エンドポイント。`{"text":...}` を VOICEVOX で合成し StackChan で再生 |

## 8. 状態・積み残し

- 現在のフラグ状態: **OFF**（6/13 の設定を維持。ユーザーがダッシュボードのトグルで切替）。
- git 差分は `docs/worklog/`（本ファイル）と `tasks/todo.md` のみ（hook・dashboard・status_api は非 git）。
- 前タスクの `docs/phase-f-report.md`（Phase F learning-report）とまとめてコミット予定。
