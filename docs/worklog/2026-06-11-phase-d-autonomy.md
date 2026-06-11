# 作業記録: Phase D 自律性 — heartbeat / 検索ツール / ノートツール（2026-06-11）

> このファイルは「後から読んで、何をして・何が動いていて・どういう構成なのかを学べる」ことを目的とした記録です。
> 疑問が出たら、このファイルごと Claude や Gemini に貼り付けて「ここを詳しく」と聞ける粒度で書いています。

---

## 1. 今日のゴールと結果

**ゴール**: Phase D の 3 タスクを段階実施 — D1 heartbeat（無音の仕草）→ D2 Hermes MCP ツール追加（web 検索・ノート）→ D3 LFM2.5 役割拡大検討。

**事前のユーザー決定**:
- heartbeat は**段階導入**: 第1段階は非音声のみ（表情・首・LED）。発話は将来 opt-in で
- 検索は **Tavily 主**（無料 1,000 クレジット/月、クレカ不要）+ **ddgs（DuckDuckGo）フォールバック**
- Phase D 完了時に learning-report を作成する

**結果**:
- ✅ **D1**: `heartbeat.py` 実装 + 単体テスト。実機 E2E はユーザーの sudo 作業待ち（手順は §5）
- ✅ **D2**: `web_search.py`（Tavily + ddgs）と `notes.py`（write/read/list_note）を実装、MCP ツール 4 つ追加（計 35 ツール）。ddgs 実検索スモーク成功。Tavily はユーザーの API キー登録待ち
- ✅ **D3**: VRAM 競合は実質なし（GPU は LFM2.5 0.7GB のみ、whisper/VOICEVOX は CPU）。ルーティング閾値拡大はデータ不足（local 2件）で見送り。代わりに**コールドスタート問題を発見** — keep_alive=30m 切れで local の llm が 2.8〜3.4s（warm なら 0.5s）。`STACKCHAN_LOCAL_LLM_KEEP_ALIVE=24h` への変更を推奨（コード不要、drop-in 1 行）
- テスト: **567 件全パス**（うち新規 41 件: heartbeat / web_search / notes）

---

## 2. 何を作ったか（構成図）

```
                     razer-server (192.168.0.19)
 ┌──────────────────────────────────────────────────────────────────┐
 │ [Hermes Agent] hermes-gateway.service                            │
 │   ・MCP クライアント → gateway :8767                             │
 │       tools/call: say, switchbot_*,                              │
 │                   ★web_search ★write_note/read_note/list_notes  │
 │        ▼                                                         │
 │ [gateway] stackchan-gateway.service                              │
 │   ・★heartbeat.py — asyncio 周期タスク（無音の仕草）             │
 │   ・★web_search.py — Tavily API → 失敗/未設定時 ddgs             │
 │   ・★notes.py — ~/.stackchan/notes/ 限定のメモ読み書き           │
 │        │                                                         │
 │        │ WS :8765（heartbeat はここから set_avatar/move_head）   │
 │        ▼                                                         │
 │ [StackChan 実機 CoreS3]                                          │
 └──────────────────────────────────────────────────────────────────┘
```

### D1: heartbeat（`gateway/stackchan_mcp/heartbeat.py`）

「30分〜1時間に1回、StackChan が勝手に小さな仕草をする」機能。**完全 opt-in**（環境変数 `STACKCHAN_HEARTBEAT_INTERVAL_MIN` 未設定なら一切動かない）。

- **仕草は3種**: 見回し（thinking 顔で左右を見て戻る）/ 表情の変化（happy 等→idle）/ うなずき。どれも**無音**
- **発火ガード3つ**: ①ESP32 接続中のみ ②音声パイプライン使用中（TTS 再生・録音中）はスキップ ③クワイエットアワー（デフォルト 22:00-08:00）はスキップ
- **ジッター**: 間隔に ±25% の揺らぎを入れて機械的な定時動作を避ける
- **首は元の位置に戻す**: 仕草前に `get_head_angles` で現在角度を読み、終わったら復帰。読めなければ首は動かさない（安全側）
- 設計原則との整合: gateway 側に置いたので **Hermes は reactive のまま**(原則4)。無音なので**夫婦の会話に割り込まない**(原則1)

### D2a: web 検索（`gateway/stackchan_mcp/web_search.py`）

- `TAVILY_API_KEY` があれば Tavily（LLM 向け検索 API。結果に AI 要約 `answer` が付く）
- キー未設定・Tavily 障害時は **ddgs（DuckDuckGo、キー不要）に自動フォールバック**
- 依存: Tavily は aiohttp 直叩きで追加依存なし。ddgs は `pyproject.toml` の extra `search` に分離（`uv sync --extra search`）

