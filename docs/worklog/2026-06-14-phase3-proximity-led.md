# 2026-06-14 フェーズ3: 近接 listen mode + トグル + LED 明るさ/横並び

ダッシュボード機能拡張プロジェクト（全5フェーズ）のフェーズ3。当初「音量200 + 近接listen」を予定していたが、**音量200はソース上で実現不可と判明し除外**。近接センサーの反応を mode 3択化（デフォルト listen）。セッション中のユーザー追加要望で、近接の**トグル化**・**LED 明るさ**・**LED UI 横並び**も同時に実装。firmware flash は1回（app のみ）。

---

## なぜ音量200を諦めたか（ソース根拠）

`firmware/managed_components/espressif__esp_codec_dev/esp_codec_dev.c`:
- デフォルトボリュームカーブ `_get_default_vol_curve`（L82-85）: `vol=0 → -50dB`, `vol=100 → 0dB`（線形）。
- `_get_vol_db()` L99-101: **`vol >= 100` は 0dB（最大）でクリップ**。
- AW88298 のレンジ上限も 0dB（`aw88298.c` `vol_range.max_vol.db_value = 0`）。

→ **100 が既にコーデック/アンプの物理最大音量。200 を渡しても 100 と同じ音**。デジタル増幅は歪むため不採用（CLAUDE.md 安全方針）。真に上げるなら PA アナログゲイン（`hw_gain`）引き上げだが歪みリスクで見送り。スライダー上限は 100 据置。

---

## 変更内容

### firmware（`main/boards/stackchan/stackchan.cc`）— flash 要
- `enum class ProxMode { Off, Reflex, Listen }`、デフォルト `Listen`。`StringToProxMode` / `ProxModeToString` ヘルパー。
- `prox_reflex_enabled_`(bool) → `prox_mode_`(ProxMode) に置換（全6参照箇所）。
- `HandleProximity`: mode 分岐。
  - `reflex` = 従来（首上げ + happy + idle revert）。
  - `listen` = **tap 同等トグル**: `GetDeviceState()==kDeviceStateListening` なら `StopListening`（録音送信）+ 全LED消灯、else `StartListening` + 全LED緑（控えめ輝度）。首振り・表情なし。
- `ProximityPollTick`: 発火ガードを `prox_mode_ != Off` に。**listening 中は cooldown をバイパス**（2回目のかざしで必ず止められる。start 同士の連発は従来 5秒 cooldown で抑制）。
- NVS migration（`InitializeLtr553Proximity`）: `mode` キーが無ければ旧 `enabled` から導出（`true→listen / false→off`）。旧キーは残置（read-only・冪等）。
- `get_touch_state`: `prox_reflex_enabled`(bool) → `prox_mode`(string)。`set_proximity_config`: 引数 `enabled` → `mode`(string, allow-list 検証＋`ok:false`返却)。

### gateway（`gateway/stackchan_mcp/`）— flash 不要・要再起動
- `http_server.py`: `/control/proximity` を `mode` 検証に、`_proximity_status` を `prox_mode` 読みに。新規 `POST /control/led_brightness`。
- `stdio_server.py`: `set_proximity_config` inputSchema を `mode` enum（reflex/listen/off）に。
- `control.py`（LED 明るさ）: `DEFAULT_LED` に `brightness:100`、`_clamp_led_brightness`/`_scale_rgb`/`set_led_brightness` 追加。**LED 送信2経路**（`_send_led_color` と `apply_led_state`）の両方で r/g/b を brightness でスケール。`_normalize_led` が brightness を保持。

### dashboard（`/home/kenji/razer-dashboard/dashboard.html`、git 管理外）
- ③センサー・反射: 近接 UI を `<select id="sc-prox-mode">`（listen/reflex/off）に。
- ⑤デバイス調整: LED を **3列横並び**（`.led-cols`/`.led-col`、既存トークン流用）+ **「🌈 LED の明るさ」スライダー**（`sc-led-bright` → `/control/led_brightness`）。

---

## 構成図

```
[手かざし] → LTR-553 → ProximityPollTick (100ms, debounce×3, cooldown 5s)
                          │  ※ listening 中は cooldown バイパス
                          ▼
                    HandleProximity(prox_mode_)
                      ├ reflex → 首上げ + happy（ファーム自律）
                      ├ listen → トグル: StartListening / StopListening(送信)
                      └ off    → 無反応

[dashboard select/slider]
   → status_api:8080 → gateway:8767
        /control/proximity   → set_proximity_config(mode, threshold) → NVS "stackchan_prox"
        /control/led_brightness → set_led_brightness → _scale_rgb で全LED送信に係数
```

## 用語

- **ps_raw**: LTR-553 の近接生値（0..2047）。物体が近いほど大きい。baseline ~380、手かざし10cm ~820-1277。検出は `ps_raw ≥ threshold`。
- **cooldown**: 反応の連続発火抑制（5秒）。トグル listen では listening 中だけバイパスし、2回目のかざし（停止）を常に許可。
- **LED brightness**: firmware に輝度概念が無いため、gateway 側で r/g/b に 0..100% の係数を掛ける方式（flash 不要）。idle/listening/hermes 全スロットに共通適用。

## 検証

- gateway: **pytest 798 passed / ruff clean**（近接 mode + LED 明るさの新規テスト計+5）。
- firmware: Docker `idf:v5.5.2` build **warning 0 / `v2.2.6_stackchan.zip` 生成**。
- flash: app のみ（`0x20000`）、`Hash verified`、NVS 保持。
- 実機 E2E（ユーザー確認「全体的にいい感じ」✅）:
  - migration 成功: `/control/status` で `proximity.mode = "listen"`（旧 `enabled=true` から移行）、`threshold=824` 保持。
  - LED `brightness=100` 露出（gateway 新コード反映）。
  - 近接トグル listen / reflex / off、LED 明るさ・横並び、tap/背面なで回帰、すべて良好。

## 注記・積み残し

- 近接 **threshold は 824 のまま**（NVS 保持値、手かざし下限ギリギリでやや渋め）。反応を上げたければ dashboard で **600〜700** 推奨（baseline 380・通行人帯 448 より十分上、手かざし 820+ を確実に拾う）。ユーザーが「いい感じ」と判断したため変更せず。
- dashboard の旧 `.led-slot` CSS は孤児化（横並びで不使用）。フェーズ5の UI 仕上げで整理予定。
- **learning-report は全5フェーズ完了後（フェーズ5末）に1本**作成予定。本フェーズは worklog のみ。
- 次: フェーズ4（サーバタブに Codex 利用率 + Gemini API 利用額）。
