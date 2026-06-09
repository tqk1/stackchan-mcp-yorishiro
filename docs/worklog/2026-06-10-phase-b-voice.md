# 作業記録: Phase B 音声会話パイプライン構築（2026-06-10）

> このファイルは「後から読んで、何をして・何が動いていて・どういう構成なのかを学べる」ことを目的とした記録です。
> 疑問が出たら、このファイルごと Claude や Gemini に貼り付けて「ここを詳しく」と聞ける粒度で書いています。

---

## 1. 今日のゴールと結果

**ゴール**: StackChan に話しかけると、Hermes Agent が考えて、StackChan の声で返事をする「最小の会話往復」を作る。

**結果**: ✅ 達成（シミュレーション検証まで）。実機タップでの最終確認だけ残っている。

```
「好きな食べ物はある?」
  → StackChan(マイク) → 音声認識 → Hermes が思考
  → 「うん、ぼくは食べられないけど、選ぶならラーメンかな」
  → 音声合成 → StackChan(スピーカー)
```

往復時間: 約 7〜18 秒（Hermes の思考時間に依存）。

---

## 2. いま動いているシステムの全体図

razer-server (このマシン、Ubuntu) の上で 3 つの常駐サービス + 開発用 gateway が動いている。

```
                        razer-server (192.168.0.19)
  ┌─────────────────────────────────────────────────────────────┐
  │                                                             │
  │  [Hermes Agent]  systemd: hermes-gateway.service            │
  │   ・Discord で reactive に動く「思考体」(モデル: gpt-5.5)    │
  │   ・APIServerAdapter :8642 ← 今日有効化。HTTP で会話を      │
  │     注入できる口 (OpenAI 互換 API)                          │
  │          ▲                                                  │
  │          │ ③ HTTP POST /v1/chat/completions                 │
  │          │   「転写テキスト」→「返答テキスト」              │
  │          │                                                  │
  │  [gateway]  ※今は開発用に手動起動 (scratch/mcp_repl.py)     │
  │   Python プロセス 1 個の中に 3 つの顔がある:                │
  │   ├─ WebSocket サーバー :8765 … ESP32 と常時接続           │
  │   ├─ HTTP capture サーバー :8766                            │
  │   │    ├─ /capture    … ESP32 がカメラ画像を POST          │
  │   │    ├─ /pcm        … 外部から音声を流し込む口           │
  │   │    └─ /voice_turn … ★今日実装。会話1往復の心臓部       │
  │   └─ MCP サーバー … ツール(say/listen/move_head 等 30個)   │
  │          │                                                  │
  │          │ ② STT (faster-whisper, ローカル)                 │
  │          │ ④ TTS 依頼                                       │
  │          ▼                                                  │
  │  [VOICEVOX]  systemd: voicevox.service :50021               │
  │   ・テキスト → 音声 (WAV) を作る合成エンジン                │
  │                                                             │
  └─────────────────────────────────────────────────────────────┘
            ▲ WebSocket (JSON + バイナリ音声フレーム)
            │ ①録音した音声 / ⑤合成した音声
            ▼
  [StackChan 実機]  M5Stack CoreS3 (192.168.0.10)
   ・firmware は xiaozhi-esp32 の fork
   ・mDNS で gateway を自動発見して自分から接続しにくる
```

### 会話 1 往復の流れ（番号は図と対応）

1. **録音**: 画面タップ → firmware が録音開始、音声を Opus 圧縮して WebSocket でgateway へ送る。録音終了で gateway が Ogg ファイルにまとめ、`STACKCHAN_AUDIO_HOOK_URL`（= 自分自身の `/voice_turn`）へ POST
2. **音声認識 (STT)**: `/voice_turn` が Ogg を展開し、faster-whisper（ローカル、CPU）でテキスト化
3. **思考**: テキストを Hermes の API (:8642) に投げ、返答テキストをもらう
4. **音声合成 (TTS)**: 返答テキストを VOICEVOX (:50021) で音声化
5. **再生**: 音声を Opus 圧縮し、WebSocket で実機へ送って再生

---

## 3. 今日やった作業（時系列）と「なぜ」

