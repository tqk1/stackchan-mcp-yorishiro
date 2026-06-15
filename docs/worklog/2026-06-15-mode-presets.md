# worklog 2026-06-15 — モード（プリセット）機能 + 初期タブ変更

ダッシュボード機能拡張プロジェクト完了後の**追加要望**2件。

1. **モード機能**: 現在の全設定を名前付きプリセットとして保存し、いつでも一括適用。
2. **初期表示をサーバータブに**。

計画: `~/.claude/plans/5-shimmying-cherny.md`（モード機能版に書き直し）。

---

## 確定した設計判断

- **保存範囲 = 現在の全設定スナップショット（首の向き neutral_pose は除く）**。
  - neutral_pose を外した理由（ユーザー指示）: **置いている場所に依存する**設定であり「モード」概念に合わない。加えて firmware NVS にあるが**読み戻し手段が無い**唯一の設定だったため、外すと gateway 改修が **純粋な追加だけ**で済む（既存 `set_neutral_pose`/`load_state`/`save_state` に手を入れない）。
- **保存場所 = gateway 側**（`~/.stackchan/presets/<name>.json`、1プリセット1ファイル）。全端末で共有・ブラウザのキャッシュ削除でも残る＝セッション跨ぎで安定。
- **2フェーズ（/clear 境界）**: M1=gateway バックエンド（pytest 検証→コミット）、M2=ダッシュボード UI（HTML-only・実機 E2E）。

### プリセットに入る設定

| 設定 | スナップショット元 | 適用方法 |
|---|---|---|
| volume / muted / pre_mute_volume | `load_state()` | `set_volume` /（muted時）`mute` |
| mic_gain | `load_state()` | `set_mic_gain` |
| brightness | `load_state()` | `set_brightness` |
| led{brightness, idle/listening/hermes 色} | `load_state()` | `set_led_brightness` + `set_led`×3 |
| proximity{mode, threshold} | `_proximity_status()`（device 問い合わせ） | `call_tool("self.touch.set_proximity_config")` |
| heartbeat{gestures} | `_heartbeat_status()`（runner） | `runner.set_gestures()` |

除外: 首の向き / 表情 / 聞き取りトリガー / CC通知 / テスト発話。

---

## M1: gateway バックエンド（コミット `2d13ac7`）