### D2b: ノート（`gateway/stackchan_mcp/notes.py`）

- Hermes が「メモして」「買い物リストに追加」等に使う想定。`~/.stackchan/notes/` **配下限定**
- 安全策: パス区切り・`..`・先頭ドット拒否、resolve 後にディレクトリ内チェック、1ファイル 256KB 上限、拡張子は .md/.txt のみ
- `write_note`（append 対応）/ `read_note` / `list_notes` の3ツール

### MCP への組み込みパターン（Phase C の switchbot と同じ）

1. `stdio_server.py` の `_dispatch_mcp_tool()` に分岐追加（**ESP32 接続ガードより前** — これらは実機不要なので）
2. `stdio_server.py` の `list_tools()` に Tool 定義追加（description は Hermes が読む説明書）
3. `http_server.py` の `BYPASS_TOOLS` に追加（ESP32 用キューを通さない）

---

## 3. 用語解説

- **heartbeat**: エージェント分野で「定期的に自発動作を起こすトリガー」のこと。心拍のように一定間隔で打つ
- **ジッター (jitter)**: 意図的に入れるランダムな揺らぎ。毎時ちょうどに動くと機械っぽいので ±25% ずらす
- **Tavily**: LLM エージェント向けに設計された検索 API。普通の検索と違い、結果を LLM が読みやすい形（要約付き）で返す
- **ddgs**: DuckDuckGo 検索の非公式 Python ライブラリ。API キー不要だが、非公式ゆえ時々ブロックされる
- **パストラバーサル**: `../../etc/passwd` のような名前でサンドボックス外のファイルに触る攻撃。notes.py は resolve 後の親ディレクトリ検査で防ぐ
- **BYPASS_TOOLS**: gateway の HTTP MCP サーバーで「ESP32 への単一実行キューを通さない」ツール一覧。クラウド/ローカル処理だけのツールは実機の接続状態と無関係に動くべきなのでここに入れる

---

## 3.5 実機 E2E で見つかった問題と修正（2026-06-11 夜）

- **D1 heartbeat**: 1 分間隔テストで仕草 2 回発火をログ+目視確認 ✅ → 本運用(30分・quiet 22:00-08:00)へ差し替え済み
- **D2 音声 E2E は初回失敗**: 「買い物リストに牛乳をメモして」が STT で「**ハイモノリストに輸入を埋めまして**」に誤転写 → マーカー語が消えてローカル LLM に流れ、**ツールを呼べないのに「追加しました」と嘘の応答**(メモは作られていなかった)。応答が自然なので一見正常に見える、という危険な失敗モード
- **修正(コミット 441a95f)**: ①「メモ」「リスト」+ **依頼形マーカー(して/といて/ちょうだい/お願い)** を追加 — 依頼=行動=ツールが必要、なので依頼形は全部 Hermes へ。誤爆(「はじめまして」)は遅くなるだけで安全側 ②ローカル用プロンプトに「道具は使えない、できたふりをするな」を追記(1.2B には効きが弱いと実測したため①が本命)
- **教訓**: 音声系の E2E は「応答が自然だった」では合格にできない。**サーバーログ(transcript / route / 呼ばれたツール)と成果物(ファイル等)で裏取りする**
- keep_alive=24h の効果も実測で確認: local llm 2760ms(コールド)→ 698ms(ウォーム)

## 4. 学び・ハマりどころ

- `time.fromisoformat("22")` は Python 3.11+ では**有効**（22:00 扱い）。クワイエットアワーのバリデーションテストを書く際に「22-08 は不正」と思い込んでいたら通ってしまった
- このリポジトリの async テストは conftest の自動マーカーが `__wrapped__` 持ちにしか効かないため、**`@pytest.mark.asyncio` を明示**するのが流儀
- `aiohttp_unused_port` fixture は共通 conftest ではなく**各テストファイルにローカル定義**する流儀
- systemd の gateway は editable install（`.venv` が repo を直接参照）なので、**ブランチを切り替えて restart するだけで新コードが動く**

---

## 5. ユーザー作業（次回セッションまでに）

