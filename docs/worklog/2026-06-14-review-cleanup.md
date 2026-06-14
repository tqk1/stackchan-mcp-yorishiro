# 2026-06-14 全体レビュー & クリーンアップ

ブランチ: `feature/review-cleanup`（`develop` から分岐）

## 目的

Phase A〜F で `develop` に約12,600行を追加してきたが、開発途中に何度もコンテキスト切れで「止まって再開」を繰り返した。表面上は動いているが、中途半端な実装・残骸・実害バグが混入していないかを、このタイミングで一度横断レビューする。不要なものは削除し、判断が要るものはユーザーに確認する。

## レビュー手法（多エージェント並行）

| 役割 | 担当 | 成果 |
|---|---|---|
| gateway 健全性 | Explore | 「ほぼ問題なし」と楽観評価 |
| firmware 健全性 | Explore | ウェイクワード残骸を検出 |
| リポジトリ衛生・秘匿情報 | Explore | dashboard バックアップ、秘匿分離の確認 |
| 機械チェック | `ruff check` | 未使用 import/変数ゼロ（楽観評価の裏取り） |
| 論理面の第二意見 | **Codex（codex-rescue）** | Explore が見逃した**実害バグを発見**、firmware 調査の誤検出2件を棄却 |

**学び**: Explore は「抜粋読み」のため構造把握は速いが、並行性・状態遷移のような実行時の論理バグは見落としやすい。`ruff`（静的）で機械的裏取りし、Codex に「論理面を精読・refute せよ」と投げる二段構えが効いた。実際、最重要バグ（後述①）は Codex だけが拾った。

## 発見と対応

### 🔴 実害バグ（修正済み）

1. **heartbeat が会話中に割り込む** — `gateway/stackchan_mcp/heartbeat.py` `_skip_reason()`
   - 原因: skip 判定が `tts_lock.locked()` のみ。`tts_lock` は TTS 再生中しか保持されず、STT→Hermes 処理待ちの数秒間は解放されている。その窓で heartbeat tick が通り、首ジェスチャーが割り込む。**設計原則①「夫婦の会話に割り込まない」に直接違反**。
   - 修正: `getattr(self._gateway, "voice_turn_active", False)` を確認して会話ターン中は skip。このフラグは `gateway.py:61` で初期化、`hermes_bridge.py:253/273` で set/clear、`stdio_server.py:406` でも同じパターンで参照済みだった（既存の正規ルートに揃えた）。

2. **firmware ウェイクワード初期化が黙って失敗** — `firmware/main/audio/wake_words/custom_wake_word.cc`
   - 原因: `ParseWakenetModelConfig()` が index.json の読込/パース失敗時に void return するだけで、`Initialize()` が成功（true）を返す。`commands_` が空のまま起動し、ウェイクワードが永遠に検出されない「無言詰まり」になる（量産ビルドで顕在化）。
   - 修正: `ParseWakenetModelConfig()` を `bool` 返却に変更（`.h`/`.cc` 両方）。`Initialize()` で「パース失敗 or `commands_` 空」なら `ESP_LOGE` ＋ `false` 返却。`models_list == nullptr` のフォールバック経路は無改変。

### 🗑️ 残骸削除（削除済み）

3. **firmware: `[TEST BUILD ONLY]` ウェイクワード探索ブロック** — `custom_wake_word.cc:97-120`
   - ②「スタックちゃん」認識は MultiNet 中国語ピンインの限界で既にクローズ済み（タップ/背面なで運用）。にもかかわらず、探索用の「12候補一括登録＋threshold 0.1」ブロックが `#ifdef CONFIG_CUSTOM_WAKE_WORD` で**本番ビルド有効のまま**残り、config.json の単一ワード設定（`su ta ke qiang` / threshold 20）を上書きしていた。
   - 対応: テストブロックと `[TEST]` ログ群を除去し、`ParseWakenetModelConfig()` 経由の単一ワード方式に一本化。`esp_mn_commands_*` の登録本体は温存。将来 microWakeWord 移行の余地は残した。

