# Phase D 振り返りレポート — StackChan が「ひとりで動き、ちゃんと道具を使う」まで

> **このレポートの使い方**: Phase D（自律性）でやったことを、
> 後から読み返して自分の理解に変えるための学習用ドキュメントです。
> 分からない箇所が出たら、このファイルごと Claude や Gemini に貼り付けて
> 「ここを詳しく」と聞いてください（章7にコピペで使える質問例があります）。
>
> 対象期間: 2026-06-11（コミット `d843616` 〜 経路分離適用）
> 作業ログ（時系列の生記録）: `docs/worklog/2026-06-11-phase-d-autonomy.md`
> 前のフェーズ: `docs/phase-c-report.md`（速く答える + 家電操作）

---

## 1. 全体像

### 1.1 何を作ったか（一言で）

Phase C で「**速く答える・家電を操る**」ようになった StackChan を、Phase D では
**「自分から少し動く（heartbeat）・道具をちゃんと使う（検索/メモ/家電を MCP 経由で）」** に進化させました。

達成した体験:

```
（ご主人がパソコンに向かっている間）
StackChan:（30 分に 1 回）ふと見回す・ちょっと笑う・うなずく   ← D1 heartbeat

あなた:「リビングの電気を消して」
StackChan:（実機消灯）「リビングの電気を消しました。」          ← D2+D4 経路分離後の MCP 経由

（こちらは検証は STT 安定時の宿題）
あなた:「明日 8 時にゴミ出しがあるってメモして」
StackChan:（~/.stackchan/notes/2026-06-12.md に書く）           ← D2 + D4
あなた:「今日の○○のニュースを調べて」
StackChan:（Tavily か ddgs で検索 → 要約）                     ← D2 + D4
```

そして、Phase D で **一番大きな学び** はソフトウェアの話ではなく **人間と LLM の話** でした。
「terminal を使うな、メモは write_note を使え」と Hermes（中身は gpt-5.5）に
**お願いしても効かない**。お願いではなく **見せない（構造で消す）** ようにしたら、迷わず狙ったツールに流れるようになった。
これは Phase D の章 3.4「事件簿: 嘘の完了報告」と章 4.1 で詳しく扱います。

### 1.2 システム構成図（Phase D 後）

Phase C との違いは ★ 印の 4 つです。

```
                  razer-server (192.168.0.19)
┌──────────────────────────────────────────────────────────┐
│ [Hermes Agent] hermes-gateway.service                     │
│   ・Discord + API サーバー :8642                          │
│   ・★platform_toolsets で API 経路だけツール制限 (D4)     │
│        api_server: web/vision/todo/memory/messaging/clarify
│        + mcp_servers.stackchan 自動付与（35 ツール）       │
│   ・MCP クライアント → gateway :8767                      │
│        │                                                  │
│        ▼                                                  │
│ [gateway] stackchan-gateway.service                       │
│   ・★heartbeat.py — 周期的な無音仕草 (D1)                 │
│   ・★web_search.py — Tavily + ddgs フォールバック (D2)    │
│   ・★notes.py — ~/.stackchan/notes/ 限定 RW (D2)          │
│   ・local_llm.py — 依頼形マーカー追加・keep_alive 24h (D3) │
└──────────────────────────────────────────────────────────┘
```

### 1.3 「会話 1 ターン」と「自律 1 仕草」

会話ターン（Phase C のルーティングに、D4 の経路分離が乗った形）:

```
画面タップ → 録音 → STT
  → decide_route(文字)
      ├─ 短い挨拶・雑談（マーカー語なし、30字以下）→ ローカル LFM2.5（~0.5秒）
      └─ 家電/調べ物/メモ/長文                    → Hermes
                                                    ↑ ★D4 で組み込みツール抑制
                                                    　 platform=api_server 経路は
                                                    　 terminal/browser/file を見せない
  → VOICEVOX → スピーカー
```

自律仕草（D1）:

