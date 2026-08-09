# 2026-08-09 — タップ会話ループの文書化 / MCP ツール呼び出しログ / プロンプト i18n

対象ブランチ: `feature/tts-piper`（`14d8035`, `da7c82c`・origin push 済み）
検証: pytest **1140 passed**（+1）/ ruff clean

## 背景

外部ユーザーによる初の導入で、タップ会話（画面タップ→録音→STT→エージェント→TTS）が
**無言で動かない**状態が数日続いた。原因は機能の欠落ではなく、**有効化スイッチが
どこにも書かれていなかった**こと。

```
STACKCHAN_AUDIO_HOOK_URL   ← これ 1 個でループが有効になる
```

`README` にも `.env.example` にも記載が無く、しかも未設定時の分岐は
`esp32_client.py:650` で **DEBUG レベル**にしか記録しない。既定の INFO では
「タップしても何も起きない、ログにも何も出ない」という最悪の見え方になる。

Features で謳っている機能に、到達手段が存在しなかった。

## やったこと

### 1. `### 7. tap-to-talk voice loop` を README 両言語に新設

- スイッチ（`STACKCHAN_AUDIO_HOOK_URL`）と、未設定時に**無言で失敗する**ことの明記
- 頭脳側（`HERMES_API_URL`）と、Hermes で見落としやすい 2 点
  - **API サーバーは opt-in**: `API_SERVER_ENABLED=true` を付けて起動したときだけ
    8642 を bind する。対話チャットモードでは開かない → gateway 側は毎回 502
  - **会話の記憶は API キーに紐付く**: gateway が `X-Hermes-Session-Id` を送るのは
    `HERMES_API_KEY` があるときだけ。無いと毎ターンがステートレス
- 言語切替に必要な環境変数一式
- 起動ログでの確認方法と、症状 → 原因の対応表

`.env.example` にも voice-loop セクションを追加（33 行 → 約 90 行）。

> 執筆中に `VOICEVOX_URL` と書きかけたが、実名は `STACKCHAN_VOICEVOX_URL` だった。
> **ドキュメントの変数名は必ず `grep` でコードと突き合わせる。**

### 2. MCP ツール呼び出しのログ

どのツールをモデルが呼んだかが、これまで**一切ログに残っていなかった**
（トランスポートは汎用の `CallToolRequest` しか記録しない）。「LED を白にしたのは
誰か」といった事後追跡が原理的に不可能だった。

`log_mcp_tool_call()` を新設し、**トランスポートの入口 2 箇所**に置いた。

| 置いた場所 | 経路 |
|---|---|
| `stdio_server.py` の `call_tool` | stdio MCP |
| `http_server.py` の `handler` | streamable-http MCP (:8767) |

**★ 最初 `_dispatch_mcp_tool` の冒頭に置いたが、これは誤り。**
同関数は MCP クライアント経路だけでなく **gateway 自身の内部ディスパッチャ**でもある
（presence の I2C ポーリング 5 秒毎、ダッシュボードのセンサータブ ≈2.5Hz、
`get_touch_state`）。ここに INFO を置くと、**クライアントが一度も呼んでいない呼び出しで
ログが洪水になる**。呼び出し元を全部数えてから場所を決めること。

プライバシー: **引数はキーのみ INFO、値は DEBUG**。`say` の発話本文や `write_note` の
本文がログファイルに残らないようにした。

```
INFO stackchan_mcp.stdio_server: MCP tool call: say(text)
```

### 3. `HERMES_VOICE_TOOLS_PROMPT` の新設

`hermes_bridge.py:171` で、システムプロンプトに `HERMES_VOICE_TOOLS_LINE`
（**日本語ハードコード**）が**毎ターン無条件に連結**されていた。

```python
"content": system_prompt + HERMES_VOICE_TOOLS_LINE,
```

`HERMES_VOICE_SYSTEM_PROMPT` を英語にしても日本語の段落が必ず混入するため、
**応答が日本語に引き戻される**。つまり既存の env だけでは英語で喋るロボットを
構成できなかった。`STACKCHAN_STT_LANGUAGE`（★⑩）で耳を英語にした続きで、
口が日本語のままだったことになる。

既存の上書きと同じ流儀で `HERMES_VOICE_TOOLS_PROMPT` を追加。README の言語レシピを
「3 つ」→「**4 つ**」に訂正した。

既定を assert する既存テストには `delenv("HERMES_VOICE_TOOLS_PROMPT")` を追加
（開発者のシェルに値が残っていてもテストが誤魔化されないように）。

## 用語

| 用語 | 意味 |
|---|---|
| audio hook | ファームが録音し終えた音声を gateway が POST する先。自分自身の `/voice_turn` に向けるとループが閉じる |
| APIServerAdapter | Hermes に同梱された OpenAI 互換 HTTP サーバー。`API_SERVER_ENABLED=true` で 8642 に開く |
| BYPASS_TOOLS | streamable-http でキューを経由せず直接ディスパッチされるツール群 |

## 学び

1. **「機能はある」と「到達できる」は別**。Features に書いた機能は、有効化手順まで
   書いて初めて存在する。
2. **無言の失敗を DEBUG に落とさない**。ユーザーが最初に見るのは INFO。
   未設定で機能が丸ごと死ぬ分岐は、せめて一度は警告する価値がある（今回は未対応・宿題）。
3. **ログを足す場所は、呼び出し元を数えてから決める**。共有ヘルパの冒頭は
   「全部通る」場所であって「クライアントが呼んだ」場所とは限らない。
4. **i18n はレイヤで漏れる**。STT（耳）を直しても、プロンプト（口）に
   ハードコードが残っていれば英語ロボットにはならない。
   残り: `proactive.py`（49 行）/ `weather.py`（38 行）/ 画面ステータス文言。
