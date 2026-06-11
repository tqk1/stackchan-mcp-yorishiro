# ✅ Phase D クローズ済み(2026-06-11 23:10)

**D4「Hermes が gateway の MCP ツールを呼ばない問題」は経路分離で解決**:

- 採用: 案 B(`~/.hermes/config.yaml` の `platform_toolsets.api_server` 追記、Hermes 本体無改造)
- 効果: 再起動後の `[stackchan-voice]` で `terminal`/`browser_*`/`write_file`/`read_file` の CallToolRequest **0 件**。家電(`mcp_stackchan_switchbot_send_command`)実灯 ✅、Discord 経路の `terminal` 無回帰 ✅、clarify による聞き返しも実観測 ✅
- 詳細: `docs/phase-d-report.md`(レポート)、`docs/worklog/2026-06-11-phase-d-autonomy.md` §6(作業ログ)
- バックアップ: `/home/kenji/.hermes/config.yaml.bak-20260611-phaseD`

## 残りの宿題(声を出しやすい時間帯に再検証)
1. **メモ単体 E2E**: 「○○のことメモして」発話 → `~/.stackchan/notes/*.md` 生成確認、agent.log で `mcp_stackchan_write_note` の CallToolRequest 確認
2. **検索単体 E2E**: 「○○のニュース調べて」発話 → agent.log で `mcp_stackchan_web_search` の CallToolRequest 確認
3. **Tavily API キー登録(任意・推奨)**: `~/.yorishiro/secrets.env` に `TAVILY_API_KEY=tvly-...`(未設定でも ddgs フォールバックで動く)

## 遠い将来の TODO
- 外部クライアント(Claude Code 等)から `/v1/chat/completions` で `terminal` が必要になったら、案 C(`HERMES_HOME` プロファイル分離)に切替。詳細は `docs/phase-d-report.md` §4.2

## ブランチ・コミット状態(Phase D)
- ブランチ: `feature/phase-d`(push 未実施)
- D4 編集差分: `~/.hermes/config.yaml`(git 管理外)、`docs/worklog/2026-06-11-phase-d-autonomy.md` §6、`docs/phase-d-report.md` 新規、`CLAUDE.md` 最終行、`tasks/todo.md` 本ブロック
- 直前の Hermes プロンプト誘導コミット: `0f997d2 fix(gateway): steer Hermes voice turns to MCP tools via system prompt`(D4 でより構造的解決を選んだが、保険として有効なので残す)

---

# Phase D — 自律性: heartbeat + Hermes MCP ツール追加 + LFM2.5 役割拡大（2026-06-11 着手）

## ユーザー決定事項（2026-06-11 AskUserQuestion で確認済み）
- **スコープ**: 全部。D1 heartbeat → D2 MCP ツール追加 → D3 LFM2.5 役割拡大検討の順に段階実施
- **heartbeat 挙動**: 段階導入。**第1段階は非音声のみ**（表情・首・LED、無音）。発話ありは動作が安定してから opt-in で追加（原則1「夫婦の会話に割り込まない」に配慮）
- **検索バックエンド**: **Tavily 主**（無料 1,000 クレジット/月、クレカ不要、要 TAVILY_API_KEY）+ **ddgs フォールバック**（キー未設定・障害時に自動切替）
- **learning-report**: Phase D 完了時に作成する（docs/phase-d-report.md）

## 調査済みの前提（2026-06-11 サブエージェント調査）
- gateway に定期実行の仕組みは皆無 → heartbeat は asyncio 周期タスクの新規実装。`Gateway.start()` 内で `asyncio.create_task()` する形が最短
- gateway → Hermes 方向は `hermes_bridge.py` の `ask_hermes()`（OpenAI 互換 :8642）実装済み → **heartbeat は gateway 側スケジューラに置けば原則4「Hermes は reactive のまま」を守れる**
- push 送信基盤あり: `ESP32Manager._connection` + `ESP32Connection` の WS send。MCP ツール（set_avatar / move_head / set_led 等）は gateway 内から直接呼べる
- 発火ガードが必要: ESP32 接続中か / 音声パイプライン（TTS/listen）非稼働か / クワイエットアワー
- MCP ツールは現在 31 個（stdio_server.py の list_tools）。検索・ファイルツールはここに追加

