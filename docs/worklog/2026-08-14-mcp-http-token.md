# 2026-08-14 — Streamable HTTP の `/mcp` は loopback でもトークンを要求する

前回（2026-08-13）の案内で `--transport streamable-http` に切り替えてもらった
ところ、Dale さんの Claude Code が `401 Unauthorized` で接続できなかった。
**原因は前回こちらが書いた README の記述そのものが誤っていたこと。**

## 1. 症状

Dale さんの手順は全て正しく実行されていた。

```
gateway: acquired ownership lock / Uvicorn running on http://127.0.0.1:8767
client : claude mcp add --transport http stackchan http://127.0.0.1:8767/mcp
結果   : /mcp が ✘ failed
```

gateway 側のログ:

```
POST /mcp                                        401 Unauthorized
GET  /.well-known/oauth-protected-resource/mcp   404 Not Found
GET  /.well-known/oauth-authorization-server     404 Not Found
POST /register                                   404 Not Found
```

Dale さん自身が「gateway が 401 で拒否し、その後 Claude Code が OAuth を
探しに行って何も見つけられていない」と正しく読み取っていた。

## 2. 原因

`_GuardedASGIApp.__call__`（`gateway/stackchan_mcp/http_server.py:1252-1264`）:

```python
path = scope.get("path", "")
token_protected = path in {"/mcp", "/status"} or path.startswith(CONTROL_PATH_PREFIX)
if self._token and token_protected:          # ← bind 先が loopback かは見ていない
    expected = f"Bearer {self._token}"
    if request.headers.get("authorization") != expected:
        → 401 AUTH_FAILURE_MESSAGE
```

トークンが**設定されていれば**、loopback だろうと `/mcp` は Bearer を要求する。

### 混同していた別関数

`validate_bind_safety()`（`http_server.py:93-96`）は**まったく別の話**だった。

```python
def validate_bind_safety(host: str, token: str | None) -> None:
    """Reject non-loopback daemon binds when no HTTP bearer token is set."""
    if not token and not is_loopback_bind_host(host):
        raise ValueError(NON_LOOPBACK_TOKEN_REQUIRED_MESSAGE)
```

これが決めているのは「**トークン無しで起動してよいか**」であって、
「**トークンがあるとき認証を省くか**」ではない。

| | loopback | 非 loopback |
|---|---|---|
| トークン未設定 | 起動可・`/mcp` 認証なし | **起動拒否** |
| トークン設定済 | 起動可・**`/mcp` は Bearer 必須** | 起動可・`/mcp` は Bearer 必須 |

前回この表の右下と左下を混同し、README に
「No token is needed while the daemon stays on loopback」と書いた。
Dale さんはファーム接続用に `STACKCHAN_TOKEN` を設定済み（＝表の左下）なので、
401 になるのが正しい動作だった。

## 3. 実機検証

[[feedback_verify_on_real_env]] に従い、推定でコードを直す前に稼働中の
gateway（razer-server :8767）で再現を取った。

```bash
curl -X POST http://127.0.0.1:8767/mcp -d '{"jsonrpc":"2.0",...,"method":"initialize",...}'
#   → 401  Unauthorized: missing or invalid bearer token

curl -X POST ... -H "Authorization: Bearer $STACKCHAN_TOKEN"
#   → 200
```

Dale さんのログと完全一致。ケンジさんの gateway も同じ挙動（`token: SET (len=48)`）
であり、**環境差ではなく仕様**であることが確定した。

`claude mcp add --header` が使えることも `claude mcp add --help` で確認
（`-H, --header <header...>`）。

## 4. 修正

### ① README 両言語の誤記訂正

`README.md` / `README.ja.md` の "One gateway, several clients" セクション末尾。
「loopback ならトークン不要」→ トークン設定時の `--header` 付き登録例に差し替え、
併せて **「クライアントはこの 401 を曖昧な接続失敗として報告するので、
gateway のログを見ること」** を明記した。