4. **dashboard 手動バックアップ群**（git 管理外 `~/razer-dashboard/`）
   - `dashboard.html.bak-*`×5、`status_api.py.bak-*`×5、`fetch_usage.log`、`__pycache__/` を削除（計 ~146KB）。現行 `dashboard.html`/`status_api.py`（06-14）が最新確定版であることを mtime で確認してから実行。

### 🟡 軽微な堅牢性改善（修正済み）

5. `heartbeat._save_state()` を `control.py` 同様の `tempfile.mkstemp + os.replace` アトミック書き込みに（書き込み中断で日次カウントが壊れるのを防止）。
6. `control.py` の `mute()`/`unmute()` をモジュールレベル `asyncio.Lock` で保護（ダッシュボード同時操作で `pre_mute_volume` が0に上書きされる競合を防止）。
7. `local_llm.py` の `STACKCHAN_LOCAL_LLM_TIMEOUT_S` パースガード（非数値でも WARN ＋ default フォールバック、設定ミスの黙過を解消）。

### 🟢 誤検出（対応不要、Codex が棄却）

- `cores3_audio_codec.cc` の input_gain default が「2箇所に分散」→ 実際はコンストラクタ1箇所のみ、NVS フォールバックは現値参照で正常。
- `mcp_server.cc` の mic_gain 手動 min/max チェックが「冗長」→ Property の min/max はスキーマ出力専用でバリデーションしないため、手動チェックは**必要**。

### 📋 整理

- `tasks/todo.md`: 555行 → 51行。完了済み Phase 記録を `tasks/todo-archive-2026Q2.md` に byte-identical で退避し、生きた未完了項目（ToF VL53L0X 待ちの部屋スケール視線追従、microWakeWord 将来候補、Phase D 遠い将来 TODO 等）だけを残した。
- 保持: `gateway/stackchan_mcp/server.py`（README に "legacy / unused in prod" と明記された fork 元由来モジュール。upstream 同期のため温存）。

## レビュー → 修正 → 検証フロー

```
[3 Explore 並行] ─┐
[ruff 機械チェック]├→ 発見の統合 → ユーザー判断(AskUserQuestion×4)
[Codex 論理精読] ─┘            │
                               ▼
        ┌──────── feature/review-cleanup ────────┐
        │  gateway 修正(並行委任)   firmware 修正(並行委任) │
        │  不要ファイル削除         todo.md アーカイブ      │
        └────────────────────┬────────────────────┘
                              ▼
   検証: pytest 756✅ / ruff✅ / Docker build✅ / 秘匿・差分混入なし✅
                              ▼
            実機 flash + E2E（ケンジさん協力・次段）
```

## 用語・仕組みメモ（学習用）

- **アトミック書き込み（`tempfile.mkstemp` + `os.replace`）**: 一時ファイルに全部書いてから `os.replace` で本ファイルに「名前を差し替える」。`os.replace` は OS レベルで不可分なので、途中でプロセスが落ちても本ファイルが半端な状態にならない。`control.py` が既にこの方式で、今回 `heartbeat.py` も揃えた。
- **`asyncio.Lock`**: 非同期処理で「read → 変更 → write」の途中に別の処理が割り込むのを防ぐ鍵。`await` の度に処理が他へ譲られるため、ロックなしだと同時 mute/unmute で値が壊れる。
- **`voice_turn_active`**: gateway が「いま音声ターン処理中（録音→STT→Hermes→TTS）」かを示すフラグ。heartbeat の自律ジェスチャーがこの間に割り込まないためのゲート。
- **MultiNet / ウェイクワード**: ESP-SR の命令語認識エンジン。日本語語を中国語ピンイン近似で登録する方式は精度に限界があり、②はこれが根因でクローズ。`ParseWakenetModelConfig()` は端末上の `index.json`（モデル定義）を読んでコマンド/閾値を設定する初期化処理。
- **`#ifdef CONFIG_CUSTOM_WAKE_WORD`**: ビルド時に Kconfig マクロが定義されていればコンパイルされるブロック。config.json でこのマクロが定義されていたため、テスト専用のつもりのコードが本番に焼かれていた＝「止めて再開」中に戻し忘れた典型的な残骸。