## D1: heartbeat（非音声・第1段階）
- [x] D1-1: `stackchan_mcp/heartbeat.py` 新規 (2026-06-11) — asyncio 周期タスク、`Gateway.start()` で起動 / `stop()` でキャンセル。**opt-in**: `STACKCHAN_HEARTBEAT_INTERVAL_MIN` 未設定なら完全無効。ジッター ±25%（`STACKCHAN_HEARTBEAT_JITTER`）
- [x] D1-2: 発火ガード実装 — ①device_connected ②tts_lock.locked()（TTS/listen 共用ロック）③クワイエットアワー（跨ぎ対応、デフォルト 22:00-08:00）。スキップは次 tick 待ち
- [x] D1-3: 仕草 3 種（見回し/表情/うなずき、全て無音・Hermes 不使用）。首は get_head_angles で元角度に復帰、読めなければ首は動かさない。仕草失敗でもループは死なない
- [~] D1-4: 単体テスト 22 件 ✅ / **実機 E2E はユーザーの sudo 作業待ち**（手順: docs/worklog/2026-06-11-phase-d-autonomy.md §5。1分間隔 drop-in → 仕草確認 → 本運用 heartbeat.conf へ差し替え）
- 将来（第2段階、今回はやらない）: `STACKCHAN_HEARTBEAT_SPEAK=1` で Hermes に文脈を渡して一言生成 → say。クワイエットアワー必須のまま

## D2: Hermes MCP ツール追加（検索・ファイル）
- [x] D2-1: `stackchan_mcp/web_search.py` (2026-06-11) — Tavily REST 直叩き（aiohttp、新依存なし、Bearer 認証）→ 失敗/未設定時 ddgs 自動フォールバック。extra `search`（ddgs>=9）追加、.venv に sync 済み。**ddgs 実検索スモーク成功**（日本語クエリ OK）
- [ ] D2-2: ユーザー作業 — Tavily にメール登録し `TAVILY_API_KEY` を `~/.yorishiro/secrets.env` へ追記（キー未設定でも ddgs で動く）
- [x] D2-3: `stackchan_mcp/notes.py` (2026-06-11) — `~/.stackchan/notes/` 配下限定（`STACKCHAN_NOTES_DIR` で変更可）の write_note（append 対応）/ read_note / list_notes。パストラバーサル防止・256KB 上限・.md/.txt のみ。stdio/HTTP MCP に計 4 ツール公開（BYPASS_TOOLS 入り、計 35 ツール）
- [~] D2-4: 単体テスト 19 件 ✅ / **Hermes 経由 E2E は gateway 再起動（sudo）後に**（音声で「〜を調べて」→ web_search → 音声報告 / 「メモして」→ write_note）
- ESP32 キューはバイパス（SwitchBot ツールと同パターン）

## D3: LFM2.5 役割拡大検討（検証中心、コードは最小）
- [x] D3-1: VRAM 確認 (2026-06-11) — **競合は実質なし**。GPU 利用者は Ollama (LFM2.5 0.7GB) のみ（faster-whisper=CPU int8、VOICEVOX=CPU、アイドル時 0 MiB / 6144 MiB）。同時稼働の懸念は解消
- [x] D3-2: ルーティング実績評価 (2026-06-11) — journalctl 集計: route=local 2 件 / hermes 6 件。**閾値拡大はデータ不足で時期尚早**（運用データ蓄積後に再評価）。代わりに**コールドスタート問題を発見**: local の llm 実測 2.8〜3.4s（warm 0.5s のはずが、keep_alive=30m 切れで毎回コールドロード）。**推奨: drop-in に `STACKCHAN_LOCAL_LLM_KEEP_ALIVE=24h` 追加**（コード変更不要、0.7GB 常駐は VRAM 的に許容）→ ユーザー承認後に適用
- [x] D3-3: heartbeat 第2段階の所見 — LFM2.5 はツール呼び出し不可だが「一言生成」なら適役。VRAM 余裕も確認済みで実現性あり（実装は将来の opt-in 段階で）

## フェーズ完了時
- [ ] worklog 更新（docs/worklog/2026-06-11-phase-d-autonomy.md、セッション中随時）
- [ ] learning-report 作成（docs/phase-d-report.md、Phase A/B/C と同テイスト）
- [ ] CLAUDE.md の開発ステータス更新

---

# Phase C 本体 — C1 近接視線追従 + C2 SwitchBot + C3 応答ルーティング（2026-06-11 着手、ユーザー承認: 3 本並行）

## 調査済みの前提（2026-06-11 サブエージェント調査）
- **C1**: firmware に近接センサードライバは無い（Si12T は背面タッチ専用、近接モード未使用）。Grove Port A の外部 I2C は MCP ツール `self.i2c.*` 実装済み（`stackchan.cc:5837`）。サーボは `WriteHeadAngles(yaw, pitch, ms)`（`stackchan.cc:3679`）、表情は `SetAvatarExpressionIfActive()`（`stackchan.cc:4381`）、周期反射の実装パターンは Si12T タッチポーリング（100ms esp_timer、`stackchan.cc:4017`）を踏襲可能
- **C2**: HA はこのマシンにも LAN にも存在しない → **SwitchBot API v1.1 直結の薄い MCP サーバー自作**が現実的。前提は Hub Mini/Hub 2 + 開発者トークン。Hermes には HA REST 直叩きツール（`tools/homeassistant_tool.py`、HASS_TOKEN で有効化）も眠っており、将来 HA を立てれば流用可
- **Hermes MCP 登録形式**: `~/.hermes/config.yaml` の `mcp_servers.<name>` に url/headers（`${VAR}` 環境変数展開対応、`mcp_tool.py:2234`）