```bash
claude mcp add --transport http stackchan http://127.0.0.1:8767/mcp \
  --header "Authorization: Bearer $STACKCHAN_TOKEN"
```

### ② 起動ログに認証要否を 1 行（`cli.py`）

Dale さんは uvicorn のログを見て 401 に気づいた。答えを同じ場所に置く。

```
Streamable HTTP MCP daemon starting on http://127.0.0.1:8767/mcp
  /mcp requires 'Authorization: Bearer <STACKCHAN_TOKEN>' (register clients
  with that header, e.g. claude mcp add --header)
```

トークン未設定時は `/mcp is unauthenticated (no STACKCHAN_TOKEN set)`。

既存の "daemon starting" 行が実際に journal に出ていることを
`journalctl -u stackchan-gateway` で確認済み（同一 logger・同一関数内の
連続行なので新しい行も確実に出る）。restart は不要と判断した。

## 5. 検証

- **pytest 1144 passed / ruff clean**
- 実機 curl で 401 → 200 の再現と解消を確認（上記 §3）

## 6. 用語

- **Streamable HTTP**: MCP の HTTP トランスポート。1 プロセスが待ち受け、
  複数クライアントが同じエンドポイントに接続できる。stdio はクライアントが
  サーバーを spawn するので 1 対 1 になる（前回の主題）。
- **Bearer token**: `Authorization: Bearer <値>` ヘッダーで送る共有秘密。
  本 gateway では `STACKCHAN_TOKEN` / `BEARER_TOKEN` の値がそれで、
  ファームの WS 接続・`/mcp`・`/status`・`/control/*` を同じ 1 本で守っている。
- **OAuth discovery**: MCP クライアントが 401 を受けたとき
  `/.well-known/oauth-protected-resource` 等を叩いて認証方式を自動発見する仕組み。
  本 gateway は OAuth を実装していないので 404 が並ぶ。**401 の理由が
  「単純な bearer 不足」でも同じ探索に入る**ため、クライアント側の
  エラー表示は原因を示さない。

## 7. 教訓

**★⑲ 実稼働構成の未文書化、3 件目。**

| # | 日付 | 欠落していたもの |
|---|---|---|
| ⑮ | 08-09 | tap-to-talk の設定一式（`STACKCHAN_AUDIO_HOOK_URL` 等） |
| ⑱ | 08-13 | 常駐 gateway + 複数クライアント（stdio では不可能） |
| ⑲ | 08-14 | トークン設定時は `/mcp` も Bearer 必須 |

3 件とも「ケンジさんの環境では何ヶ月も前からそう動いているが、
worklog と私的メモにしか書かれていない」もの。外部ユーザーが
最短距離で踏み抜いている。

**さらに今回固有の教訓**: ⑲ は単なる欠落ではなく、⑱ を直したときに
**こちらが書いた新しい記述が間違っていた**。関数名（`validate_bind_safety`）の
docstring を読んで「loopback なら不要」と要約したが、その関数は
**起動可否**を決めるもので**リクエスト認証**とは別レイヤーだった。
コードを読んだ「つもり」で書いたドキュメントが、次のユーザーを詰まらせた。
→ ドキュメントに書く挙動も、実機で 1 回叩いてから書く。

## 8. 返信

`private-notes/reply-to-dale-2026-08-14.txt`（送信待ち）。
構成: ①読み取りは正しく操作も正しい・前回のこちらの記述が誤り ②loopback は
「トークン無しで起動してよいか」を決めるだけ ③実機で 401→200 を再現した ④
`--header` 付き再登録の実コマンド（値は `.env` から literal で貼る・シェル
展開に頼らない＝PowerShell/bash 差） ⑤Hermes も同じヘッダーが要る（先に
知らせて二晩目を防ぐ） ⑥修正 2 件 ⑦3 件連続で私的メモ由来だったことを軽く
認める ⑧前回の宿題は急がなくてよい。

トーンは [[feedback_dale_tone_light]] に従い、自責への言及なし。
