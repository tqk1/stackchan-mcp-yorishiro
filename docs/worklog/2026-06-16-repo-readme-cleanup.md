# 2026-06-16 リポジトリ整理 & GitHub 公開ページ整備

## 背景・目的

StackChan 開発が Phase A〜F まで一区切り。GitHub で人に見せられる状態にする。

調査で判明した核心: **既定ブランチ `main` に出ている README は fork 元 kisaragi-mochi の README をほぼそのまま引き継いだ内容**で、Hermes / 依代 / Phase A〜F の独自機能の記載が `main`/`develop`/全 feature ブランチで **ゼロ**だった。つまり yorishiro 独自の紹介 README は未作成。一方リポジトリ自体は整頓済み（秘匿情報分離・ビルド成果物 gitignore・tasks アーカイブ済）。

→ 今回の作業は「README 英日の全面刷新」と「GitHub の見せ方・衛生」に集約。

## ユーザー決定

1. **既定ブランチを `develop` に変更**（README は develop に。`main` は上流同期専用のまま）。
2. トーン = 物語性は活かし、**個人情報（夫婦・自宅・内部IP・Hermes endpoint）は伏せる**。
3. 整理範囲 = README 英日刷新 / GitHub 衛生 / GitHub 作法ファイル / docs 索引、の 4 項目すべて。

## 実行（マルチエージェント + Codex）

- Explore×3 で現状調査（構造・機能棚卸し・フォーク帰属/ライセンス素材）。
- 並行サブエージェント 2 本: ①GitHub 作法ファイル（SECURITY.md・Issue テンプレート）②docs 索引（docs/README.md）。
- README 英日は本体（オーケストレータ）が identity-first 構成へ刷新。
- **Codex 第二意見（必須ゲート）**: 漏えい監査・fork 礼儀/帰属・GitHub 作法/正確性を精読 → 🟡2件＋🟢1件を反映。

## 変更内容

### README.md / README.ja.md（identity-first へ刷新）
上部を全面刷新（言語バナーは踏襲）:
- タイトル `stackchan-mcp-yorishiro` ＋ タグライン「依代 — 自律エージェントに身体を与える」＋ バッジ（build / License MIT / firmware）
- **Acknowledgements / 謝辞**（kisaragi-mochi・78/xiaozhi-esp32・stack-chan/石川真也・m5stack-avatar・Feetech・M5Stack）
- **上流との違い**（オンデマンドなツール面 vs 常駐する身体化コンパニオン）
- **Features**（Phase A〜F: 音声対話／ローカル優先ルーティング／ファーム自律反射／通知型 heartbeat／SwitchBot／ダッシュボード／カスタマイズ）
- **Architecture**（agent → gateway → firmware ＋ voice/SwitchBot/dashboard。プレースホルダのみ）
- **Design principles**（明示的トリガー等の技術的原則のみ）/ **Documentation map**（Phase A〜F へのリンク）

Quick Start を **build-from-source 主経路**に修正:
- firmware: 上流の pre-built バイナリには yorishiro 機能が無い旨を明記し、オプション B（ソースビルド）を推奨に。
- gateway: PyPI `stackchan-mcp` は上流のもので yorishiro 機能を含まない旨を明記し、`gateway/` からのソース起動を推奨に。

既存の良質な技術節（WebSocket/auth・TTS/STT・notify・安全注記・ライセンス・upstream 帰属）は**保全**（identity 部のみ再構成）。

### 新規ファイル
- `SECURITY.md` — GitHub Private Vulnerability Reporting ベースの脆弱性報告ポリシー（個人連絡先は書かない）。
- `.github/ISSUE_TEMPLATE/{bug_report.yml, feature_request.yml, config.yml}` — Issue Forms。バグ報告にはハードウェア欄・ログのリダクト注意を含む。
- `docs/README.md` — docs 索引（Architecture / Setup / Phase reports / Dashboard / Worklog の 5 カテゴリ・25 エントリ）。

### Git
- `feature/review-cleanup` の未 push コミット群を `develop` へ ff（13 コミット）。
- 作業ブランチ `feature/repo-readme-cleanup` で上記を実施 → develop へマージ。

## 検証

- 秘匿情報スキャン: 変更ファイルに実 IP（192.168.0.19 / 100.70.x）・razer・Hermes endpoint・メール等の混入なし（一致したのは `192.168.1.100` 等のドキュメント例・`<node>.<tailnet>.ts.net` プレースホルダのみ）。
- 内部リンク実在確認: docs/phase-*.md・architecture.md 等すべて ok。
- Codex 監査: ①リーク OK ②帰属 OK（人称の軽微指摘のみ）③SECURITY のパッケージ名・Issue テンプレのリンクを修正。
- gateway `pytest` / `ruff`: コード非変更だが develop 統合状態で緑を確認。

## 用語メモ

- **依代（よりしろ）**: 神霊を招いて宿らせる対象。ここでは自律エージェント（Hermes ベース）を「霊」、StackChan を「器」に見立てたプロジェクト名。
- **Issue Forms**: GitHub の YAML 形式 Issue テンプレート（`.github/ISSUE_TEMPLATE/*.yml`）。フォーム UI で構造化入力を促せる。
- **Private Vulnerability Reporting**: GitHub の Security タブから非公開で脆弱性を報告できる仕組み。公開 Issue を立てずに済む。
- **ff（fast-forward）マージ**: 分岐していないブランチを、コミットを作らずポインタ前進だけで進めるマージ。

## 残（ユーザー操作 or 次セッション）

- **既定ブランチを develop に変更**（`gh`/API トークンが無いため GitHub Web UI: Settings → Branches → default branch）。
- **リポジトリ Topics / description 設定**（GitHub Web UI: About 欄）。
- マージ済み feature ブランチの整理（一覧提示 → 確認の上）。
