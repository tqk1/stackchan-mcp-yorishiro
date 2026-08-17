# 2026-08-17 — 検査しない文字列は、いつか相手を黙らせる

外部環境からの報告4通を受けての作業。うち1件が**デバイスの完全な固着**で、
残りは認識精度・応答速度・設定手段に関する具体的な要望だった。

---

## 1. 何が起きたか

英語圏の利用者が、タップ会話で「Edmonton の天気は？」と尋ねた。

- 画面が**完全に真っ黒**
- 青い LED は**点灯したまま**
- タッチは**完全に無反応**
- **自動リブートはしない**（画面長押しの強制再起動でのみ復帰）
- gateway のログには**何も異常が出ていない**（通常の 200 OK のみ）

そして重要な対比として、**数日前の同じ質問では正常に「取得できない」と答えていた**。

---

## 2. 原因 — gateway と firmware をまたいで一本につながった

3つの調査（gateway の weather 経路 / firmware のハング経路 / STT 設定）を
並行で走らせ、結果を突き合わせた。

```
「Edmonton の天気は？」
   │
   ▼
Hermes(DeepSeek) が web_search ツールを呼ぶ
   │  ← 日本語のシステムプロンプトで「1〜3文で短く」と指示している
   │  ← 日本語のツール指示（HERMES_VOICE_TOOLS_LINE）
   ▼
長い応答、あるいはツール呼び出しの生マークアップが混入した応答
   │  （利用者自身が見つけた既知の不具合 #27834。closed as "not planned"）
   ▼
gateway: 長さも文字種も検査せず set_subtitle(reply) と TTS へ渡す
   │  hermes_bridge.py:501,506 ← ★ ここが穴
   ▼
firmware: SetSubtitleText が上限なしで lv_label_set_text
   │  → その場で lv_refr_now() を同期実行（stackchan.cc:4925-4949）
   ▼
そこで固まると Application タスクが LVGL ロックを保持したまま居座る
   │
   ├─ LVGL 専用タスクは lvgl_port_lock(0) の非ブロッキング試行に失敗し続ける
   │     → lv_indev_read（タッチ）と lv_timer_handler（再描画）が同時停止
   │
   ├─ LED は表示更新の「前」に確定済み（application.cc:943 → :945）
   │     → 青のまま残る
   │
   └─ TWDT はアイドルタスクのみ購読 + PANIC 無効（sdkconfig:1605-1610）
         → リブートしない
```

**観測された4点すべてが、推測を挟まずに説明できる。**

「数日前は答えられた」のは、ツールを使わず短文で即答した回だったから。
原因が変わったのではなく、**発症条件を踏む確率が変わった**だけだった。

---

## 3. なぜ gateway 側で塞ぐと決めたか

firmware 側にも明確な弱点がある。`DisplayLockGuard`（`display.h:66-73`）は
30秒の Lock に失敗しても**エラーログを出すだけでスコープに入り**、ロックを
持たないまま LVGL オブジェクトを触る。

```cpp
DisplayLockGuard(Display *display) : display_(display) {
    if (!display_->Lock(30000)) {
        ESP_LOGE("Display", "Failed to lock display");   // ← 出すだけ
    }
}
```

それでも今回触らなかった理由:

1. 上流（xiaozhi-esp32）由来の構造で、影響範囲が広い
2. 修正には**外部の利用者に再 flash を頼む必要**があり、遠隔では負担もリスクも大きい
3. **実害は gateway 側で完全に塞げる** — デバイスに不正な入力が届かなければ発症しない

デバイスは自分を守れない。**送る側が送らないのが正しい。**

---

## 4. 実施した修正

### (A) デバイスへ送る文字列のガード（`control.py`）

出口は2つ（`set_device_status_text` / `set_device_subtitle`）しかないので、
そこで塞げば**すべての呼び出し元**が守られる。

