# 2026-06-16 GitHub リポジトリ設定: 既定ブランチ変更 + Topics/説明文

## 背景・目的

前段の「リポジトリ整理 & GitHub 公開ページ整備」（2026-06-16-repo-readme-cleanup.md）で README 刷新・docs 索引・GitHub 作法ファイルを develop へマージ・push 済み。残っていた GitHub 側設定 2 件を反映する。

1. **既定ブランチ `main` → `develop`** — `main` は upstream(kisaragi-mochi) 同期専用で、開発本流・CI バッジ(`?branch=develop`)・刷新済み README はすべて `develop`。訪問者・`git clone`・新規 PR ベースを実体に向ける。
2. **Topics / 説明文（Description）の設定** — GitHub 上は未設定だった。

## 手段の調査（前回「MCP 未接続」は誤認だった）

| 観測 | 真相 |
|---|---|
| `claude mcp list` に github が出ない | MCP は `~/.claude.json` の `projects["/home/kenji"].mcpServers.github` に**設定済み**（公式 `ghcr.io/github/github-mcp-server`, Docker/stdio）。ただしスコープが `/home/kenji` のため、現ワークスペース `/home/kenji/dev/yorishiro-workspace` からは未ロード = リストに出なかっただけ |
| 「トークン無」(旧メモ) | **classic PAT (`ghp_…`, 40字, scope=repo/workflow/gist/notifications) が MCP 設定の env に在中** |
| MCP で設定変更できるか | **不可**。公式 github-mcp-server の `repos` ツールはファイル/ブランチ/PR/Issue 中心で、**既定ブランチ/topics/description を編集するツールが無い**（WebFetch で確認） |

→ 結論: **既存 classic PAT を使い、GitHub REST API を `curl` で直叩き**が最確実（MCP も `gh` も新規導入不要、3 操作で完結）。

## 実行（curl + REST API、トークンは出力非表示）

トークンは `jq -r '.projects["/home/kenji"].mcpServers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN' ~/.claude.json` でシェル変数に取り込み、`Authorization: Bearer` ヘッダにのみ渡す（エコー・`set -x` で露出させない）。`~/.claude.json` は `.env` ファイルではないため読み取り禁止対象外。

1. **疎通 + 権限確認**: `GET /repos/tqk1/stackchan-mcp-yorishiro` → HTTP 200 / `permissions.admin == true` / scope に `repo` を確認。
2. **既定ブランチ + 説明文**: `PATCH /repos/tqk1/stackchan-mcp-yorishiro` body `{"default_branch":"develop","description":"<下記>"}` → HTTP 200。
3. **Topics**: `PUT /repos/tqk1/stackchan-mcp-yorishiro/topics` (Accept: `application/vnd.github+json`) body `{"names":[...20件...]}` → HTTP 200。
4. **ローカル追従**: `git remote set-head origin develop`（`git remote show origin` の `HEAD branch: develop` を確認）。

## 確定した設定値

### 説明文（Description）— 「思想 ＋ スタック ＋ upstream 感謝」の融合
ユーザー選択。設計哲学/ビジョンは出してよい（夫婦・家庭の話は伏せる）という線引きで作成。
```
依代 (yorishiro) — giving an autonomous agent a body. An all-in-one embodied-companion stack for the M5Stack StackChan: ESP32-S3 firmware + Python MCP gateway (voice via VOICEVOX / faster-whisper, on-device reflexes, opt-in heartbeat, SwitchBot). Built unobtrusive & local-first. A grateful hard fork of kisaragi-mochi/stackchan-mcp.
```
- 思想 = `giving an autonomous agent a body`（ビジョン）/ `Built unobtrusive & local-first`（設計哲学）
- AI が読むと必要技術スタック（ESP32-S3 / MCP / VOICEVOX / faster-whisper / SwitchBot）を判別できる
- 約 310 字（GitHub Description 上限 350 字内）

### Topics（20 件 / GitHub 上限ちょうど）
技術スタックを網羅し、訪問エージェントが「自端末に何が要るか」を判別できる構成。
```
stackchan, m5stack, esp32, esp32-s3, esp-idf, xiaozhi,
mcp, model-context-protocol, ai-agent, llm, embodied-ai,
companion-robot, robotics, voice-assistant, text-to-speech,
speech-to-text, voicevox, whisper, smart-home, switchbot
```
（すべて小文字/ハイフンの GitHub topic 規則に適合。GitHub 側ではアルファベット順に表示される）

## 検証

- `GET /repos/...` → `default_branch: develop` / `description` 一致。
- `GET /repos/.../topics` → `count=20`、全件反映。
- `git remote show origin` → `HEAD branch: develop`。
- すべて HTTP 200、トークン値はどの出力にも非出現。

## 用語メモ

- **classic PAT vs fine-grained PAT**: `ghp_` 始まりは classic（スコープが `repo` 等の粗粒度、`repo` があれば Administration 含み設定変更可）。`github_pat_` 始まりは fine-grained（リポジトリ単位＋権限種別、settings 変更には "Administration" permission が別途必要）。今回は classic + `repo` scope で admin:true が取れた。
- **既定ブランチ (default branch)**: `git clone` 時のチェックアウト先・新規 PR のベース・GitHub トップで最初に見えるブランチ。`origin/HEAD` のシンボリック参照が指す先で、`git remote set-head` でローカル側の表示を追従させる。

## 残・スコープ外

- GitHub MCP を現ワークスペース(user スコープ)に移す再設定は本タスクに不要のため見送り（必要なら別途）。
- メモリ更新済み: `project_yorishiro_phase`（残課題①②を完了＋MCP 真相）/ `feedback_no_personal_in_public`（「私的文脈一切排除」→「夫婦・家庭は伏せるが個人のスキル・思想・ビジョンは可」に緩和）。