```
（gateway 起動と同時に asyncio タスクが回る）
  毎ティック（30 分間隔 ± 25% ジッター）:
    if 接続中 and 音声非稼働 and not クワイエットアワー(22:00-08:00):
      仕草を 1 つ無音で実行（見回し / 表情 / うなずきからランダム）
    else:
      次ティックまで待つ（夫婦の会話に絶対割り込まない）
```

---

## 2. 採用した技術の解説

それぞれ「何か → なぜ必要か → 喩え → 使った場面」の順で説明します。

### 2.1 asyncio 周期タスク — 「常駐ロボットの呼吸」

**何か**: Python の非同期実行ループの中で、一定時間ごとに関数を回す仕組み。

**なぜ必要か**: heartbeat（自律仕草）は「30 分に 1 回」程度の超低頻度で OK。専用スレッドを立てるとリソースが無駄で、systemd cron で叩くと既存 gateway プロセスとの接続情報を共有できない。**gateway プロセス内に asyncio タスクとして同居させる** のが最短だった。

**喩え**: ロボットの心臓。普段は心拍が一定で目立たないが、心拍が止まると死んでいる。逆に心拍が速すぎると（heartbeat 間隔が短いと）落ち着きがない人になる。

**使った場面**: `gateway/stackchan_mcp/heartbeat.py`。`Gateway.start()` で `asyncio.create_task()` し、`stop()` でキャンセル。

```python
# heartbeat.py（要点のみ）
async def _heartbeat_loop(gateway, interval_min, jitter):
    while True:
        # 抑制条件チェック → 通過したら _perform_gesture()
        await asyncio.sleep(_next_interval_sec(interval_min, jitter))
```

### 2.2 Tavily + ddgs フォールバック — 「無料枠と保険」

**何か**: **Tavily** は LLM 向けに整形された検索 API（無料 1,000 クレジット/月、クレカ不要）。**ddgs** は DuckDuckGo のスクレイピング系 Python ライブラリ。

**なぜ必要か**: Tavily の方が品質が高いが従量課金になる将来も視野に入れたい。逆に ddgs は無料だが時々ブロックされる。Tavily を主・ddgs を保険にすると、無料枠の安心感と運用の堅牢さが両立する。

**喩え**: 主治医（Tavily）と街のかかりつけ医（ddgs）。普段は主治医を頼り、休日や満員のときは街医者で間に合わせる。

**使った場面**: `gateway/stackchan_mcp/web_search.py`。`TAVILY_API_KEY` 未設定 or 障害時は ddgs に自動切替。

### 2.3 `include_default_mcp_servers=True` — 「MCP は別の入口だから自動で繋がる」

**何か**: Hermes の `platform_toolsets` を絞っても、`mcp_servers` 配下に登録した外部 MCP サーバーは別軸として自動付与される、というデフォルト挙動。

**なぜ必要か**: Phase D4 で「`terminal` を見せない」設定にしたとき、もし MCP サーバー (`stackchan`) まで連動して消えてしまうと `write_note` も `switchbot_*` も呼べなくなって本末転倒だった。**Hermes の設計では「ツールセット = コア機能の取捨選択」「MCP = 外付け機能」が独立軸として扱われている**ので、ツールセットを絞っても MCP は残る。

**喩え**: 家にある道具棚（コアツール）を整理して使わないノコギリを片付けても、家の外の貸し倉庫（MCP サーバー）にしまったものは別管理で消えない。

**使った場面**: Hermes 内部の `tools_config.py:1325-1329` で `include_default_mcp_servers=True` がデフォルト True なので、こちらは何も書かなくて済んだ。

### 2.4 `platform_toolsets` — 「窓口ごとに渡す道具を変える」

**何か**: Hermes は Discord・Slack・Telegram・API（`/v1/chat/completions`）など複数の **プラットフォーム** から呼ばれる前提で、`config.yaml` の `platform_toolsets:` キーに **プラットフォーム → ツールセット** のマップを書く。

**なぜ必要か**: 同じ Hermes プロセス・同じ人格（記憶）のまま、**窓口ごとに見えるツールを変えられる**。Discord からは terminal/browser を使えるが、StackChan（API 経由）からは見せない、ということが 1 行で書ける。Phase D4 の核心。