## C-fix: Hermes→gateway MCP 認証修復（回帰・最優先）
今朝の STACKCHAN_TOKEN 有効化で /mcp が 401 になり、Hermes が 5 回リトライ後 giving up（05:02 ログ確認済み）。
- [x] Cfix-1: `~/.hermes/config.yaml` バックアップ (config.yaml.bak-20260611) + `mcp_servers.stackchan.headers` に `Authorization: Bearer ${STACKCHAN_TOKEN}` 追加（Hermes は `${VAR}` 展開対応 mcp_tool.py:2234、`~/.hermes/.env` を起動時ロード）
- [x] Cfix-2: ユーザーが token を `~/.hermes/.env` へ追記（grep で値非表示のまま転記）
- [x] Cfix-3: 再起動後 /mcp 200 OK、Hermes が MCP ツール呼び出し成功を E2E で確認 (2026-06-11 06:45)

## C1-0: 近接センサー内蔵有無の確認（C1 の分岐点）
- [x] `mcp_repl.py` で `i2c_scan`（外部 Grove バス）→ 空。**ただしユーザー指摘で CoreS3 内蔵の LTR-553ALS-WA（内部バス 0x23、近接+照度）を発見**（公式 docs.m5stack.com で確認。firmware は未ドライブだった）
- [x] 方針決定（ユーザー承認）: LTR-553 で「手かざしリフレックス」として実装（有効距離 数cm〜10cm。部屋スケールの視線追従は将来 ToF Unit で拡張可）

## C1: 近接リフレックス（firmware、LTR-553 手かざし）→ **物理的に不可と判明、ToF Unit 待ち**
- [x] C1-1/C1-2: LTR-553 ドライバ + リフレックス実装（stackchan.cc、Si12T パターン踏襲、コミット c306f8a）
- [x] C1-3: ビルド → flash → 実機キャリブレーション (2026-06-11) — **結論: 前面シェルに LTR-553 用開口がなく外界の IR が届かない**。感度最大（gain x64・15 パルス・LED 100mA）でも ps_raw はパネル内面クロストークの ~380 で固定、手かざしで全く変化せず
  - 副所見: 当初の閾値 200 < ベースライン 380 で**毎起動誤発火**していた → PROX_REFLEX_ENABLED=false + 閾値 700 に修正して再 flash 済み（ドライバと get_touch_state の ps_raw 診断は温存）
  - 仮にシェルを開口しても有効距離 ~10cm（手かざし専用）。本来の目標「近づくと向く」(1〜2m) には **M5Stack ToF Unit (VL53L0X, Grove Port A, ~¥1,000)** が必要 → 購入はユーザー判断待ち（外出中）
- 学び: `i2c_scan` ツールは外部 Grove バスのみ。内蔵チップは config.h の一覧 + 公式 docs で確認する

## C2: SwitchBot 家電操作（gateway 内 fork 専用モジュール）
前提: Hub Mini ×2（リビング・寝室）確認済み、トークンは `~/.yorishiro/secrets.env` に設定済み
- [x] C2-1: `stackchan_mcp/switchbot.py`（API v1.1、HMAC 署名）+ stdio/HTTP MCP に 3 ツール公開（list_devices / get_status / send_command）。ESP32 キューはバイパス。テスト 19 件
- [x] C2-2: 新サービス不要 — 既存 :8767 MCP HTTP に同居（Hermes 再登録も不要）。secrets.env は既存 EnvironmentFile で自動ロード
- [x] C2-3: E2E 成功 (2026-06-11 06:45-06:48) — 音声「デバイスを教えて」→ Hermes が switchbot_list_devices → 実デバイス一覧を音声報告。「リビングの電気をつけて」→ turnOn 実行（実灯確認: ユーザー回答待ち）

## C3: 応答ルーティング層（gateway `hermes_bridge.py` + `local_llm.py`）
- [x] C3-1: モデル選定 — LFM2.5-1.2B-JP Q4_K_M（warm 0.5s、0.7GB、日本語特化）。gemma3:4b は 1.5-2s で次点
- [x] C3-2: `decide_route()` 純関数（マーカー語 or >30 文字 → Hermes、短文 → local）+ Ollama 直行パス（keep_alive 30m）。**追加修正: 家電操作ワード（つけて/消して/電気/エアコン等）を Hermes マーカーに追加**（ローカルモデルはツールを呼べないため。C2 との整合）
- [x] C3-3: フォールバック実装（local 失敗 → Hermes）+ E2E 確認 — route=local で挨拶応答 OK（初回 3.4s はコールドスタート、以降 ~0.5s）。timings キーは hermes → llm に改名、route フィールド追加
- デプロイ: drop-in `docs/deploy/stackchan-gateway.service.d/local-llm.conf` を /etc に配備済み（外せばロールバック）