### `gateway/stackchan_mcp/control.py`（純追加）
- 保存先 `~/.stackchan/presets`（env `STACKCHAN_PRESETS_DIR` で上書き＝テスト用）。
- `normalize_preset_name`: 名前を**サニタイズ**（`/`・`\`・`..`・先頭ドット・制御文字・33文字以上を拒否＝パストラバーサル防止）。日本語名OK。
- `save_preset(name, snapshot, *, overwrite=False)` / `list_presets()` / `load_preset()` / `delete_preset()`：`save_state` と同じ atomic write（tempfile + os.replace）。
- `_sanitize_snapshot`: クランプ＋プリセット関連フィールドだけ残す（heartbeat の speak/interval_min 等は捨てる）。
- `apply_preset(gateway, name)`: `_preset_lock`（asyncio.Lock）＋ `voice_turn_active` ガードで保護。既存セッターを順に呼び、各失敗を `failed[]` に集約（部分適用は `ok:false`）。

### `gateway/stackchan_mcp/http_server.py`
- `_build_preset_snapshot(gateway)`: 現在値スナップショットを `_build_control_status` と同じソース（`load_state` + `_proximity_status` + `_heartbeat_status`）で組み立て。
- エンドポイント4本（`build_app` 内・既存 token/host ガード配下）:
  - `GET  /control/presets/list`
  - `POST /control/presets/save`（device 接続必須＝proximity を問い合わせるため。同名は overwrite フラグ、未指定で409）
  - `POST /control/presets/apply`（接続必須＋voice turn 中は409）
  - `POST /control/presets/delete`

### 実装上の発見（学習ポイント）
- 短縮ツール名 `set_proximity_config` は `stdio_server._dispatch_mcp_tool` の中で **`self.touch.set_proximity_config`** に解決される。control.py からは `gateway.esp32.call_tool` を**フルネーム**で呼んで統一した（他のツールも `self.audio_speaker.set_volume` 等フルネーム）。

### 検証
- `pytest 816 passed`（+18: control 10 / http 8）/ `ruff clean`。
- テスト基盤は既存の `FakeGateway`/`FakeESP32`/`ControlFakeGateway`/`FakeHeartbeat` + `tmp_path` + `monkeypatch.setenv` を流用。

---

## M2: ダッシュボード UI（`~/razer-dashboard/dashboard.html`・git管理外・即反映）

- 「よく使う」グループ**先頭**に **モードカード**を新設（確立済みの型 `.card`/`.row`/`.field`/`.ctl-row`/`.btn-act`/`.btn-primary`/`.btn-warn` を流用、独自実装ゼロ）:
  - 適用: `<select>`（保存済み一覧）+「適用」（`btn-primary`）+「🗑」削除（`btn-warn`+`confirm()`）。
  - 保存: 名前入力（maxlength=32）+「保存」。同名は `confirm()` で上書き確認。
- JS: `loadPresets`/`renderPresetOptions`/`scApplyPreset`/`scSavePreset`/`scDeletePreset`。適用成功後に `loadStackchan()` で全コントロール再同期。一覧はタブ切替時＋保存/削除後に再取得（30秒ポーリングには載せない＝ドロップダウン操作を邪魔しない）。
- 適用/削除ボタンは初期 `disabled`、プリセット読込成功時のみ有効化（空状態を明示）。

### 初期タブ変更（要望2）
- `activeTab='server'` + `showTab('server')`、冗長な `load()` を除去。Playwright で初期=サーバータブを確認。

### 検証
- **自動**: `node --check` OK。Playwright（同梱chromium）でモードカードが「よく使う」先頭に描画・保存ボタン44px・**JS例外(pageerror)0**・初期適用/削除 disabled を確認。
- ライブ gateway は旧コードのため `/control/presets/list` は再起動まで404（dashboard は静かに無視）。**バックエンド挙動は pytest 816 で担保**。
- **残（ユーザー実機 E2E）**: gateway 再起動 →（a）設定変更→「保存」で命名（b）別の値に変更（c）保存モードを「適用」→ 音量/明るさ/LED/近接/heartbeat が一括で戻る（d）別端末/再読込でも同じモードが見える。

---

## 用語メモ（学習用）

- **プリセット/モード**: 設定一式の名前付きスナップショット。1ファイル=1モードで gateway に保存。
- **スナップショット**: ある時点の設定値をまとめて写し取ったもの。`_build_preset_snapshot` が現在値を集める。
- **atomic write（tempfile + os.replace）**: 一時ファイルに書いてから原子的に置き換える保存法。書き込み途中で電源が落ちても壊れたファイルが残らない。`save_state` と同じ流儀。
- **パストラバーサル**: `../` 等でディレクトリ外のファイルを触らせる攻撃。名前をファイル名に使うので `normalize_preset_name` で防ぐ。
- **`_preset_lock`/voice_turn ガード**: 一括適用が会話中や別の適用と重なって設定が混線しないよう直列化する。

## 事件簿: 「保存を押しても反応がない」（2026-06-15・実機投入直後）

- **症状**: モードカードの「保存」を押しても無反応。
- **調査**: `:8080` 経由で実挙動を確認 → `GET /control/presets/list` も `POST /control/presets/save` も 404。
- **原因2つ**:
  1. **gateway 未再起動**: POST は status_api(`startswith("/control/")`)で gateway へ転送されるが、旧コードの gateway が 404「Not Found」を返していた（要 `systemctl restart stackchan-gateway.service`）。
  2. **status_api の GET プロキシは固定パス許可リスト**（`do_GET` の `if path in (...)`）で、`/control/presets/list` が**含まれていなかった**。POST は prefix 一致で通るが GET は通らない。→ 計画段階の「`/control/*` は汎用プロキシ」という前提は **POST のみ正しく、GET は allowlist** だった。
- **修正**:
  - `status_api.py do_GET` の allowlist に `/control/presets/list` を追加（要 `systemctl restart status-api.service`）。
  - dashboard: 保存/適用/削除の**失敗フィードバックをモードカード近く** `sc-preset-fb` に表示（従来は遠い `sc-err` のみ＝「反応がない」誤認の一因）。`presetFb` を緑/赤で出し分け。
- **教訓**: > 既存プロキシに新パスを足すときは、GET と POST で**転送条件が違う**ことがある（status_api は POST=prefix / GET=exact allowlist）。新エンドポイントは GET/POST 両方で実際に叩いて確認する。

## ステータス

**完了（2026-06-15）**。M1（gateway バックエンド・コミット `2d13ac7`）+ M2（dashboard UI）+ 上記2バグ修正（status_api GET allowlist / 失敗フィードバック）。両サービス再起動後に**ユーザー実機 E2E 成功（「いい感じ」）**＝保存→適用の一括復元が実機で動作確認。learning-report は不要（worklog のみ・ユーザー確認済）。

要望2（初期タブ=サーバー）も完了。ダッシュボードの追加要望2件はこれでクローズ。