**喩え**: 同じ受付係に、来訪者が業者なら工具一式を渡す・お年寄りなら筆談ボードと電話だけ渡す、と装備を切り替えてもらう感じ。

**使った場面**: `~/.hermes/config.yaml` の `platform_toolsets.api_server:` に許可リストを書き、`terminal` / `file` / `browser` / `code_execution` を**意図的に列挙しない**ことで drop した。

```yaml
# ~/.hermes/config.yaml（追記分のみ）
platform_toolsets:
  # ... 既存の discord / telegram / slack / ... 各 platform はそのまま ...
  api_server:
  - web        # web_search / web_extract
  - vision
  - todo
  - memory
  - messaging
  - clarify
```

### 2.5 `clarify` toolset — 「分からなければ聞き返す権利」

**何か**: Hermes の小さなコアツール。「ユーザーの意図が複数あり得るとき、Yes/No ではなく**選択肢を提示して聞き返す**」を 1 つのツール呼び出しとして実装したもの。

**なぜ必要か**: D4 で `terminal` や `browser` を外したら、Hermes が「**強引にどれかのツールを呼ぶ**」のではなく「**聞き返す**」を選べる必要があった。clarify を toolset に残しておくと、聞き返しがちゃんと動作として顕在化する。

**喩え**: 道に迷った人に「とりあえずこっちに行ってみよう」と勘で答えるか、「Aさんの家に行きたいですか？それともAビルですか？」と一言で済ますか。後者を選べる人は本当の意味で賢い。

**実測**: 23:07:36 のターンで transcript が `'東京の京の勉強して'` と崩壊したとき、Hermes は強引に terminal を叩くでも嘘応答するでもなく、`reply='ごめん、少し聞き取りが怪しいです。「東京の今日の天気を調べて」か、「今日の勉強を手伝って」のどちらですか？'` と返した。Phase D4 の効果が出ている象徴的なログ。

### 2.6 依頼形マーカー — 「お願いされた瞬間はローカルに渡さない」

**何か**: D3 で発見した症状の対策。「メモして」「お願い」「お願いします」「ちょうだい」「といて」のような **依頼の動詞** を `decide_route()` のキーワードに足して、依頼形が含まれる発話は Hermes 側に流す。

**なぜ必要か**: 短文だからとローカル LFM2.5 (1.2B) に流すと、ローカルは MCP ツールを呼べないにもかかわらず「**追加しました**」と嘘の完了報告をすることがあった（章 3.3 事件簿）。**依頼=行動=ツールが必要**、なら依頼形は全部 Hermes 案件。

**喩え**: 子供（ローカル LLM）にお買い物のお願いはしない（お小遣いも財布も持ってない）。お話相手にはなれるけど、頼みごとはお父さん（Hermes）に。

### 2.7 `keep_alive=24h` — 「警備員を交代させない」

**何か**: Ollama にモデルロードしたあと、そのモデルを VRAM 上に**何時間保持するか**の設定。

**なぜ必要か**: D3-2 で実測したら、keep_alive=30m だと **30 分会話が無いと VRAM から退避** → 次の発話で再ロード（コールドスタート 3.4 秒）が走っていた。VRAM の余裕は十分あるので 24h に伸ばすと **温かいまま** 0.5 秒で返ってくる。

**喩え**: 受付係を 30 分の閑散時間で帰らせるか、24 時間常駐させるか。シフト代（VRAM）が許すなら常駐の方が来客対応が圧倒的に速い。

**使った場面**: `STACKCHAN_LOCAL_LLM_KEEP_ALIVE=24h` を drop-in `local-llm.conf` に追加（コード変更不要）。

---

## 3. 工程の流れ（時系列）

### 工程1: D1 — heartbeat（無音仕草の周期実行）

- 設計の核: **opt-in（環境変数未設定なら完全無効）** + **3 つの抑制ガード**（接続中 / 音声稼働中 / クワイエットアワー 22:00-08:00）
- 実装: `gateway/stackchan_mcp/heartbeat.py`（asyncio 周期タスク、ジッター ±25%）
- 仕草の中身: 見回し / 表情変化 / うなずき の 3 種。**全て無音**（夫婦の会話に割り込まない原則）
- 実機合格: 30 分間隔の本運用で稼働、見回し・表情変化を目視確認