1. **実機 E2E（heartbeat 検証）** — sudo が必要なので手動で:
   ```bash
   # 1分間隔のテスト設定を適用
   sudo mkdir -p /etc/systemd/system/stackchan-gateway.service.d
   printf '[Service]\nEnvironment="STACKCHAN_HEARTBEAT_INTERVAL_MIN=1"\nEnvironment="STACKCHAN_HEARTBEAT_QUIET=off"\n' | sudo tee /etc/systemd/system/stackchan-gateway.service.d/heartbeat-test.conf
   sudo systemctl daemon-reload && sudo systemctl restart stackchan-gateway
   # 2〜3分観察（実機が仕草をする / ログに heartbeat: gesture が出る）
   journalctl -u stackchan-gateway -f | grep -i heartbeat
   # 確認できたら本運用値(30分)に差し替え
   sudo rm /etc/systemd/system/stackchan-gateway.service.d/heartbeat-test.conf
   sudo cp ~/dev/yorishiro-workspace/stackchan-mcp-yorishiro/docs/deploy/stackchan-gateway.service.d/heartbeat.conf /etc/systemd/system/stackchan-gateway.service.d/
   sudo systemctl daemon-reload && sudo systemctl restart stackchan-gateway
   ```
2. **Tavily 登録**（任意・推奨）: <https://tavily.com> にメール登録 → API キー取得 → `~/.yorishiro/secrets.env` に `TAVILY_API_KEY=tvly-...` を追記 → `sudo systemctl restart stackchan-gateway`。未登録でも ddgs で検索は動く
3. 再起動後、音声で「○○を調べて」「メモして」を試すと D2 の E2E になる

---

## 6. Phase D 最終ステップ — 経路分離による組み込みツール抑制（2026-06-11 22:00〜23:10）

### 6.1 症状（再掲）

D1 heartbeat と D2 検索・メモツールは投入済み。だが Hermes (gpt-5.5/openai-codex) が gateway の MCP ツール (`mcp_stackchan_write_note` / `mcp_stackchan_web_search`) を呼ばず、組み込み `terminal` / `browser_*` / `read_file` / `write_file` で代用したり、過去履歴を根拠に嘘応答する症状が続いた。

例（2026-06-11 21:45 ターン）:

- メモ依頼 → `read_file` 1 回のみ → 「すでに入っています」と回答（実際は `~/.stackchan/notes/` ディレクトリ未作成）
- 天気依頼 → `terminal` → `browser_navigate` → `browser_console` 経由（`web_search` 不使用）

コミット `0f997d2`（gateway 側 `HERMES_VOICE_TOOLS_LINE` のシステムプロンプト誘導）では効果不十分と判明。**「terminal は使うな」「web_search を使え」と書いても、組み込みツールが見えている限り gpt-5.5 はそちらを優先する。プロンプト誘導 < 構造的制約**。

### 6.2 採用案と根拠（案 B: `platform_toolsets.api_server`）

Hermes 側を調査して 3 案を比較:

- **案 A（リクエスト body 上書き）**: `api_server.py` は body の `disabled_toolsets` を読まない（grep 0 件）→ **実装不可、棄却**
- **案 B（`platform_toolsets.api_server` で経路分離）**: `~/.hermes/config.yaml` 1 箇所追記。Hermes 本体は無改造、Discord 経路は無影響、MCP server `stackchan` は `include_default_mcp_servers=True` で自動付与 → **採用**
- **案 C（`HERMES_HOME` プロファイル分離）**: 別 systemd unit でプロセス分離。Hermes 無改造だが運用が重く、Discord と人格分裂 → **将来課題に凍結**（外部クライアントから `/v1/chat/completions` で terminal が必要になった時点で再検討）

動作保証:

- `~/.hermes/hermes-agent/gateway/platforms/api_server.py:989` で `_get_platform_tools(user_config, "api_server")` 呼び出し
- 公式テスト `tests/gateway/test_api_server_toolset.py:101-129` が `{"platform_toolsets": {"api_server": ["web", "terminal"]}}` → enabled が `[terminal, web]` だけになることを保証

### 6.3 実装（config.yaml 1 ブロック追記）

```yaml
# ~/.hermes/config.yaml の platform_toolsets: ブロック末尾に追記
api_server:
- web        # web_search / web_extract
- vision     # 写真の意味解釈
- todo       # 頭の整理
- memory     # Hermes 内部メモリ (write_note とは別)
- messaging  # send_message
- clarify    # 聞き返し
```

意図的に外したコア toolset: `terminal`, `file`, `browser`, `code_execution`, `delegation`, `search_files`, `cronjob`, `tts`（gateway で VOICEVOX を直接叩くので冗長）。

事前検証（Hermes venv で `_get_platform_tools` を直接呼ぶ）:

