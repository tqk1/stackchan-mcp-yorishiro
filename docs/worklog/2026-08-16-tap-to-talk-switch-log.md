# 2026-08-16 — タップ会話のスイッチが「切れている」ことを起動時に言わせる

Dale さんからの報告。⑳（import 順）の修正を pull して gateway は
クリーンに起動するようになったが、**画面をタップしても応答しない**。
LED が青 → アクティビティアイコンが回る → 白 → 消灯。そして
**Hermes のターミナルには何も出ない**（新しいターンも転写もエラーも無し）。

質問はこうだった:

> tap-to-talk は、タイプしたメッセージと同じように Hermes を経由するのか？
> それとも ESP32 のマイクと Hermes をつなぐ別のパーツがあって、それが
> このノートPCで起動していないのか？

今回のこちらの仕事は、**バグ修正より先に「その前提が違う」ことを説明する**
ことだった。

## 1. 経路は逆向きだった（＝Hermes が静かなのは正常）

```
typed:  Hermes  →  gateway  →  ロボット     Hermes がクライアント（/mcp）
tap:    ロボット →  gateway  →  Hermes      gateway がクライアント（:8642）
```

タップ経路で gateway は **Hermes の OpenAI 互換 API を自分から叩く**
（`HERMES_API_URL` + `/v1/chat/completions`、`hermes_bridge.py:187-190`）。
Dale さんがタイプしている対話セッションとは別のポート・別の向き・
別のプロトコルで、**正常に動いていてもあの画面には何も出ない**。

つまり「Hermes がアイドルだった」は**失敗の証拠ではなく、証拠のある場所
ではない**というだけ。証拠は gateway のログにある。

## 2. transport 切り替えは無関係と確定

⑱ で stdio → `--transport streamable-http` に移行させた直後の症状なので、
まずそこを疑うのが自然だった。investigator に両起動パスを追わせ、
**差分ゼロ**と確定:

| | stdio | streamable-http |
|---|---|---|
| gateway 起動 | `cli.py:728` | `cli.py:941` |
| 経由する関数 | `_start_gateway_or_exit()` | 同じ |
| WS :8765 / capture :8766 | `gateway.start()` | 同じ |
| `/voice_turn` 登録 | `capture_server.py:474` | 同じ（transport 非依存） |
| `.env` → engine 構築の順序 | 遅延 import で正 | `28b2bb1` で正に揃った |

「同じはずだ」と仮定せず並べて読んだ。★⑲ の教訓
（[[feedback_verify_on_real_env]]）をコード読解にも適用した形。

## 3. 実際の欠陥＝**「切れている」ことを誰も言わない**

`STACKCHAN_AUDIO_HOOK_URL` が未設定だと、デバイス起点のキャプチャ経路が
丸ごと無効になる。ところがその事実は**どこにも出力されない**:

- `esp32_client.py` の `start()` は**有効なときだけ** INFO を出していた
- タップ時の握り潰しは `logger.debug`（`esp32_client.py:649-656`）で、
  gateway は `logging.basicConfig(level=logging.INFO)`（`cli.py:797`）

結果、**外から見ると「マイクが壊れている」と完全に区別がつかない**。
LED は一通り動き、ログは無言、例外も出ない。

★⑬（2026-08-08）で同じ変数が原因の報告を受けており、**同じ設定が同じ
沈黙で2回踏まれた**ことになる。★⑮ で README には書いたが、
**ドキュメントは「読めば分かる」であって「動いているものが教えてくれる」
ではない**。

### なぜ今になって切れた可能性が高いか

`_load_dotenv()` は引数なしの `load_dotenv()`（`cli.py:434`）＝
**起動ディレクトリから上に辿って** `.env` を探す。`gateway/` から起動すれば
`gateway/.env` は見つかり、リポジトリのルートから起動すれば見つからない。
Dale さんは 8/13 に起動方法を変えている。作業ディレクトリも変わっていれば、
**あのファイルは何も言わずに黙る**。