## フェーズ完了時
- [x] worklog 更新 + learning-report 作成 (2026-06-11) — `docs/phase-c-report.md`（Phase A/B レポートと同テイスト、約370行）。**Phase C クローズ**（C1 のみ ToF Unit 購入待ちで持ち越し）

---

# Phase C-0 — レイテンシ短縮: VAD 無音自動停止（2026-06-10 ユーザー承認: 案1+案3）

## 前提（調査済み）
- 体感遅延の主因は録音の 30s タイムアウト待ち。タップ停止 (`stackchan.cc:2330`) は実装済みだが、無音検出で自動停止できればタップ2回目も不要
- 案3（返答文字数制限）は **既に実装済み** — `hermes_bridge.py` の `DEFAULT_VOICE_SYSTEM_PROMPT`（1〜3文指定）が毎ターン注入されている → 追加作業なし
- AFE VAD は稼働中（`AudioService::IsVoiceDetected()` 公開済み、`application.h:67` に転送 accessor）だが停止には未結線

## 設計（firmware 最小変更、board ローカル）
- `PollTouchpad()` の既存タイムアウト管理と同じ場所に VAD 監視を追加:
  - `VAD_WARMUP_MS = 800` — listening 突入直後はポップアップ音の自己拾音を避けるため VAD 無視
  - 発話を一度でも検知 (`speech_seen`) した後、無音が `VAD_SILENCE_STOP_MS = 1200` 続いたら `StopListening()`
  - 発話未検知のまま無音でも止めない（VAD 不感への保険。30s タイムアウトが backstop）
- gateway 側変更なし。リスクが顕在化したら定数調整のみで済む構造

## チェックリスト
- [x] C0-1: `stackchan.cc` PollTouchpad に VAD 無音自動停止を実装 (2026-06-10) — speech_seen + 無音 1200ms で StopListening、warmup 800ms、未検知時は止めない（30s backstop 維持）
- [x] C0-2: Docker ビルド成功 (22:42、v2.2.6、mDNS 設定マージ確認)
- [x] C0-3: app flash + 再接続確認 (22:43、tools=30)
- [x] C0-4: 実機テスト合格 (2026-06-10 23:03) — 録音が発話分+1.2s で自動停止（38〜57 frames = 2〜3.4s、従来 498）。**STT 精度も回復**（「お元気ですか」を正確に転写。従来の転写崩壊は 30s の無音尾が whisper を劣化させていたのが原因で、マイクは正常）。ターン全体 9.8s（stt 0.6 / hermes 2.6 / tts 6.6 ※再生込み）。ユーザー所感「とても流暢」
  - 注意: 声が小さい/遠いと VAD が発話を検知せず 30s タイムアウト側に落ちる（22:56 のターンがこれ。設計通りの保険動作）。その場合も再タップで即送信可能
- [x] C0-5: コミット済み — firmware 682c38f (VAD auto-stop) / gateway 8c59987 (STACKCHAN_VOICE_DUMP_DIR 診断ダンプ、env 未設定なら no-op。systemd drop-in は未適用のまま=無効)

---

# Phase B — 音声最小往復: タップ → 録音 → STT → Hermes → TTS → 再生

着手日: 2026-06-10

## 設計確定（2026-06-10 ユーザー決定）
- **判断1（音声構成）**: (a) 完全ローカル — STT/TTS ともローカルエンジン、TTS は VOICEVOX
- **判断3（Hermes 接続）**: (b) MCP stdio — Hermes が MCP クライアントとして gateway のツール群を利用
  - 音声会話ターンの注入（gateway → Hermes 方向）は Hermes 内蔵の **APIServerAdapter**（OpenAI 互換 HTTP、port 8642、`API_SERVER_ENABLED=true` で有効化）を使用

## 調査済みの前提（2026-06-10 サブエージェント調査）
- **firmware**: 音声系は全部生きている。Opus enc/dec（esp_audio_codec、16kHz mono 60ms）、AW88298 スピーカー / ES7210 マイク（`boards/stackchan/cores3_audio_codec.cc`）、WS バイナリ音声フレーム送受（`websocket_protocol.cc`）、画面タップ→StartListening（`stackchan.cc` PollTouchpad）→ **自前実装は不要、結線と検証のみ**
- **gateway**: STT orchestrator（faster-whisper / openai-whisper）+ TTS orchestrator（VOICEVOX→Opus→WS 送信）実装済み。タッチ起動録音は `STACKCHAN_AUDIO_HOOK_URL` へ Ogg/Opus を POST する仕組みあり（**現状 URL 未設定でフレーム破棄中**）
- **Hermes**（`~/.hermes/hermes-agent`、systemd `hermes-gateway` で稼働中）: MCP クライアント内蔵（`hermes mcp add` で stdio サーバー登録可、現在 mcp_servers 未設定）。APIServerAdapter は `gateway/platforms/api_server.py`、`POST /api/sessions/{id}/chat` でセッション固定可。プロファイルは API 経由で指定不可＝起動時のアクティブプロファイル固定
- 注意: gateway の WS :8765 は 1 プロセス占有。検証中は standalone 起動、B7 で Hermes spawn に切り替える際は二重起動に注意