### 工程2: D2 — Web 検索 + ノート（MCP ツール追加）

- 検索: `gateway/stackchan_mcp/web_search.py`。Tavily 主・ddgs 保険のフォールバック構造
- ノート: `gateway/stackchan_mcp/notes.py`。`~/.stackchan/notes/` 限定の write_note / read_note / list_notes。**パストラバーサル防止 + 256KB 上限 + .md/.txt のみ** のサンドボックス
- 既存 :8767 MCP HTTP に同居（新サービス不要）。ツール数は 31 → **35** に
- 単体テスト 19 件追加、ddgs 実検索スモーク成功

### 事件簿1: 嘘の完了報告（D2 の最初の E2E）

「**買い物リストに牛乳をメモして**」と発話したら、ローカル LFM2.5 が嬉しそうに「追加しました」と答えた。**でも `~/.stackchan/notes/` には何も書かれていなかった**。

原因の連鎖（手順を追って書くと）:

1. STT が「ハイモノリストに輸入を埋めまして」と転写崩壊
2. マーカー語「メモ」「リスト」が消えたので `decide_route()` が短文判定 → ローカル LFM2.5 に流す
3. ローカル LFM2.5 (1.2B) は MCP ツールを呼べない（call_tool 機能なし）
4. でも会話としては自然に応答する「追加しました」

**最も危険な失敗モード**: ユーザーから見ると応答が自然で違和感がない。気付かないまま「ロボットの記憶力が弱い」と誤解する。

修正（コミット `441a95f`）:

- ①「メモ」「リスト」+ **依頼形マーカー（して / といて / お願い / ちょうだい）** を `decide_route()` のキーワードに追加 → 依頼=Hermes へ
- ②ローカル LLM 用システムプロンプトに「道具は使えない、できたふりをするな」を追記（1.2B には効きが弱いと判明、①が本命）

教訓: **音声系の E2E は「応答が自然だった」では合格にできない**。サーバーログ（transcript / route / 呼ばれたツール）と成果物（ファイル / 実機の動き）で必ず裏取りする。

### 工程3: D3 — LFM2.5 役割の見直し

- VRAM 競合の心配なし: GPU 利用者は Ollama のみ（0.7GB/6GB）、faster-whisper も VOICEVOX も CPU 動作
- ルーティング実績はデータ不足（local 2 件 / hermes 6 件）で閾値拡大は時期尚早
- 副産物: **コールドスタート問題発見** → keep_alive 24h で常駐させて 0.5 秒維持

### 事件簿2: Hermes が組み込みツールに偏る（本丸）

D1/D2/D3 を本運用に出したあと、E2E ターンが期待通り動かない:

```
2026-06-11 21:45 メモターン:
  → agent.log で見えるのは read_file の 1 回だけ
  → 応答は「すでに入っています」（実際は notes ディレクトリ未作成）

2026-06-11 21:45 天気ターン:
  → terminal → browser_navigate → browser_console の経路
  → web_search ツールは呼ばれず
```

`mcp_servers.stackchan` の ListTools は通っていたが、Hermes 側が **ツール選択の段階で MCP ツールを外して、組み込み (`terminal` / `browser_*` / `read_file` / `write_file`) を優先** していた。`tool_turns=24` まで蓄積していて、組み込みツール慣れが強化されているように見えた。

第一弾の対症療法（コミット `0f997d2`）:

- `gateway/stackchan_mcp/hermes_bridge.py` のシステムプロンプトに `HERMES_VOICE_TOOLS_LINE` を追加
- 「調べ物・天気・ニュースは必ず MCP の web_search を使え」「メモは write_note を使え」「terminal は使うな」「ツールを呼ばずに『やりました』と報告するな」と明示

→ **効かなかった**。同じ夜に再現。プロンプト誘導は **gpt-5.5 が組み込みツールを見えている限り**、その引力に勝てない。

