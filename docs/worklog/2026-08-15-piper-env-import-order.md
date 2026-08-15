# 2026-08-15 — streamable-http 経路で `.env` が engines に届かない（import 順）

Dale さんからのバグ報告（⑳）。`say` が必ず失敗し、gateway ログは
`Piper model path is not configured`。ところが `gateway/.env` には
`STACKCHAN_PIPER_MODEL` が正しく書かれていて、モデルファイルも存在する。

Dale さん側の Claude Code が import 連鎖まで辿った状態で報告してくれたため、
こちらの仕事は「診断の裏取り」と「影響範囲の確定」から始まった。

## 1. 症状

- transport は `serve --transport streamable-http`（⑱ で移行した新しい経路）
- Claude Code / Hermes とも 40/40 tools で接続はできている
- `say` を叩くと毎回 `Piper model path is not configured`
- `STACKCHAN_PIPER_MODEL` は `gateway/.env` にあり、パスも実在

## 2. 原因（Dale さんの診断どおり）

`cli.py` の `_run_streamable_http_placeholder()` が、**`.env` を読む前に**
`.http_server` を import していた。

```python
def _run_streamable_http_placeholder(*, advertise_mdns: bool = True) -> None:
    from .ownership import release_lock_if_owner
    from .http_server import (...)   # ← ここで import 連鎖が走る

    _configure_gateway_startup()     # ← _load_dotenv() はこの中
```

import 連鎖:

```
cli.py                 from .http_server import ...
  http_server.py:31      from .stdio_server import ...
    stdio_server.py:25     from .stt import listen_and_transcribe
    stdio_server.py:26     from .tts import synthesize_and_send
      tts/__init__.py:74     _try_register(_register_piper, "piper")   # module level
        tts/piper.py:218       self._model_path = model_path or os.getenv(PIPER_MODEL_ENV)
```

engine は **module level で construct** され、`__init__` で env を
**一度だけ**読む。その時点で `.env` はまだ読まれていないので `None` が
そのままプロセスの寿命ぶんキャッシュされる。

### stdio 経路が無事な理由

`_run_stdio_gateway` は `_configure_gateway_startup()`（＝`_load_dotenv()`）を
先に呼び、`.stdio_server` の import は `_run()` の**関数本体の中**にある
（`cli.py:701` 付近の遅延 import）。よって `.env` が先に載る。
`--preflight` も自前で先頭に `_load_dotenv()` を持つ（`cli.py:521`）ので無事。
つまり **壊れていたのは `serve --transport streamable-http` だけ**。

## 3. 影響範囲は Piper だけではなかった

`os.getenv` をトレースして `import stackchan_mcp.http_server` の間に
読まれる env を列挙した（`scratchpad/trace_import_env.py`）:

```
STACKCHAN_PIPER_MODEL
STACKCHAN_VOICEVOX_URL
STACKCHAN_VOICEVOX_DEFAULT_SPEAKER
STACKCHAN_FASTER_WHISPER_MODEL
STACKCHAN_FASTER_WHISPER_DEVICE
STACKCHAN_FASTER_WHISPER_COMPUTE_TYPE
```

**6 つ**が import 時に焼き付いていた。Piper だけが例外を投げるので目立っただけで、
VOICEVOX は `http://127.0.0.1:50021` に、faster-whisper は既定値に、
**黙って**フォールバックしていた（＝エラーにならないぶん質が悪い）。

## 4. なぜこちらでは一度も出なかったか

本番 gateway は同じ `serve --transport streamable-http` で動いている。
しかし systemd unit / drop-in が全部 `Environment=` と
`EnvironmentFile=` で渡していて、Python 起動時点で既に `os.environ` に
入っている。よって import 順に関係なく読める。

一方 README は「`gateway/.env` に書け」と案内している（README.md:943）。
**ドキュメントどおりにやると踏む**バグだった。⑮（tap-to-talk）⑱（1 gateway 多
クライアント）⑲（loopback でもトークン必須）に続いて 4 回目の同型
＝「自分の実稼働構成が systemd と私的メモにしかない」。

