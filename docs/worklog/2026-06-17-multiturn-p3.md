# マルチターン会話 Phase 3 — 仕上げ（ダッシュボードトグル + UX）

日付: 2026-06-17 / ブランチ: `feature/multiturn` / ステータス: **gateway 実装完了・実機 E2E 待ち（未コミット）**

---

## 作業概要

Phase 1（継続判定）+ Phase 2（文脈保持）に続く Phase 3。マルチターン会話を **ダッシュボードから ON/OFF** でき、設定が **gateway 再起動後も維持** されるようにし、加えて **上限到達時の UX** を改善した。すべて gateway 完結で firmware ビルド/flash は不要。

確定スコープ（ユーザー 2026-06-17）:
- ① ダッシュボード ON/OFF トグル + 永続化
- ② 上限到達ターンの「タップして続けてね」字幕 UX
- 継続ターン短縮 LISTEN_TIMEOUT は**見送り**（firmware 領域・Phase 4）
- 全完了後 learning-report 作成（Phase 1-3 まとめ）

---

## 設計判断：env か永続トグルか

`multiturn.py:is_enabled()` の旧 docstring は「Phase 3 で env と永続トグルを **OR** する」と予告していたが、これは**不採用**にした。

理由: 本番デプロイは systemd drop-in `multiturn.conf` で `STACKCHAN_MULTITURN=1` を設定済み。OR だと env=1 が常に勝ち、**ダッシュボードで OFF にできない**＝トグルが実質無意味になる。

採用した設計（`routing_force_hermes` と同じ「純・永続フラグ」方式）:

- **永続トグル（`control.multiturn_enabled()`）が実行時の唯一の真実**。
- env `STACKCHAN_MULTITURN` は **state ファイルに `multiturn` キーが無いときの初期既定値**に降格（`control._default_multiturn()`）。
- 結果: 既存デプロイは初回 ON のまま（env=1 が初期値を seed）、ユーザーがダッシュボードで OFF にすると `multiturn: false` が永続化され、**env=1 でも OFF が勝つ**（直感的）。

循環 import 回避のため、control は multiturn を import せず env パース（4語の真偽判定）を薄く複製した。

---

## 変更ファイルと役割

```
[dashboard.html] 🔄連続会話トグル ──POST /control/multiturn {enabled}──┐
       │                                                              │
       │ GET /control/status (routing.multiturn で現在値追従)          │
       ▼                                                              ▼
[status_api.py :8080]  ← 変更なし（POST /control/* は汎用転送・GETは既存allowlist）
       │ 汎用プロキシ（トークン付与）
       ▼
[http_server.py :8767]  control_multiturn エンドポイント + status の routing ブロックに multiturn
       │
       ▼
[control.py]  multiturn を load_state/save_state に追加 + multiturn_enabled()/set_multiturn()
                                    ▲
                                    │ 実行時ゲート読み取り
[hermes_bridge.py] _maybe_continue: ゲートを env → control.multiturn_enabled() に変更
                   handle_voice_turn finally: 上限到達×質問なら字幕「タップして続けてね」
[gateway.py]  self.multiturn_prompt_pending = False（ワンショット・字幕用フラグ）
[multiturn.py] TAP_TO_CONTINUE_HINT 定数 + is_enabled docstring 更新
```

### 1. `control.py`
- `_default_multiturn()`: env `STACKCHAN_MULTITURN` の真偽（初期既定値の seed）。
- `load_state`/`save_state` に `multiturn`(bool) フィールド追加（default = `_default_multiturn()`）。
- `multiturn_enabled()` / `set_multiturn(enabled)`: `routing_force_hermes` / `set_routing_force_hermes` の完全ミラー。

### 2. `http_server.py`
- `control_multiturn` エンドポイント（`control_routing` のコピー、body `{"enabled": bool}` → `set_multiturn`）。
- `_build_control_status` の `routing` ブロックに `multiturn: state["multiturn"]` を同梱。
- Route `/control/multiturn` (POST) 登録。

### 3. `hermes_bridge.py`
- `_maybe_continue` のマスターゲートを `multiturn.is_enabled()`（env）→ `control.multiturn_enabled()`（永続）に変更。
- **UX**: `should_continue` が False を返したとき、「route==hermes ∧ 末尾？ ∧ turn_count≧max ∧ 接続中 ∧ 非mute ∧ 非録音」＝**上限到達のみが停止理由**なら `gateway.multiturn_prompt_pending = True`。`handle_voice_turn` の finally が（継続中でない時に）これを消費して字幕「タップして続けてね」を残す（それ以外は従来どおり字幕クリア）。フラグはワンショット（同一ターン内で set→consume）。

### 4. `dashboard.html`（`~/razer-dashboard/`・非 git・即反映）
- 「🧠 応答モード」カードに「🔄 連続会話」トグル追加（Hermes 固定トグルのミラー）。
- status 追従 JS（操作中は上書きしない `.disabled` ガード）+ change ハンドラ（`POST /control/multiturn`）。
- `status_api.py` は変更不要（POST 汎用転送・状態は GET /control/status 経由）。

---

## 検証

- **pytest 964 passed**（955 → +9: control 5 / http_server 2 / hermes_bridge 2）。
  - control: 既定 False / env からの seed / set ラウンドトリップ / **env override（OFF が env=1 に勝つ）** / 他設定書き込みで残存。
  - http_server: `/control/multiturn` 設定 + status 反映 / 非 bool 拒否。既存 routing status の完全一致テストに `multiturn:false` 追従。
  - hermes_bridge: **上限到達×質問→字幕ヒント** / **非質問の通常終了→字幕クリア（ヒント出ない）**。
- **ruff clean**（gateway 全体）。
- dashboard.html: script 構文 OK（new Function パース）・ID 整合（HTML/status/handler の3箇所）。
- 既存テスト互換: `_patch_voice_pipeline` で `control.multiturn_enabled` を `multiturn.is_enabled`（env リーダー）に束縛 → 既存 `setenv STACKCHAN_MULTITURN=1` テストが live state ファイルに依存せず通る（`routing_force_hermes` の既存スタブと同じ手法。ユーザーがダッシュボードでトグルして live に `multiturn` キーが書かれてもテストが壊れない）。

---

## 残（次のステップ）

- **実機 E2E（ユーザー）**: `sudo systemctl restart stackchan-gateway`（新エンドポイント/ゲート反映）→ ダッシュボードの「🔄 連続会話」で ON/OFF が効く・再起動後も維持・会話で上限到達時に字幕が出る・自然な往復。
  - 注: 再起動前は `/control/status` に `routing.multiturn` が無く、トグルは OFF 表示になる（実体は env=1 で ON）。再起動後に正しく追従。
- **E2E green 後**: commit（feature/multiturn）+ learning-report（Phase 1-3 まとめ・`docs/`）。

---

## 用語メモ

- **ワンショットフラグ**: 1 回だけ立てて 1 回で消費する状態。ここでは「このターンの finally で字幕を出すか」を `_maybe_continue`（try 内）→ finally に一方向で伝える。ターンをまたがない（継続発火 `multiturn_active` とは同一ターンで排他）。
- **default seeding**: 永続設定にキーが無いとき、初回だけ env から既定値を読む手法。移行時に既存 env デプロイの挙動を壊さず、以降は永続値が真実になる。
