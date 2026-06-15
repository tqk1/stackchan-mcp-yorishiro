# 2026-06-15 フェーズ4: サーバタブの Codex/Gemini 利用率 → 両方スキップで決着

ダッシュボード機能拡張プロジェクト（全5フェーズ）のフェーズ4。当初「サーバタブに CC 利用率と同じ形式で **Codex 利用率** と **Gemini API 利用額** を並べる」予定だった。計画段階で「**まず取得手段を調査し、無ければ相談**」としていた通り、調査の結果 **両方とも取得手段に障害があり、ユーザー判断で両方スキップ**（実装コードは書いていない）。本フェーズは worklog のみ。

---

## 結論サマリ

| 対象 | 判断 | 理由 |
|---|---|---|
| Codex 利用率 | **スキップ** | OpenAI に「枠%」を返す公式 API が無い・ローカルにもキャッシュ無し → 取得不可 |
| Gemini API 利用額 | **見送り** | 自動取得は Cloud Billing→BigQuery Export が事実上唯一・GCP 未設定で設定コストに見合わず |
| dashboard 受け皿 | 無傷で温存 | CC利用率カードのパターンは将来流用可 |

---

## なぜ Codex 利用率が取れないか

**前提**: Claude Code は `https://api.anthropic.com/api/oauth/usage`（OAuth トークン認証 + `anthropic-beta: oauth-2025-04-20` ヘッダ）で「5時間枠 / 7日枠の使用率(%)とリセット時刻」を返す専用エンドポイントを持つ。これを `~/razer-dashboard/fetch_usage.py` が cron で叩き `~/cc-usage.json` にキャッシュ → `status_api.py` の `cc_usage()` が `/status` に同梱 → dashboard が `setUsage()` で描画している。

**Codex（OpenAI/ChatGPT）側に同等物が無い**:
- OpenAI/ChatGPT のサブスク利用率（5時間枠等）を返す**公式 API が存在しない**（2026-06 時点）。`/v1/usage` 系はサブスクのレート枠%を返さない。
- `~/.codex/` のローカル状態は SQLite ×3（`state_*` / `logs_*` / `goals_*`）で、`tokens_used`（累積トークン消費数）はあるが、**利用率% / rate_limit / resets_at は記録されていない**。
- `codex` CLI 自体も PATH 不在（このマシンでは別経路で利用）。

→ CC と同形式の「枠%」カードは原理的に作れない。累積 `tokens_used` を「消費量」として出すことは理論上できるが、ユーザーの当初要望（利用率%）とは別物のためスキップ。

## なぜ Gemini 利用額は設定が重いか

- **Gemini API（Generative Language API）/ AI Studio には利用額を返す公式エンドポイントが無い**。API キー方式では課金照会できない。
- GCP の実コストをプログラムで取る経路は実質 **Cloud Billing → BigQuery Billing Export → SQL クエリ** の一択（Cloud Billing API 単体は請求先の紐付け管理用でコスト額は返さない。コストレポートは Console UI のみ）。
- 必要物: GCP プロジェクト + Billing Export 有効化（過去分は遡れず、有効化後にデータが溜まり始める。反映に数時間）+ BigQuery 読み取り権限のサービスアカウント JSON。
- razer-server は **現状 Gemini 未使用・gcloud 未インストール・GCP 未設定**。ゼロから上記を組む割に、現状コストは実質ゼロで表示メリットが薄い。

→ 設定コストに見合わないため見送り。**将来 Gemini を実際に使い始めたら、BigQuery Export を有効化した上で `fetch_usage.py` と同じ JSON キャッシュ方式で再開できる**。

## 将来再開時の構成図（参考・今回は未実装）

```
[Anthropic OAuth usage API] ──┐  ※ 既存・稼働中
                              │
[Codex: 公式 usage API 無し] ─┼─→ fetch_usage.py (cron) → ~/cc-usage.json
                              │        └ window(): {used_percentage, resets_at} へ正規化
[Gemini: Cloud Billing       │
   → BigQuery Export → SQL] ──┘
                                       │
                              status_api.py :8080 cc_usage()
                                       │  /status に同梱
                                       ▼
                              dashboard.html #tab-server
                                 setUsage()/remain()/usageColor() で描画
```

将来 Codex/Gemini を足すなら、`fetch_usage.py` の出力 JSON に `codex:{...}` / `gemini:{...}` キーを増やし、dashboard 側は `setUsage('codex', u?.codex)` のように既存関数を流用するだけで済む（受け皿は既に汎用）。

## 用語

- **OAuth usage API**: Anthropic が OAuth トークン保有者向けに提供する利用率照会エンドポイント（`/api/oauth/usage`）。サブスクの枠消化%とリセット時刻を返す。OpenAI/Google には同等の公開物が無いのが今回の肝。
- **BigQuery Billing Export**: GCP の課金明細を BigQuery テーブルへ日次エクスポートする機能。GCP コストをプログラムで集計する正攻法だが、有効化が前提でセットアップが重い。
- **window()**（`fetch_usage.py`）: API の `{utilization, resets_at(ISO)}` を dashboard 用の `{used_percentage, resets_at(epoch)}` に正規化する変換関数。利用率カードの共通入口。

## 次

- **最終フェーズ5: dashboard 人間工学的仕上げ**（`~/razer-dashboard/dashboard.html`・git管理外・flash不要）。着手時にスコープ確認（中=推奨/軽量/フル）。完了後 learning-report 1本（全5フェーズ）。
- フェーズ4 決着＋フェーズ5 準備の詳細: `~/.claude/plans/eager-enchanting-truffle.md`。