## ゴール（Phase B 完了条件）
画面タップ → 話しかける → StackChan が Hermes の応答を VOICEVOX 音声で喋る（応答遅め可）

## チェックリスト
- [x] B1: 足回り確認 (2026-06-10) — gateway に `tts` + `stt-faster-whisper` extras 導入。VOICEVOX は旧 yuno 残骸 `~/trash/` 行きだったエンジン本体 (2.1GB) を `~/apps/voicevox/` へ救出し、unit を drop-in (`voicevox.service.d/override.conf`) でパス修正して復旧 (v0.25.2, :50021)
- [x] B2: TTS 単体 (2026-06-10) — `say` → VOICEVOX → Opus 91 frames 実機送信成功（初回 8.6s、VOICEVOX ウォームアップ込み）。**スピーカーからの実音確認はユーザー帰宅後**
- [x] B3: STT 単体 (2026-06-10 実機確認) — 画面タップ → 録音 → faster-whisper 転写動作確認。※転写品質に課題: 30s 録音中の発話が 'ん'/'うっ' としか転写されないターンあり（録音窓が長すぎる影響の可能性、タップ停止運用で再評価）
- [x] B4: Hermes APIServerAdapter 有効化 (2026-06-10) — drop-in (`hermes-gateway.service.d/api-server.conf`) で `API_SERVER_ENABLED=true` → 127.0.0.1:8642 で /health OK、実モデルと 1 ターン疎通成功
- [x] B5: voice-turn receiver 実装 (2026-06-10) — `stackchan_mcp/hermes_bridge.py` 新規 + `capture_server.py` に `/voice_turn` ルート 1 箇所追加（fork 独自・upstream 非送付）。`STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8766/voice_turn` で自分自身に向ける構成
- [x] B6 (シミュレート版): E2E 成功 (2026-06-10) — VOICEVOX 合成音声を firmware と同形式の Ogg/Opus で `/voice_turn` に POST（`scratch/test_voice_turn.py`）→ STT「好きな食べ物はある?」→ Hermes「ラーメンかな」→ TTS 164 frames 実機送信。**ウォーム時 18.3s（STT 0.7s / Hermes 3.1s / TTS 14.6s ※音声 9.8s のリアルタイム送出込み、合成自体 ~5s）。発話終了→声出し ~8.6s**
- [x] B6 (実機版): タップ→会話成立をユーザー確認 (2026-06-10 夜) ✅ **Phase B 完了条件達成**。実測 timings: total 20.1s / 13.7s（録音 30s タイムアウト分は除く）
- [x] **レイテンシ分析 (2026-06-10)** — 体感 10〜30s の主因は**録音が毎回 30s タイムアウトまで継続**（498 frames）していたこと。タップ停止は firmware 実装済み（`stackchan.cc:2330` listening 中の短タップ → StopListening）だが運用されていなかった。処理自体は STT 1.4〜2.3s + Hermes 2.6〜3.4s + VOICEVOX 合成 2〜4s = 6〜9s。上限定数: firmware `stackchan.cc:2251 LISTEN_TIMEOUT_MS=30000` / gateway `stt/orchestrator.py:63 MAX_DURATION_MS=30000`。AFE VAD は稼働中だが自動停止には未結線（LED 表示のみ、`application.cc:232`）→ 短縮策は Phase C 候補
- [x] B7: Hermes→gateway MCP 接続 (2026-06-10、**方式(b) 常駐+HTTP MCP で稼働確認済み**) — Streamable HTTP サーバーは upstream 実装済み (`stackchan-mcp serve --transport streamable-http`、:8767) で追加コード不要。`stackchan-gateway.service` 稼働中（`docs/deploy/` に unit、enable 済み）、`~/.hermes/config.yaml` に mcp_servers.stackchan 登録（バックアップ: config.yaml.bak-20260610）。**Hermes が say ツールを MCP 経由で呼び出し、結果を報告するところまで確認済み**（ESP32 未接続のため発話自体は未達）
- [x] B8 (API_SERVER_KEY): 生成・適用済み — gateway 側 `~/.yorishiro/secrets.env` (HERMES_API_KEY)、Hermes 側 drop-in。キー認証 + `X-Hermes-Session-Id: stackchan-voice` でセッション継続通信を確認
- [x] B8 残り (STACKCHAN_TOKEN): 完了 (2026-06-11 朝) — firmware 側 `sdkconfig.defaults.local` の `CONFIG_DEFAULT_WEBSOCKET_TOKEN`（NVS 空なので fallback が効く、FORCE 不要）+ gateway 側 `~/.yorishiro/secrets.env` の `STACKCHAN_TOKEN`。リビルド・flash・gateway 再起動後、実機は認証付き接続成功、誤トークンは **HTTP 401 で拒否**を確認。**Phase B 全項目クローズ** 🎉。feature/phase-b-voice は origin に push 済み
- [x] **ESP32 オフラインの原因特定** (2026-06-10) — firmware の `PowerSaveTimer(-1, 60, 300)` が「WS 切断のまま 5 分」で AXP2101 PowerOff を発動（USB 給電でも切れる）。gateway 入れ替え時の切断 >5 分で発動した。タッチでは復帰不可、**電源ボタン（長押し）で起動**
- [x] **firmware 修正: 自動電源オフ無効化** (2026-06-10, コミット 8490088) — `boards/stackchan/stackchan.cc` を `PowerSaveTimer(-1, 60, -1)` に変更（画面減光は維持、shutdown のみ無効）。**ビルド成功済み**（`build/xiaozhi.bin` 13:47、v2.2.6、mDNS 設定マージ確認済み）
- [x] **実機 flash 完了** (2026-06-10 22:04) — app のみ書き込み（`Hash of data verified.`）、リブート後 gateway へ自動再接続確認（tools=30）。直後に MCP HTTP (:8767) 経由で `say` 実行 → 83 frames 送信成功（B2 実機版、**実音はユーザー確認待ち**）

