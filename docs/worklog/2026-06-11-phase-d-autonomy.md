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
- 🔜 **D3**: VRAM 実測・ルーティング閾値の検討（実機検証と合わせて実施）
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
