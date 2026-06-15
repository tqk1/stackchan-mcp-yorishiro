# worklog 2026-06-15 — CC通知のモード連動 + サーバータブ切替 + 配置調整

モード（プリセット）機能のフォロー要望3件（[[2026-06-15-mode-presets]] の続き）。

1. **CC発話通知がモード保存/切り替えに反応しない** → モードに連動させる。
2. **作ったモードをサーバータブからも切り替えたい**。
3. **スタックちゃんタブのモード/現在設定保存を一番下に**。

計画: `~/.claude/plans/splendid-gliding-mochi.md`。**gateway には一切触れない**（別 repo・816 pytest・要再起動）。razer-dashboard 内で完結。

---

## 根本原因（要望1）

CC発話通知フラグ `~/.claude/hooks/stackchan_notify.off`（有=OFF / 無=ON）は **status_api.py 専有**（gateway とは意図的に「別系統」）。
一方プリセットは **gateway 側**（`~/.stackchan/presets/<name>.json`）に保存され、スナップショットは gateway が自前で組む（`_build_preset_snapshot`）。
→ gateway は CC通知フラグの存在を知らないので、保存にも適用にも入らない。両者を繋ぐ層が無かった。

---

## 確定した設計判断

- **CC通知は gateway に持ち込まない**。「別系統」設計を保つため、CC通知フラグの所有権は status_api のまま。
- **status_api にサイドカーを置く**: `~/.stackchan/preset_cc_notify.json`（map: `モード名 → bool`）。
  - status_api は :8080 で全ダッシュボードのプロキシ＝razer-server に1つ。サイドカーもここに置けば gateway 保存と同様「全端末で共有」される（CC通知フラグの効果も razer-server 単機なので整合）。
- **保存ペイロードは変更しない**（`{name, overwrite}` のまま）。status_api が保存横取り時に **自前で `cc_notify_enabled()` を読んで** 記録＝「現在の設定を保存」の意味論に合致。dashboard.html 側の保存ロジックは無改修。
- **適用後の UI 反映は既存挙動を流用**: スタックちゃんタブの適用は `loadStackchan()` が `GET /cc_notify` を再読込し `#sc-cc` を自動更新。

---

## Task 1: CC通知のモード連動（`status_api.py` のみ）

プロキシの転送と送出を分離し、プリセット POST だけ横取りして副作用を足す純追加。

| 追加/変更 | 内容 |
|---|---|
| 定数 `CC_NOTIFY_PRESETS` | `~/.stackchan/preset_cc_notify.json` |
| `set_cc_notify(enabled)` | フラグ rm/touch を関数化（`_cc_notify_post` と適用で共用）。OSError は呼び出し側へ |
| `load_preset_cc()`/`save_preset_cc()` | サイドカー I/O。読み壊れ/不在は空 dict、書き失敗は握りつぶす（best-effort） |
| `_read_body(default)` | Content-Length 読みを共通化 |
| `_forward_control(method, path, body) → (status, bytes)` | gateway 転送を**送出から分離**（503/502 も bytes 化）。`_proxy_control` はこれを呼んで `_send` するだけに（挙動不変） |
| `_payload_ok(payload)` | gateway 応答が `ok:false` でないか（副作用の発火可否） |
| `_proxy_preset_post(path)` | body から `name` を取り出し転送 → **成功時のみ** save=記録 / apply=復元 / delete=削除 |
| `do_POST` ルーティング | `/control/presets/{save,apply,delete}` を `_proxy_preset_post` へ。他 `/control/*` は従来の `_proxy_control` |

**graceful 設計**: name が無い/JSON でない → 副作用なしで素通し。サイドカーに記録の無いモード（旧モードや MCP 経由作成）を適用 → CC通知は現状維持（壊さない）。副作用の OSError は本応答に影響させない。