## 次セッション再開手順（2026-06-10 clear 時点）

1. **実機復帰**: ユーザーが電源ボタン（長押し）で起動 → 自動で gateway へ接続（~13 秒）。`journalctl -u stackchan-gateway -f` で確認
2. **flash（ユーザーが USB 接続したら、電源オフ無効化の根治に必要）**: app のみで WiFi 設定は保持される:
   `~/.venvs/esptool/bin/esptool --port /dev/ttyACM0 --baud 460800 write-flash 0x20000 firmware/build/xiaozhi.bin`
   ※シリアルポートを開くだけでリセットされる点に注意（todo の Phase B-0 学び参照）
3. **実機確認（B2/B3/B6 実機版）**: ①`say` で音出し（Hermes API 経由か、service を止めて mcp_repl.py）②画面タップ→話す→タップ→返事 ③Discord で Hermes に「StackChan で喋って」
4. その後: STACKCHAN_TOKEN 設定（B8 残り）、Phase B クローズ → worklog 更新・learning-report 提案

### 現在の常駐構成（全部 systemd、自動起動）
- `voicevox.service` (:50021) / `stackchan-gateway.service` (:8765 WS, :8766 capture+voice_turn, :8767 MCP HTTP) / `hermes-gateway.service` (Discord + :8642 API)
- Hermes→gateway は MCP 登録済み（`~/.hermes/config.yaml` mcp_servers.stackchan）。秘匿値は `~/.yorishiro/secrets.env`
- 開発時に gateway を手で動かす場合: `sudo systemctl stop stackchan-gateway` してから `scratch/mcp_repl.py`
- [x] 追加: mDNS 広告アドレス固定 `STACKCHAN_MDNS_ADVERTISE_ADDR` 実装 (23ef800) — ESP32 接続 ~50s → **13s**
- [x] 追加: 学習用 worklog 開始 — `docs/worklog/2026-06-10-phase-b-voice.md`（毎セッション継続、memory 登録済み）

## Phase B での学び・メモ
- Hermes API はステートレス (`/v1/chat/completions`) なら認証不要だが、**セッション継続 (`X-Hermes-Session-Id`) には `API_SERVER_KEY` 設定が必須**（B8 で対応）
- VOICEVOX 初回リクエストはウォームアップで +数秒。`voicevox.service` は ExecStartPost で /version 待ちするので起動完了 = 即応答可
- faster-whisper 初回 transcribe はモデルロードで ~19s、以降 ~0.7s。gateway 起動時のプリロードは Phase C の最適化候補
- TTS の所要時間は Opus フレームのリアルタイム送出（60ms/frame）が支配的。体感短縮には文分割ストリーミングが Phase C 候補
- 検証ツール: `scratch/mcp_repl.py`（gateway 常駐 + コマンドファイル経由でツール実行）、`scratch/test_voice_turn.py`（実機タップ不要の E2E）