| # | 作業 | なぜ |
|---|---|---|
| 1 | 設計判断の確定: 音声=完全ローカル / Hermes接続=MCP | API コスト 0・プライバシー重視。レイテンシが厳しければ後でハイブリッド化できる段階的アプローチ |
| 2 | 事前調査（サブエージェント3並列） | 「Opus音声ストリームは自前実装」と思っていたら、**firmware も gateway も upstream が実装済み**と判明。作る量が激減した |
| 3 | VOICEVOX 復旧: 旧 yuno 残骸の `~/trash/` からエンジン本体(2.1GB)を `~/apps/voicevox/` へ移設、unit を drop-in で修正 | unit が消えたディレクトリを指して起動失敗ループしていた。**drop-in** (`/etc/systemd/system/X.service.d/*.conf`) なら元の unit ファイルを書き換えずに設定を上書きでき、戻すのも消すだけ |
| 4 | Hermes の APIServerAdapter 有効化 (`API_SERVER_ENABLED=true` を drop-in で注入) | Hermes は Discord からしか話せなかった。実は OpenAI 互換 HTTP サーバーが同梱されており、環境変数 1 個で 127.0.0.1:8642 に開く |
| 5 | `hermes_bridge.py` 実装（fork 独自の新規コードはこれだけ） | 既存部品（録音hook・STT・TTS）を 1 本につなぐ「結線」。`/voice_turn` エンドポイント約 250 行 |
| 6 | セキュリティ強化 4 件 | 自動レビュー指摘: ①ボディ2MiB上限 ②展開後PCM上限(爆弾対策) ③トークン未設定時はループバックのみ許可(LAN開放を防ぐ) ④上流エラーの中身を外に漏らさない |
| 7 | mDNS 広告アドレス固定 (`STACKCHAN_MDNS_ADVERTISE_ADDR`) | gateway が Tailscale IP 等の「届かない住所」まで名乗っていて、ESP32 が順に試して接続に ~50 秒かかっていた。LAN IP だけ名乗らせて **13 秒**に短縮 |

**コミット** (ブランチ `feature/phase-b-voice`):
- `1fd2ec4` feat: /voice_turn ブリッジ追加
- `1da204c` fix: /voice_turn セキュリティ強化
- `23ef800` feat: mDNS 広告アドレス固定

**検証ツール**（`scratch/`、git 管理外）:
- `mcp_repl.py` … gateway を起動して、ファイルにコマンドを書き込むとツールを実行してくれる常駐スクリプト
- `test_voice_turn.py` … VOICEVOX で「ユーザーの発話」を合成して `/voice_turn` に投げる。**実機タップなしで全パイプラインをテストできる**

---

## 4. 用語ミニ解説

- **MCP (Model Context Protocol)**: AI エージェントに「ツール」(関数) を提供する標準プロトコル。gateway は MCP サーバーで、`say`(喋る)・`move_head`(首を動かす) 等 30 個のツールを持つ。Claude Code も Hermes も MCP クライアントになれる
- **stdio MCP / Streamable HTTP MCP**: MCP の接続方法 2 種。stdio = クライアントがサーバーを子プロセスとして起動し標準入出力で会話（親が死ぬと子も死ぬ）。Streamable HTTP = サーバーが常駐し HTTP で会話（独立して生きられる）
- **mDNS**: LAN 内の「名前を呼ぶと住所を教えてくれる」仕組み。ESP32 はこれで gateway の IP を自動発見する
- **Opus / Ogg**: Opus = 音声圧縮コーデック(60ms 単位の小さなフレーム)。Ogg = フレームをファイルにまとめる容器
- **faster-whisper**: OpenAI Whisper 音声認識の高速ローカル実装。初回はモデル読込で ~19 秒、以降 ~0.7 秒
- **systemd drop-in**: `X.service.d/*.conf` を置くと unit 本体を書き換えずに設定を上書き・追記できる仕組み。今日 voicevox(パス修正) と hermes(API有効化) で使用

---

## 5. B7 とは何か（次の作業の予習）

**課題**: Hermes から StackChan のツール（喋る・首を動かす・写真を撮る）を使えるようにしたい。つまり Discord で「StackChan で喋って」と言えるようにする。そのために gateway を Hermes に MCP サーバーとして登録する。

**選択肢が 2 つあった**:
- **(a) Hermes の子プロセスにする (stdio)**: 設定 1 行で済むが、gateway の生死が Hermes と連動する。Hermes を再起動するたびに gateway も再起動 → ESP32 も再接続、と巻き添えが連鎖する
- **(b) gateway を独立常駐させ、HTTP で接続する** ← 採用: gateway を systemd サービスにして、Hermes からは `http://127.0.0.1:8767/mcp` に接続。お互い独立に再起動できる。調査の結果、**HTTP MCP サーバー機能は upstream 実装済み**（`stackchan-mcp serve --transport streamable-http`）で、追加コードなしで構成だけで実現できると判明

これが終わると、gateway は「手動起動の開発用プロセス」から「マシン起動時に勝手に立ち上がる常駐サービス」になる。

---

## 6. 残タスク

- [ ] **実機確認**（要ケンジさん在宅): スピーカー音出し / 画面タップ → 会話成立
- [ ] **B7**: gateway の systemd サービス化 + Hermes への MCP 登録（準備中）
- [ ] **B8**: `API_SERVER_KEY` 設定 — 今はステートレス(毎回初対面)なので、Hermes が会話を覚えるにはこのキーでセッション継続を有効化する必要がある
- [ ] レイテンシ短縮（Phase C 候補): faster-whisper のプリロード、文分割 TTS ストリーミング

## 7. 深掘り用の質問例（このファイルを AI に貼って聞く）

- 「Opus フレームを WebSocket のバイナリメッセージで送る、の意味を初心者向けに」
- 「systemd の drop-in と unit 本体編集の使い分けは?」
- 「MCP の stdio と Streamable HTTP の違いをシーケンス図で」
- 「なぜ VAD 常時起動ではなく明示タップにしたのか、この設計のトレードオフは?」