## 5. 実機での裏取り

推定で修正層を決めない（[[feedback_verify_on_real_env]]）ため、先に再現。
piper パッケージはこの venv に未導入なので、`find_spec("piper")` を通すだけの
スタブを `PYTHONPATH` に置いて再現した（`scratchpad/repro_piper_env.py`）。

```
===== 現状の順序（cli.py と同じ）=====
  os.environ STACKCHAN_PIPER_MODEL = /home/dale/voices/en_US-amy-medium.onnx
  PiperEngine.model_path = None                      ← 環境変数はあるのに None
  VoicevoxEngine url     = http://127.0.0.1:50021    ← .env の URL が消えている

===== 修正後の順序（.env を先に読む）=====
  PiperEngine.model_path = /home/dale/voices/en_US-amy-medium.onnx
  VoicevoxEngine url     = http://voicevox.example:12345
```

## 6. 修正

### ① `_configure_gateway_startup()` を import の前へ（`cli.py`）

Dale さんの提案どおり。あわせて「この順序は意味がある」ことをコメントで残した
（将来のリファクタで戻されるのを防ぐ。原因が import 順なので、コメントが無いと
ただの並べ替えに見える）。

### ② regression テスト（`tests/test_cli.py`）

`_configure_gateway_startup` をスパイに差し替え、呼ばれた瞬間に
`stackchan_mcp.tts` / `stackchan_mcp.stt` が `sys.modules` に**居ないこと**を
確認する。**別インタプリタ（subprocess）で実行**するのが要点で、テスト
セッション内では他テストが既に `.tts` を import 済みのため、in-process では
順序を観測できない。スパイは即 `SystemExit` するので `.env` も読まず、
ロックも取らず、ポートも掴まない（[[reference_test_env_isolation]]）。

修正を `git stash` して**テストが落ちること**も確認済み:

```
assert 'IMPORTED_EARLY False False' in 'IMPORTED_EARLY True True\n'   ← 修正前
```

## 7. 検証

- `pytest` **1145 passed**（1144 → +1）
- `ruff check` clean
- 実関数 `_run_streamable_http_placeholder` を通した end-to-end 確認で
  `model_path` が解決されること（§5 下段）

## 8. 用語

- **module level（モジュールレベル）**: 関数の中ではなくファイル直下に書かれた
  コード。`import` した瞬間に実行される。`tts/__init__.py:74` の engine 登録が
  これ。
- **遅延 import（lazy import）**: 関数の中に書く `import`。呼ばれるまで実行
  されない。stdio 経路が助かったのはこれのおかげ。
- **import 連鎖**: A が B を import し、B が C を import し…と芋づるに走ること。
  1 行の `from .http_server import` が `.tts` の engine 構築まで引き起こしていた。
- **python-dotenv / `load_dotenv()`**: `.env` を読んで `os.environ` に載せる
  ライブラリ。「載せる」だけなので、**既に読み終わったコードには効かない**。

## 9. 教訓

- **env を construct 時に一度だけ読む設計は、import 順に依存する**。
  読み取りが lazy（使う瞬間に `os.getenv`）なら、この種のバグは成立しない。
  今回は最小修正（順序）に留めたが、engine 側を lazy にする案は残っている。
- **エラーになる設定より、黙って既定値に落ちる設定のほうが危険**。
  Piper は叫んだから 1 日で直った。VOICEVOX と faster-whisper は
  「なんとなく動いている」まま何ヶ月も気付かれない可能性があった。
- **自分の起動方法（systemd）がドキュメントの起動方法（`.env`）と違うと、
  ドキュメント経路のバグは永久に自分では踏まない**。4 回目。

## 10. 返信

`private-notes/reply-to-dale-2026-08-15.txt`