### 工程4: D4 — 経路分離による構造的制約

ユーザー承認を得て **Hermes 側の設定を改造する** 方針に切替。3 案を比較:

- **案 A（リクエスト body 上書き）**: `api_server.py` を grep したら body の `disabled_toolsets` を読まない → 実装不可、棄却
- **案 B（`platform_toolsets.api_server` で経路分離）**: `~/.hermes/config.yaml` 1 箇所追記。Hermes 本体無改造、Discord 無影響、MCP server は自動付与 → **採用**
- **案 C（`HERMES_HOME` プロファイル分離）**: 別 systemd unit でプロセス分離。運用が重く、Discord と人格分裂 → 将来課題に凍結

公式テスト `tests/gateway/test_api_server_toolset.py:101-129` が `{"platform_toolsets": {"api_server": ["web", "terminal"]}}` → enabled が `[terminal, web]` だけになることを保証していたので、案 B の動作根拠は十分。

事前検証スクリプト（Hermes venv で `_get_platform_tools` を直接呼ぶ）で `api_server` は 7 個、`discord` は 19 個（無回帰）を目視確認したうえで `sudo systemctl restart hermes-gateway`。

E2E 結果（22:17 再起動後）:

| シナリオ | 結果 |
|---|---|
| 家電（`switchbot_*`）| ✅ 実機消灯 |
| Discord 経路の `terminal` | ✅ 無回帰 |
| 組み込みツール抑制 | ✅ `[stackchan-voice]` で `terminal` / `browser_*` / `write_file` / `read_file` が CallToolRequest **0 件** |
| `clarify` toolset | ✅ 検索ターンで強引な terminal 利用に走らず聞き返し |
| メモ / 検索 単体 | ⚠️ STT 起因で transcript 崩壊、宿題 |

---

## 4. 設計判断の記録

### 4.1 なぜ「プロンプトでお願いする」では効かなかったか

これは Phase D 最大の学びです。「terminal は使うな、web_search を使え」と日本語で明示注入していたのに、gpt-5.5 はそれを無視してでも組み込みツールに流れた。

考察:

1. **学習データの引力**: gpt-5.5 のように汎用に強い大型モデルは、「Bash でファイル書けばいい」「curl で天気取ればいい」というパターンを膨大に学んでいる。**目の前に terminal がある限り、そっちが「自然」**。
2. **MCP ツールはまだ少数派**: write_note / web_search が候補リストの中で「特殊な MCP ツール」に見える間は、汎用ツールの引力に勝てない。
3. **聞き返し（clarify）も組み込みツールに食われる**: clarify を使う前に、まず terminal を試そうとする。

打ち手として「**お願い**」より「**見せない**」が桁違いに強い。これは LLM のみならず、人間の意思決定の研究（選択肢を絞ると行動が決まる）にも通じる話。

### 4.2 案 B vs 案 C — 同じ人格を保つかどうか

案 B は Hermes プロセス 1 つの中で、窓口ごとに装備を変える。記憶（チャットセッション）は窓口を跨ぐ。
案 C はプロセスを分けて、StackChan は別人格として独立する。記憶も別。

採用したのは案 B。理由は **「夫婦の会話に StackChan を組み込みたい」というプロジェクト思想** に整合する。Discord で夫婦が話したことを StackChan が知っている、StackChan に頼んだメモが Discord 経由でも引ける、という統合体験が損なわれないため。

ただし、将来 Claude Code 等 別クライアントから `/v1/chat/completions` で terminal が必要になったら、その瞬間案 C に切り替える覚悟をしておく（worklog 6.5 学びの 3 項目目）。

### 4.3 やらなかったこと（意図的に）

- **gateway 側のリクエストヘッダー追加**: 当初 `X-Hermes-Platform: stackchan-voice` を送る案を検討したが、Hermes 側に受け口がない（platform はサーバー側で固定）ので不要と判明。
- **MCP server を `platform_toolsets.api_server` に明示列挙**: `include_default_mcp_servers=True` のおかげで不要。明示するとローカル変更点が増えるだけ。
- **ローカル LLM への function calling 機能の追加**: D3 で「依頼=Hermes へ」のルーティングに整理したので、ローカルが道具を呼べる必要は当面なし。1.2B にツール選択をさせるのは無理がある。