### 検証（再起動なし・分離テスト）
モジュール関数を一時パスに差し替えて単体検証 → **ALL OK**:
`set_cc_notify`(ON/OFF↔フラグ) / `cc_notify_enabled` / サイドカー round-trip + 破損時空dict / `_payload_ok`(true/false/欠落/非JSON) / save→手動変更→apply で復元 / delete で記録消去。

---

## Task 2: サーバータブにモード切替カード（`dashboard.html`）

- サーバータブ最下部（CC利用率カードの後・footer 前）に**切り替え専用**カード（`srv-preset-select` + `srv-preset-apply` + `srv-preset-fb`）。保存・削除はスタックちゃんタブのまま。
- `renderPresetOptions()` を **両 select 対応に一般化**（`[select, applyボタン, deleteボタン]` の組をループ、delete は sc のみ／フォーカス中ガードは各 select 個別）。
- `srvApplyPreset()` 追加（`scApplyPreset` のサーバー版）。サーバータブはデバイス UI 非表示なので `loadStackchan()` は呼ばない。CC通知復元は status_api 側で実行され次回スタックちゃんタブ表示時に反映。
- `presetFb(msg, ok, fbId)` に第3引数を追加（既定 `sc-preset-fb`）。
- `showTab('server')` で `loadPresets()` を呼び、サーバータブ表示時にも一覧を最新化。初期表示=サーバータブなので起動直後から埋まる。

## Task 3: モードセクションを最下部へ（`dashboard.html`）

「🎚 モード」section を「よく使う」先頭 → 「⚙️ デバイス調整」直後・`#sc-err` 前へ**位置のみ移動**（markup/ID/onclick 不変）。

### 検証（front-end 静的）
`section` 開閉 11=11、`tab-page` 2、`🎚 モード` 2（stackchan 最下部 + server）、主要 ID は各1。ライブ `:8080/dashboard` が更新後 HTML を配信・`/control/presets/list` 3件取得・`/cc_notify` 応答を確認。
※ Playwright は Chrome 未導入で不可 → 実描画はユーザー実機ブラウザで確認。

---

## 用語メモ（学習用）

- **サイドカー（sidecar）**: 本体（gateway のプリセット）に手を入れず、隣に置いて足りない情報（CC通知状態）を補う小さな別ファイル。本体の所有権・テストを汚さずに機能を継ぎ足す常套手段。
- **別系統（CC通知が gateway と分離している理由）**: CC通知は Claude Code/Codex の hook 群が見るフラグで、StackChan デバイス設定とは出自が違う。混ぜると hook 都合の変更が gateway を揺らすので分けてある。
- **転送と送出の分離（`_forward_control` 抽出）**: 「gateway に投げて結果を得る」と「ブラウザに返す」を別関数に。間に副作用（サイドカー更新）を挟めるようになる。`_proxy_control` の挙動は変えていない。
- **graceful degradation**: 記録が無い/失敗しても全体を止めず、できる範囲で動く（CC通知は現状維持）。本筋のプリセット操作を巻き添えにしない。

---

## ステータス

**コード完了・要ユーザー操作（2026-06-15）**。

- 変更ファイル: `~/razer-dashboard/status_api.py`（Task1）, `~/razer-dashboard/dashboard.html`（Task2/3）。いずれも git 管理外。
- **残（ユーザー実機）**:
  1. `! sudo systemctl restart status-api`（Task1 バックエンド有効化。dashboard.html は再起動不要・即反映）。
  2. ブラウザ更新 → Task1 E2E（CC通知 OFF→モード保存→ON に戻す→適用で OFF 復元・`#sc-cc` も外れる）/ Task2（サーバータブ最下部のカードで切替）/ Task3（スタックちゃんタブ最下部にモード）。
- **既存3モード（おやすみ/つうじょう/サポート）はサイドカー未記録** → 適用しても CC通知は変わらない。CC通知も連動させたいモードは**一度保存し直す**と現在の CC通知状態が記録される（worklog 明記の許容仕様）。

learning-report: `docs/dashboard-cc-notify-mode-sync-report.md` を作成（ユーザー要望）。