## 着手前確認（2026-06-10 ユーザー回答済み）
1. STT エンジン: **faster-whisper**（gateway 内蔵、CPU int8 で VRAM 温存。遅ければ whisper.cpp に切替可）
2. Hermes のプロファイル: **今のまま（default）** — 起動コマンド変更なし
3. B4 の hermes-gateway.service 変更: **承認済み**（127.0.0.1:8642 bind のみ、environment 1 行）

---

# Phase B-0 — 実機疎通: WiFi 投入 + gateway 起動 + set_avatar 経路確認

着手日: 2026-06-10

## ゴール
配網モード待機中の CoreS3 を自宅 WiFi に接続し、razer-server 上の gateway と WebSocket 疎通させ、MCP 経由で `set_avatar` コマンドが通ることを確認する。

## 前提（調査済み・根拠は firmware/gateway 内コード）
- WiFi 投入は配網 AP (Xiaozhi-E79D) → `http://192.168.4.1` の web UI から（captive portal 方式）
- 現ビルドは **mDNS 無効**のため、web UI の Advanced タブで gateway URL `ws://192.168.0.19:8765/` の**手動入力が必須**
- トークンは firmware 側空 / gateway 側 `.env` 未作成 = 認証なしで整合 → 疎通後に両側へ設定（任意）
- **avatar 画像は placeholder（1×1 黒点）**: `set_avatar` は通るが顔は表示されない。顔表示は B0-7 で別途対応

## チェックリスト
- [x] B0-1: gateway 依存インストール — `uv sync` 完了 (stackchan-mcp v0.10.0)
- [x] B0-2: razer-server のポート開放確認 — ufw は **inactive**（ファイアウォール無効）と確認、ブロック要因なし (2026-06-10)
- [x] B0-3: gateway 起動（認証なしモード、`VISION_HOST=192.168.0.19` 付き）+ 接続検知で自動 set_avatar する probe (`scratch/mcp_probe.py`) 稼働中
- [x] B0-4: WiFi 投入完了（SSID: eoRT-1127969-g、device IP: 192.168.0.10）。ただし Advanced タブの gateway URL は未入力 → `WS_URL not configured` で接続先不明に
- [x] B0-4b: **方針転換（ユーザー承認済み）**: `sdkconfig.defaults.local`（gitignore 済み）に `CONFIG_STACKCHAN_MDNS_DISCOVERY=y` + `CONFIG_DEFAULT_WEBSOCKET_URL="ws://192.168.0.19:8765/"` を設定して再ビルド → **app パーティションのみ書き込み（NVS=WiFi 設定は保持）**
  - mDNS が無効だった原因: set-target 時点ではボード未選択 → default n で確定し、後段のボード append では反映されない
  - sdkconfig 手編集は release.py の set-target で再生成されるため不可。sdkconfig.defaults.local が正規の上書き手段
- [x] B0-5: 再書き込み後、ESP32 が mDNS で gateway を自動発見し WebSocket 接続成功（device_id: 44:1b:f6:e1:e7:9c、tools_count: 30）
- [x] B0-6: `set_avatar idle` → `{"face":"idle","ok":true}` 成功 (2026-06-10)。**Phase B-0 のゴール達成**
- [x] B0-7: avatar アセット — **経路(b) ビルド焼き込みで実装まで完了** (2026-06-10)
  - 既製の顔 PNG はどこにも配布されていない（m5stack-avatar はコード描画ライブラリで画像なし）
  - `scratch/gen_avatar_faces.py`（PIL）で公式風の顔 14 枚を生成 → `~/.stackchan/avatar/` → `convert_avatars.py` → クリーンリビルド → app flash
  - **実機 LCD に顔表示をユーザー確認済み** ✅。顔の調整は gen_avatar_faces.py の数値変更 → 再生成 → リビルドで何度でも可能
  - 経路(a) `load_avatar_set`（PSRAM 揮発・ホットスワップ用）は Phase B 後半の表情差し替えで活用予定

## Phase B-0 での学び・メモ
- **USB-Serial/JTAG はポートを開くだけでデバイスがリセットされる**（`rst:0x15 USB_UART_CHIP_RESET`）。serial_watch.py で dtr/rts=False にしていても発生。WS 接続検証中はシリアルに触らないこと
- **mDNS 候補順の無駄**: gateway が Tailscale IP / docker bridge IP も広告するため、ESP32 は到達不能な候補で各 ~18 秒タイムアウトしてから LAN IP に到達する（起動〜接続まで ~50 秒）。改善候補: gateway の mdns_advertiser に広告アドレスのフィルタを入れる（upstream 改修 or local patch、Phase B で検討）
- sdkconfig は gitignore 済み。ローカル上書きは `sdkconfig.defaults.local`（同じく gitignore 済み）が正規手段で、release.py が set-target 後にマージしてくれる
- app のみ flash（`esptool write-flash 0x20000 build/xiaozhi.bin`）で NVS の WiFi 設定は保持される。確認済み
- gateway + 疎通プローブは `scratch/mcp_probe.py`（gateway を stdio MCP 子プロセスとして起動し、接続検知で set_avatar を自動実行）