```bash
cd /home/kenji/.hermes/hermes-agent && HERMES_HOME=/home/kenji/.hermes ./venv/bin/python -c "
from hermes_cli.tools_config import _get_platform_tools
import yaml
cfg = yaml.safe_load(open('/home/kenji/.hermes/config.yaml'))
print('api_server :', sorted(_get_platform_tools(cfg, 'api_server')))
print('discord    :', sorted(_get_platform_tools(cfg, 'discord')))
"
```

結果:

- `api_server`: `['clarify', 'memory', 'messaging', 'stackchan', 'todo', 'vision', 'web']`（7 個、`stackchan` は MCP 自動付与）
- `discord`: `terminal` / `file` / `browser` を含む 19 個のまま（**無回帰**）

`sudo systemctl restart hermes-gateway` で適用。gateway 側 `hermes_bridge.py` は無編集（当初想定していた `X-Hermes-Platform` ヘッダー追加は Hermes 側に受け口がないため不要）。

バックアップ: `/home/kenji/.hermes/config.yaml.bak-20260611-phaseD`

### 6.4 E2E 結果（22:17 再起動後）

| シナリオ | 結果 | 根拠 |
|---|---|---|
| MCP server 自動付与 | ✅ | 22:17:56 で 35 ツール登録 (`mcp_stackchan_write_note` / `mcp_stackchan_web_search` / `mcp_stackchan_switchbot_*` 含む) |
| 家電 (`switchbot_*`) | ✅ | 23:07:21 `tool mcp_stackchan_switchbot_send_command completed`、reply='リビングの電気を消しました。'、実機消灯 |
| Discord 経路の `terminal` | ✅ | 23:07:06 `tool terminal completed` (platform=discord)、**無回帰** |
| 組み込みツール抑制 | ✅ | 再起動後の `[stackchan-voice]` セッションで `terminal` / `browser_*` / `write_file` / `read_file` の CallToolRequest **0 件** |
| `clarify` toolset の効果 | ✅ | 検索ターンで Hermes が「天気か勉強か聞き返し」を選択 = 強引な `terminal` 利用に走らなくなった |
| メモ単体ログ | ⚠️ | STT 起因で transcript 未取得（ユーザーが声を出しにくい時間帯、後日再検証） |
| 検索単体ログ | ⚠️ | 同上（transcript = `'東京の京の勉強して'` で意図不明 → clarify に流れた） |

**構造的効果は確定**（組み込みツールへの偏りは消えた）。メモ・検索の単体ログは STT 安定時に撮り直すフォローアップとして残す。

### 6.5 学び

- **プロンプト誘導 < 構造的制約**: 21:45 までは「terminal を使うな」とシステムプロンプトに明示注入していたが、gpt-5.5 はそれを無視してでも組み込みツールに流れた。`platform_toolsets` で目の前から消した瞬間、迷いなく MCP ツールに流れるようになった。LLM に「使うな」と言うより、見せないほうが速い
- **MCP server の自動付与**: `include_default_mcp_servers=True` (`tools_config.py:1325-1329`) のおかげで、`platform_toolsets.api_server` を絞っても `mcp_servers.stackchan` は自動的に enabled に残る。「ツールセット = コア機能の取捨選択」「MCP = 外付け機能」が独立軸として扱われている設計
- **副作用範囲を把握しておく**: `platform_toolsets.api_server` は **Hermes API（port 8642）経路全体**を絞る。今は声経路だけが API を叩いているので問題ないが、将来 Claude Code 等 別クライアントから `/v1/chat/completions` を叩く要件が出たらその時点で案 C（プロファイル分離）に切り替える、と覚悟しておく
- **音声系の E2E 判定は STT 安定が前提**: 5.x で書いたのと同じ罠で、今回もメモ・検索の transcript が崩壊してログ判定が完結しなかった。「応答が自然だった」より「ツールが呼ばれた」を、しかも「狙ったツール名」で見るのが本筋

### 6.6 フォローアップ（声を出しやすい時間帯に）

- [ ] メモ単体 E2E: 「○○のことメモして」発話 → `~/.stackchan/notes/*.md` 生成確認、agent.log で `mcp_stackchan_write_note` の CallToolRequest 確認
- [ ] 検索単体 E2E: 「○○のニュース調べて」発話 → agent.log で `mcp_stackchan_web_search` の CallToolRequest 確認
- どちらも失敗時は `~/.hermes/logs/agent.log` の該当ターンを transcript 単位で diff し、Hermes 側のツール選択を可視化する