---

## 5. 用語集

| 用語 | 一言で |
|---|---|
| heartbeat（D1） | 30 分に 1 回の無音仕草。自律性の最小単位 |
| クワイエットアワー | 22:00-08:00。heartbeat を発火させない時間帯 |
| Tavily | LLM 向けの検索 API。無料 1,000 クレジット/月 |
| ddgs | DuckDuckGo の Python ラッパー。フォールバック用 |
| `~/.stackchan/notes/` | Hermes が `write_note` で書ける唯一の場所（サンドボックス） |
| `decide_route()` | 「ローカル LLM に渡すか Hermes に渡すか」を決めるルール関数 |
| `keep_alive` | Ollama の VRAM 上のモデル保持時間 |
| `platform_toolsets` | Hermes の「窓口別の装備リスト」設定 |
| `include_default_mcp_servers` | MCP サーバーを自動付与するデフォルト挙動。今回は無編集 |
| `clarify` | 「分からないので聞き返す」を 1 ツール呼び出しで完結させるコアツール |
| 依頼形マーカー | 「して」「といて」「お願い」「ちょうだい」など。`decide_route()` で Hermes 行きにするキーワード |
| MCP CallToolRequest | MCP プロトコルで「このツールを呼んでください」と要求するメッセージ。agent.log の判定に使う |
| `[stackchan-voice]` | agent.log で API 経路（声経路）のターンを示すセッション ID。Discord は別 ID |

---

## 6. 次のフェーズ

### Phase D の宿題（声を出しやすい時間帯に）

1. **メモ単体 E2E**: 「○○のことメモして」発話 → `~/.stackchan/notes/*.md` 生成確認、agent.log で `mcp_stackchan_write_note` の CallToolRequest 確認
2. **検索単体 E2E**: 「○○のニュース調べて」発話 → agent.log で `mcp_stackchan_web_search` の CallToolRequest 確認
3. **Tavily API キー登録（任意・推奨）**: `~/.yorishiro/secrets.env` に `TAVILY_API_KEY=tvly-...`
4. **heartbeat 第 2 段階（発話あり）の検討**: `STACKCHAN_HEARTBEAT_SPEAK=1` で Hermes に文脈を渡して短い一言生成 → say。クワイエットアワー必須のまま。

### 持ち越し候補

- **C1 近接視線追従**: ToF Unit (VL53L0X, Grove Port A, ~¥1,000) 購入してから再挑戦。CoreS3 内蔵 LTR-553 はシェルの IR 遮蔽で物理的に不可と判明済み
- **レイテンシ短縮**: 文分割ストリーミング TTS（応答開始までを短く）、faster-whisper プリロード
- **案 C への切替準備**: 外部クライアントから `/v1/chat/completions` を叩く要件が出たときに 30 分で切替できるように、`HERMES_HOME=stackchan-voice` 用の drop-in unit ドラフトをいつか書いておく

---

## 7. このレポートを使った学習例（Claude/Gemini にコピペできる質問）

- 「**章 4.1 で『LLM はプロンプト誘導より構造的制約に従う』と書いてあるが、これに似た現象を機械学習研究で扱った論文を 3 つ挙げて、それぞれ 1 行で要約して**」
- 「**章 2.4 の `platform_toolsets` は、OpenAI の Assistants API や Anthropic の MCP の Resource scoping と何が似ていて何が違う？**」
- 「**章 3 事件簿1 の『嘘の完了報告』は、エージェント開発でよく踏むパターンだと思う。これを安全側に倒すための『出力検証』にどんな型があるか、自分のチームで実装するなら何から始める？**」
- 「**章 6 の『heartbeat 第 2 段階（発話あり）』は、夫婦の会話に割り込まないという原則と両立できる？ どんな抑制条件を足せばいいか、3 つ提案して**」