## Out of scope（Phase B 本体）
- Opus 音声ストリーム、whisper.cpp / VOICEVOX 連携、Hermes 通信プロトコル（設計判断 1・3）
- STACKCHAN_TOKEN の本設定（疎通確認後に実施）

---

# Phase0 — Hermes Agent 身体化 / ESP-IDF 環境構築〜実機書き込み

Plan: `/home/kenji/.claude/plans/phase0-snappy-haven.md`

## ゴール
razer-server で Docker ベース ESP-IDF v5.5.2 ビルド環境を整え、M5Stack CoreS3 へ本 fork firmware を書き込み、「触ると首が動く」状態まで持っていく。

## 完了条件 (Phase A 完了条件相当) — **2026-06-10 全達成 ✅**
- [x] `docker run espressif/idf:v5.5.2 idf.py --version` 成功
- [x] `python scripts/release.py stackchan` 成功 (`build/merged-binary.bin` 生成)
- [x] CoreS3 へ flash 成功 (`Hash of data verified.`)
- [x] Boot ログで `Servo power ENABLED via PY32 pin 0` 確認
- [x] **首動き目視**: boot-init で pitch 0→45°（ユーザー確認済み。ログでも ReadPos 626→759 の物理移動を裏付け）
- [x] **背面なで反応**: ユーザー確認済み。シリアルに `touch event: STROKE ... duration=1699 ms` 記録

avatar の LCD 表示、画面タップ反応、WS 接続、gateway 起動は **Phase B 以降**。

## チェックリスト

### 環境構築
- [x] T1: Docker engine — 既に install 済み (v29.5.2, 2026-05-25 から稼働)、kenji は docker グループ所属
- [x] T2: esptool/pyserial venv — `~/.venvs/esptool` に esptool v5.3.0 + pyserial 3.5
- [x] T3: submodule (smooth_ui_toolkit v2.12.0) を init
- [x] T4: ESP-IDF v5.5.2 Docker image を pull
- [~] T5: avatar_images.local.cc 生成 — **スキップ**: PNG ソース不在 + Phase0 では avatar 表示しないので placeholder で OK

### ビルド・書き込み
- [x] T6: `python ./scripts/release.py stackchan` を Docker で実行 — `build/merged-binary.bin` (9.6M) 生成、error なし
- [x] T7: M5Stack CoreS3 を USB-C で接続、`/dev/ttyACM0` 出現確認 — USB-Serial/JTAG mode、MAC 44:1b:f6:e1:e7:9c
- [x] T8: 初回 flash — 460800bps で 39 秒、`Hash of data verified.`
- [x] T9: シリアル監視 — `Servo power ENABLED` / `Boot pre-init ReadPos` / `Si12T: init OK` / panic なし。WiFi 未設定のため配網モード (AP: Xiaozhi-E79D) で待機 = Phase0 では正常
- [x] T10: 首動き + 背面なで反応をユーザー目視確認 (2026-06-10)

## Phase0 での学び・メモ
- シリアルログ収集は `scratch/serial_bootlog.py` (リセット付き) / `scratch/serial_watch.py` (監視のみ) を使用
- USB-Serial/JTAG のリセット: DTR(IO0)=False のまま RTS(EN) をパルス。**DTR を True にすると DOWNLOAD モードに入る**ので注意
- 次フェーズ (Phase B) の前提: WiFi SSID/PASS 投入 + STACKCHAN_TOKEN 整合 + gateway 起動 → set_avatar で顔表示確認から
- upstream に v0.10.0 + firmware-v1.10.0 がリリース済み (2026-06-09)。差分は gateway mDNS advertiser 修正 + firmware mdns_gateway_discovery 改善 + Cloudflare Workers relay example。main は v0.9.1 のまま → 同期は要ユーザー判断

## 環境メモ (Phase0 着手時)
- razer-server (= 本機, Ubuntu Server 24.04, x86_64, GTX 1060 6GB)
- `groups`: `kenji adm dialout cdrom sudo dip plugdev lxd docker ollama`
- `python3 --version`: 3.12.3
- `docker --version`: 29.5.2
- ディスク残量: 76GB / 124GB

## Out of scope (Phase B 以降)
- gateway (`stackchan_mcp/`) の uv sync / 起動
- STACKCHAN_TOKEN の sdkconfig <-> .env 整合
- WiFi SSID/PASS の sdkconfig 投入、WebSocket 接続
- avatar の LCD 表示確認 (Issue #77)
- 画面 (FT6336) タップ反応確認
- yuno-chan-api / voice_server / 音声系 (Phase 4)
- Home Assistant 連携 (Phase C)
- LFM2.5 ローカル LLM 統合 (Phase D)