この探索規則も README のどこにも書いていなかった。

## 4. 修正（`e5f9eda`）

### コード

`esp32_client.py:491-505` — 無効側にも INFO を出す。

```
Device-driven listen capture disabled (STACKCHAN_AUDIO_HOOK_URL not set):
screen taps and the wake word are ignored. The listen() tool is unaffected.
```

「何が効かないか」と「何は効くか」を両方書いたのは、読む人にとって最初の
分かれ道が**壊れているのか、そもそも使っていない機能なのか**だから。
上流の主経路は MCP 駆動の `listen()` であって、そちらは無傷である。

### テスト

`tests/test_esp32_client.py` に有効/無効を parametrize で追加。
**`git stash` して修正前に無効側だけが落ちることを確認済み**
（有効側は元から通るので、それだけ通っても意味がない）。

### README 両言語

- トラブルシューティング表の1行目を
  「タップしても無反応・ログにも何も出ない」→
  「**起動ログが listen capture 無効と言う**」に差し替え
- **`.env` は起動ディレクトリから上に辿って探される**ことを明記
  （`gateway/.env` はリポジトリのルートからは見えない）
- `stackchan-mcp --preflight` が実際に見えている値を出すことを案内
  （preflight は自前で `_load_dotenv()` する＝`cli.py:521`）

検証: **pytest 1147（+2）/ ruff clean**、実際の起動ログも目視確認。
origin push 済み（`feature/tts-piper`）。

## 5. Hermes 側 issue #27834 について

Dale さんが自分で見つけて共有してくれた（DeepSeek V4 のツール呼び出しが
Windows 上の Hermes で生テキストとして表示される）。こちらのコードの話では
ないので深追いしないが、**closed as "not planned"** である点だけ返信に足した。
「追跡済み＝いずれ直る」と読んでいたので、**修正待ちの列ではなく現在の挙動**
だと伝えておく必要があった。

普通の会話には影響しない（音声ループに必要なのはそれだけ）。ただし後で
「雑談はできるのに電気を点けない」が起きたら**同じバグの別の顔**で、
逃げ道はこちら側の回避策ではなくその役割に別モデルを使うこと。

## 6. 教訓

- **無効であることを言わない設定は、故障と区別がつかない。**
  ★⑬⑮ で「文書化されていない」を直したが、**沈黙そのもの**は直していな
  かった。同じ変数で2回踏まれて初めてそこに手が届いた。
- **エラーになる設定より、黙って無効になる設定のほうが危険**
  （⑳ の「Piper だけが例外を投げ、他5つは黙って既定値」と同じ形）。
- **ドキュメント経路は自分では踏まない**（[[reference_documented_path_untested]]）
  の5件目。今回は `.env` の探索規則そのもの。systemd の `Environment=` で
  渡している限り、この規則には一生ぶつからない。

## 7. 残（Dale さん回答待ち）

- `stackchan-mcp --preflight` と gateway 起動ログ（+タップ時のログ）
- 上の新しい行が出るかどうかで原因が確定する
- 第二候補＝Hermes の API サーバー未有効（`curl :8642/health`）
- **前回までの宿題は依然未回答**（`set_volume(100)` / `STACKCHAN_MULTITURN=1` /
  16kHz Piper ボイス / `timings_ms=` 行 / 緑LED 自動消灯の有無）

## 用語

- **tap-to-talk / デバイス起点キャプチャ** — 画面タップやウェイクワードなど
  **デバイス側**が録音を開始する経路。MCP クライアントが `listen()` を呼ぶ
  上流の経路とは別物で、`STACKCHAN_AUDIO_HOOK_URL` が有効化スイッチ。
- **`/voice_turn`** — capture サーバー（:8766）に登録されるエンドポイント。
  Ogg/Opus を受け取り、STT → LLM → TTS → デバイス再生までを1本で回す。
- **parametrize** — pytest で同じテスト本体を複数の入力で回す仕組み。今回は
  有効/無効の2ケースを1つの関数で書いた。