```python
MAX_SUBTITLE_CHARS = 200   # 字幕は幅300px・高さ78pxでクリップされる
MAX_STATUS_CHARS = 64      # ステータスは1行

def _sanitize_device_text(text: str, limit: int) -> str:
    clean = text.encode("utf-8", "ignore").decode("utf-8", "ignore")  # 不正な文字を落とす
    clean = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in clean)  # 制御文字
    clean = " ".join(clean.split())        # 連続空白を圧縮
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean
```

上限値は「安全そうな数字」ではなく**表示できる量**から決めた。字幕は
78px でクリップされるので、それ以上送っても見えない。

### (B) 応答の長さをログに残す（`hermes_bridge.py`）

これまで `reply[:120]` しか記録しておらず、**長すぎる応答が来ても証拠が
残らなかった**。今回まさに「実際に何文字だったか」が分からず、原因の確定に
実機ログが必要になった。

```python
logger.info("voice_turn: reply=%r len=%d session=%s", reply[:120], len(reply), session_id)
```

### (C) STT の語彙ヒント（`stt/faster_whisper.py`）

固有名詞の誤認識（ロボット自身の名前、話者の名前）が2件報告された。
調べると認識設定が**速度側に振り切ったまま固定**されていた。

| | 修正前 | 修正後 |
|---|---|---|
| `beam_size` | `1` 固定（Whisper 既定は 5） | `STACKCHAN_FASTER_WHISPER_BEAM_SIZE` |
| 語彙ヒント | **渡す手段が存在しない** | `STACKCHAN_FASTER_WHISPER_HOTWORDS` |

faster-whisper 1.2.1 の実ソースを読み、`hotwords` が `prefix` 未指定なら
`sot_prev` にトークンとして注入され、**VAD の有無に依存しない**ことを確認して
から採用した（VAD 依存という説があったため）。

起動ログには**未設定の場合も含めて必ず名乗らせた**:

```
Loading faster-whisper model=base device=cpu compute_type=int8 beam_size=1 hotwords=not set
```

前回（★⑬⑮㉑）と同じ教訓 — **無効であることを言わない設定は、故障と
区別がつかない**。

### (D) 検索地域の固定解除（`web_search.py`）

DuckDuckGo フォールバックが `region="jp-jp"` ハードコードだった。日本国外の
利用者が「ここの天気は？」と聞くと日本向けの検索結果を引く。
`STACKCHAN_SEARCH_REGION` で上書き可能にした（既定は現行維持）。

### (E) 事実と食い違っていたコメントの訂正（`control.py`）

`apply_persisted_volume` の docstring が
「The firmware does not persist the user's chosen volume」と書いていたが、
firmware の実装を読むと**音量は NVS に永続化されている**:

- `AudioCodec::SetOutputVolume`（`audio_codec.cc:40-46`）が `Settings` 経由で NVS へ書き込む
- `AudioCodec::Start`（`audio_codec.cc:29-38`）が起動時に読み戻す

mic gain のほうは `SetInputGain` に NVS 書き込みが無く、コメントは正しい。
**volume だけが上流で永続化対応されていた**ため、片方のコメントが古いまま
になっていた。

### (F) ドキュメント（`.env.example` / README 両言語）

- Piper の推奨例を `medium` → **`low`**（ネイティブ16kHz＝リサンプル素通し・
  合成が軽い・既知の歯擦音の歪みも同時に解消）
- 英語運用の設定を「4つ」→「5つ」に（`STACKCHAN_SEARCH_REGION` 追加）
- **「1〜3文で」はレイテンシの設定である**ことを明記（応答は全文を合成し
  終えてから喋り始めるため）
- ツール指示の英語例に「ツールを呼ばずにやったと言うな」を含めた
  （実際に「設定を保存しました」と嘘をつく事例が報告されたため）

### (G) アバターの並び順をツール説明に明記（`stdio_server.py`）

「応答の直前に毎回 embarrassed（照れ）の顔が出る」という報告があった。
調べると、**gateway にも firmware にも、応答直前に表情を変える実装は無い**。

