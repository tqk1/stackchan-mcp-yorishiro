# 2026-06-17 マルチターン会話 Phase 1（MVP）実装

ブランチ: `feature/multiturn`（develop から分岐）
プラン: `~/.claude/plans/codex-fluffy-breeze.md`（ユーザー承認済み）

## このセッションでやったこと（概要）

「Hermes が質問で会話を締めたら、TTS 発話のあと自動でもう一度聞き取りに入る」マルチターン会話を、**gateway 側だけ**で実装（firmware 改修なし）。Phase 1 = MVP（継続ループ＋安全弁＋テスト）。実機の自己拾音ゲート実験はユーザーステップとして残置。

調査は **Explore×3 → Plan エージェント → 独立 Claude 赤チーム** の多段で実施（Codex CLI はこの Linux 機に未インストールのため、第二意見は独立 Claude の赤チームで代替）。

## 調査で覆った重要事実

`project_multiturn_next` メモにあった保留理由「録音停止機構が不明確／firmware に VAD 自停止コード無し」は **誤りだった**。実機稼働中の firmware には Phase C0 の VAD 無音自動停止が存在する:

- `firmware/main/boards/stackchan/stackchan.cc` `PollTouchpad()`（2485-2637行）
- warmup 800ms → `IsVoiceDetected()` で発話検知 → 発話後 1200ms 無音で `StopListening()`。発話ゼロなら 30s タイムアウト。
- VAD 状態（`listening_started_ms`/`speech_seen`/`last_voice_ms`）は `kDeviceStateListening` への **遷移エッジ**（2509-2517行）で毎回リセット。タップ/ウェイクワード/**gateway 主導の `listen:start`** のいずれで遷移しても同じ。

→ 録音停止は device 主導で明確に定義済み。gateway が `send_listen_state("start","manual")` を再送すれば、firmware VAD がまた自動で止め、次の `/voice_turn` POST が走る＝継続ループが gateway だけで成立。

## アーキテクチャ判断（二段方式）

1. **MVP = gateway 側 sleep**（今回・firmware 改修なし・flash 不要）
2. **保険 = firmware continuation モード**（Phase 4・条件付き）。MVP の実機検証で自己拾音/ポップ音/テンポ崩れが許容不可なら着手。

理由: gateway-only は速いが **AEC がデフォルト OFF**（`firmware/main/application.h:141` `kAecOff`）ゆえ自己拾音の構造的リスクがある。安く出して実機で測り、データで firmware 改修要否を決める。

## 赤チーム指摘と対策（実装に織り込み済み）

| # | 指摘 | 対策 |
|---|---|---|
| P0 | TTS 完了通知がプロトコルに無い／再生キューに残音／AEC off で自己ループしうる | 継続前に **固定 guard sleep**（`MULTITURN_TTS_GUARD_MS`）。実機で自己拾音を確認（ゲート実験） |
| P1 | `voice_turn_active` がターン境界 finally で False に戻る窓で heartbeat 割り込み（原則①違反の再発経路） | 会話セッション全体を覆う別フラグ `multiturn_active`＋`heartbeat._skip_reason` にゲート。stale で自己失効 |
| P1 | 独立 HTTP POST 間のカウンタ競合 | カウンタは成功パスで確定／`send_listen_state` を try/except ConnectionError／入口で stale-gap リセット |
| P2 | Hermes 文脈は `X-Hermes-Session-Id`＝API key 依存・固定 id 日跨ぎ蓄積 | **Phase 2** で対応（今回は未着手） |
| ✅ | firmware VAD リセットは gateway 主導でも効く | 主張は正しいと裏取り → firmware 改修不要 |

### guard 計算の補正（プランからの変更点）

プランは当初 `sleep(duration_ms + マージン)` としていたが、`tts/orchestrator.py:344-364` を読むと **フレームは wall-clock で 1 フレーム/20ms（再生レート）ペーシング送出**。つまり `synthesize_and_send` が返る時点で経過時間 ≈ `duration_ms`、firmware 側の残音は decode キュー depth（最大 ~40 フレーム ≈ 0.8s）だけ。
→ guard は **固定の queue-drain 遅延**だけでよく、`duration_ms` に依存させない。既定 **1000ms**（自己拾音が P0・AEC off なので初回は安全側、実測で下げる）。

## 実装した変更（ファイル別）

- **新規 `gateway/stackchan_mcp/multiturn.py`**: 純ロジック＋小状態。
  - `MultiturnSession`（`turn_count`/`last_activity`、`reset`/`note_continuation`/`is_gap_stale`）
  - `should_continue(*, enabled, route, reply, turn_count, max_turns, device_connected, muted, recording)` 純関数（継続ポリシーの単一の真実）
  - `reply_invites_continuation(reply)`（末尾 ？/? 判定）、env getters（`is_enabled`/`max_turns`/`session_timeout_s`/`tts_guard_ms`）
- **`gateway.py`**: `Gateway.__init__` に `self.multiturn = MultiturnSession()` / `self.multiturn_active = False`。
- **`hermes_bridge.py`**:
  - `handle_voice_turn` 入口: `multiturn_active=False`＋stale-gap なら counter リセット。
  - finally: `multiturn_active` が True（＝継続発火済み）なら表示クリア/idle LED 復帰をスキップ（on_listen_started が listening 表示を持つ）。
  - 空 transcript 早期 return で `multiturn.reset()`（沈黙＝会話終了）。
  - 成功パス末尾に `_maybe_continue()`: is_enabled 先行ゲート → `should_continue` → 発火時は `note_continuation`＋`multiturn_active=True`＋guard sleep＋`send_listen_state("start","manual")`（ConnectionError は握って reset）。応答 JSON に `multiturn` フラグ追加。
- **`heartbeat.py`**: `_skip_reason` に `_multiturn_suppresses()` ゲート（`multiturn_active` かつ gap が stale でない間だけ抑止＝答えが来なくても session timeout で自己失効）。
- **`control.py`**: `is_muted()` 薄アクセサ追加（`routing_force_hermes` ミラー、テストで差し替え可能・実状態ファイル依存回避）。

## テスト

- 新規 `tests/test_multiturn.py`: `reply_invites_continuation`／env getters／`MultiturnSession`／`should_continue` 条件マトリクス。
- `tests/test_hermes_bridge.py`: 継続発火・既定off・local除外・非質問・天井・mute・未接続・空transcriptリセット・stale-gap入口リセット・継続時の表示クリアskip。`_StubESP32` に `send_listen_state` スパイ追加。
- `tests/test_heartbeat.py`: multiturn gap 抑止＋stale 自己失効＋フラグ解除。
- 結果: **943 passed**（develop 899 +44）、**ruff clean**。

## 実機テスト手順（次にユーザーがやる＝P0 自己拾音ゲート）

1. gateway を新コードで起動（`STACKCHAN_MULTITURN=1` を環境に。必要なら `sudo systemctl stop stackchan-gateway` → source から起動、または editable インストール反映後 restart）。
2. タップ会話で Hermes に「？」で終わる応答をさせる（例: こちらが質問を促す）。
3. 観察ポイント:
   - 発話のあと自動で「きいてるよ」に戻り聞き取り再開するか。
   - **スタックちゃんが自分の声を拾って勝手にループしないか**（最重要・AEC off）。ループするなら `MULTITURN_TTS_GUARD_MS` を上げる or Phase 4（firmware continuation）へ。
   - TTS 末尾が切れないか。上限（既定4）で止まるか。黙れば（空transcript）止まるか。
4. guard を実測で詰める（自己拾音が出ない最小値）。

## 残課題

- 実機 自己拾音ゲート実験（上記）。
- commit（E2E 後 or ユーザー指示で）。
- Phase 2 文脈保持 / Phase 3 ダッシュボードトグル+UX / Phase 4 firmware continuation（条件付き）/ 全完了後 learning-report。