## 検証結果

- gateway: `uv run pytest` → **756 passed** / `uv run ruff check .` → **All checks passed**
- firmware: Docker `espressif/idf:v5.5.2` で `release.py stackchan` → **exit 0**、`custom_wake_word.cc` コンパイル済み（警告なし）、`releases/v2.2.6_stackchan.zip` 生成
- 秘匿情報: `sdkconfig.defaults.local`（IP/token）・`releases/*.zip` とも git 管理外を確認

## 残タスク

- [x] コミット（3分割: `fix(gateway)` / `fix(firmware)` / `docs`、ブランチ `feature/review-cleanup`、未 push）
- [ ] **実機 flash 未完了 → 次セッションに持ち越し**（原因調査後に実施）。E2E（焼けたら）: ①顔が出てタップで首が動く、②会話中（STT→Hermes 待ち）に首が勝手に動かない（設計原則①、今回の本丸）。

### flash 難航の記録（次回への申し送り）

`build/xiaozhi.bin`（app-only @ `0x20000`、assets 不変）を焼こうとしたが書き込めなかった。経緯と切り分け:

- **症状**: esptool が接続時に `OSError: [Errno 71] Protocol error` — pyserial の `_update_rts_state()` の `TIOCMBIC`(RTS) ioctl が EPROTO。Docker（idf イメージ）・ホスト venv（`~/.venvs/esptool` v5.3.0）の**両方**で再現。`--before` を `default-reset` / `usb-reset` のどちらにしても同じ（どちらも reset strategy 内で `port.open()` し RTS を叩く）。
- **切り分け**: `--before no-reset`（NoOpReset、RTS を叩かない）では EPROTO が消え、代わりに「No serial data received」= デバイスがダウンロードモードにいないだけ。→ **RTS 由来は確定。自動リセットが効かない層が原因**。
- **手動ダウンロードモード**: CoreS3 のリセットボタン長押しを試みたが、USB が切断/電源オフになり `/dev/ttyACM0` が頻繁に消失。esptool 接続待ち（`--connect-attempts 0` バックグラウンド）も、ダウンロードモード遷移時の USB 切断で fd が無効化され「Write timeout」。遠隔での安定操作が困難 → ユーザー判断で次回に延期。
- **重要な手掛かり**: 前回 develop 版を焼いた時は **Claude Code の esptool 自動リセットだけで（ボタン操作なしで）焼けていた**。つまり **develop 版ファーム自体が USB-Serial-JTAG の reset 要求をブロックするようになった**疑いが濃厚。

### 次回の調査方針

1. `firmware/sdkconfig.defaults*` / `config.json` の `CONFIG_ESP_CONSOLE_*`、特に `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG` / `CONFIG_ESP_CONSOLE_SECONDARY_*` / USB 関連を、**develop と「前回焼けた版」で diff** し、USB-Serial-JTAG の自動 reset を妨げる設定変更を特定する。
2. 恒久対策（例: コンソールを UART に寄せる / USB-Serial-JTAG の reset を維持する設定）を入れてから焼く。これが入れば今後は Claude Code 単独で焼けるはず。
3. どうしても今版を焼く必要があれば、CoreS3 を安定した USB 接続で確実にダウンロードモードに入れ（緑 LED、ポート再出現を `ls` 確認）、`--before no-reset --after no-reset` で flash。
4. flash 後は `sudo systemctl start ModemManager` を戻す（今回 flash の競合切り分けで一時停止した。なお ModemManager は今回の EPROTO の原因では**なかった**）。