- タップ会話の経路（`/voice_turn` → TTS 送出）は表情に一切触らない
- firmware が自律的に `"embarrassed"` を出すのは**背面 Si12T の「なで」検知のみ**
  （`stackchan.cc:4232-4241`）＝ 設計原則②そのままの反射
- `kDeviceStateSpeaking` の遷移（`application.cc:990-999`）には表情呼び出しが無い

一方、`load_avatar_set` のペイロードは**バイトオフセットだけで解釈される**。
14枚の画像に名前情報は無く、**並び順がそのままマッピング**になる。

| index | 0-5 (顔) | 6-8 (目) | 9-13 (口) |
|---|---|---|---|
| | idle, happy, thinking, sad, surprised, **embarrassed** | open, half, closed | closed, half, open, e, u |

つまり順序を1つでも取り違えると、**チェックサムも転送バイト数も正常のまま、
全ての表情が別の絵になる**。誰かが `set_avatar("happy")` を送っていて index 1
に照れ顔が入っていれば、報告された症状と完全に一致する。

そして `load_avatar_set` のツール説明文は**枚数とバイト数しか書いておらず、
この並び順が書かれていなかった**。順序は `avatar_set.h` のコメントと
`convert_avatars.py` のソースにしか無い。ツール説明だけを見て自作の
パッカーを書けば、取り違える余地がそのまま残っていた。

説明文に並び順と「順序が違っても正常にロードされてしまう」ことを明記した。

---

## 5. 検証

- `pytest` **1166 passed**（基準 1147 → +19）
- `ruff check` clean
- 新規 `tests/test_faster_whisper.py` — **このモジュールは今まで
  テストが1本も無かった**。STT のテストは全て orchestrator 層でフェイクを
  注入する設計だったため、具体エンジンが構造的な死角になっていた
  （Piper の実ローダが本番でハングするまで気付かなかったのと同じ形）

---

## 6. 用語

| 用語 | 意味 |
|---|---|
| **LVGL** | 組み込み向けの GUI ライブラリ。ESP32 の画面描画を担う |
| **`lv_refr_now()`** | 画面の再描画を**その場で同期的に**実行する。非同期に投げるのではなく、呼んだタスクが完了まで戻らない |
| **TWDT** | Task Watchdog Timer。固まったタスクを検出してリブートする仕組み。今回は**アイドルタスクしか監視しておらず、PANIC も無効**なので発火しない |
| **NVS** | Non-Volatile Storage。ESP32 のフラッシュ上の設定保存領域。再起動しても残る |
| **beam_size** | 音声認識の探索幅。1 は最短経路のみ（速いが弱い）、5 は複数候補を保持して比較する |
| **hotwords** | 認識時に「この語が出るかもしれない」と事前に教える仕組み。デコーダのプロンプトとして注入される |
| **RGB565** | 1ピクセルを16bit（赤5・緑6・青5）で表す形式。組み込みの画面でよく使う |

---

## 7. 教訓

**検査しない入力は、いつか必ず壊れた形で来る。**

gateway は「Hermes が返した文字列」を信頼していた。だが Hermes の先には
LLM がいて、LLM は長い応答も壊れたマークアップも返す。信頼できない相手から
来たものを、自分を守れない相手（firmware）へそのまま渡していた。

**そして今回、証拠が残っていなかった。** `reply[:120]` しか記録して
いなかったため、「実際に何文字だったのか」が誰にも分からなかった。
ログの切り詰めは、**切り詰めた事実を記録しない限り**、後から原因を
確定する手段を奪う。

もう一つ。**日本語の既定プロンプトが、非日本語環境では静かに効かなくなる。**
「1〜3文で短く」は文体の指示に見えるが、応答を全文合成してから喋る構造では
**そのままレイテンシの設定**である。設定が効いていないことは、遅さとして
現れるだけで、エラーにはならない。
