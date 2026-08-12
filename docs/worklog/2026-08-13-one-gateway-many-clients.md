# 2026-08-13 — gateway は 1 つ、クライアントは複数 / 修正層を実環境で確かめる

対象ブランチ: `feature/tts-piper`（`8f74499`・origin push 済み）
検証: pytest **1144 passed**（+4）/ ruff clean ＋ **稼働中の実 gateway に対する実行確認**

## 症状

外部ユーザーから「MCP サーバーが `connecting` のまま解決しない」。しかも
**gateway ↔ ESP32 は完全に正常**（40 ツール検出・ESP32 ready・MAC 取得）で、
MCP クライアント側だけが繋がらない。同じ現象が Hermes と Claude Code の
両方で起きていた。

## 真因

**stdio MCP サーバーは「gateway を 1 つ起動する」こと自体が仕事**である。

MCP クライアントが `stackchan-mcp` を stdio で登録すると、クライアントは
それを子プロセスとして起動する。そして**その子プロセスが gateway そのもの**
になる。WS `:8765`、capture `:8766` を開き、デバイスの所有権を取る。

```
stdio 登録 = 「動いている gateway に繋ぐ」ではなく
             「2 つ目の gateway になろうとする」
```

1 台のデバイスに gateway は 1 つなので、2 つ目は所有権ロック
（`ownership.py` → `cli.py` の `OwnershipError` 分岐）で拒否され `exit(1)`。

```
stackchan-mcp: device already owned by stackchan-mcp-… (pid …, since …)
```

**拒否は正しい。問題は起きる場所**で、これは **MCP ハンドシェイクより前**。
クライアントは「サーバーが失敗した」という応答を受け取れず、プロセスが
終了したことしか観測できない → `connecting` のまま。加えてメッセージは
stderr に出るため、多くの MCP クライアントは表示しない。

**gateway 自体は健全に見えるのに、クライアントだけが永久に繋がらない。**

## 構造的な背景

| | 前提するクライアント数 | 適切なトランスポート |
|---|---|---|
| 上流の想定 | 1（Claude Code が gateway を spawn し所有） | stdio で十分 |
| 本フォークの実態 | 複数（常駐 gateway ＋ Hermes ＋ Claude Code、裏で音声ループ） | **streamable-http 必須** |

タップ会話ループは gateway が会話と会話の間も生き続ける必要がある。
生き続けている以上、その隣で stdio 登録が成功することは原理的に無い。

そして**本番で使っている常駐 + HTTP `:8767` の構成は worklog にしか
書かれていなかった**。README section 3 は stdio だけ、リポジトリの
`.mcp.json` も stdio。2026-08-09 の `STACKCHAN_AUDIO_HOOK_URL` と同型の
「動く構成が未文書」である。

## 解

`_run_streamable_http_daemon` は `gateway.start()` も呼ぶので、
**1 プロセスが 3 役を兼ねる**。

```
stackchan-mcp serve --transport streamable-http

  :8765  ESP32 WebSocket
  :8766  写真アップロード / voice_turn
  :8767  Streamable HTTP 上の MCP  ← クライアントはここへ
```

`validate_bind_safety` は**非 loopback bind のときだけ**トークンを要求する
ので、既定の `127.0.0.1` ならトークン不要。

## やったこと

1. **所有権拒否メッセージに解決策を追記**（`cli.py` の `_acquire_startup_lock`）。
   **stdio モードのときだけ**、`--transport streamable-http` の実コマンドと
   登録先 URL を出す。デーモン transport では出さない（無関係な助言になる）。
2. **`gateway.start()` の `OSError` ハンドリング**（`_start_gateway_or_exit`）。
   所有権ロックが先に効くので通常は発火しないが、**無関係なプロセス**が
   WS_PORT / CAPTURE_PORT を掴んでいる場合は素の traceback が出ていた。
   到達条件は docstring に明記した。
3. **README 両言語に「One gateway, several clients」を新設**。ポート早見表、
   stdio が使える条件と使えない条件、`claude mcp add --transport http` の
   実コマンド、タップ会話ループは常にデーモン形式が必要である旨。

## ★ 最大の学び — 修正層を推定で決めない

**最初、原因をポート bind 衝突だと推定して修正を書いた。**

根拠はコード読解だった。`cli.py` の

```python
await gateway.start(advertise_mdns=advertise_mdns)   # ここでポートを bind
...
await run_stdio_server(notify_config=notify_config)  # MCP はこの後
```

に加え、`esp32_client.py` の `websockets.serve()` に `try/except` が無い。
「ポートが埋まっていれば例外で即死し、ハンドシェイクに到達しない」——
筋は通っている。

**実際に走らせたら違った。** このマシンは gateway が常駐しているので、

```bash
python -m stackchan_mcp serve
```

を叩くだけで再現でき、出力は**所有権ロックの拒否メッセージ**だった。
ポート bind まで到達していない。書いたばかりのポート衝突ハンドラは
**実際には一度も通らないコード**だった。

そのまま出していれば「直した」と誤報告していた。

> **コードは「その行に到達したら何が起きるか」しか教えない。**
> **到達するかどうかは、走らせないと分からない。**

テストがグリーンでも意味は「書いたテストが通った」であって
「実環境でその経路が発火する」ではない。再現できる環境が手元に
あるなら、修正層を決める前に再現する。

## 用語

| 用語 | 意味 |
|---|---|
| stdio transport | MCP クライアントがサーバーを子プロセスとして起動し、標準入出力で話す方式。1 クライアント専有 |
| Streamable HTTP transport | サーバーが HTTP で待ち受け、複数クライアントが接続できる方式。`/mcp` |
| 所有権ロック | `~/.stackchan/owner.lock`。1 台のデバイスに gateway が 1 つであることを保証する hardlink ベースの排他 |
