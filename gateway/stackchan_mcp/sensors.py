"""Port A I2C sensor reads for the yorishiro sensor tab (道A).

yorishiro fork specific module (not intended for upstream PR).

Two Grove sensors sit behind a PaHUB2 (PCA9548A) I2C multiplexer on
**Port A** so the dashboard can show, live, *what they sense* while
Kenji walks around the room — the raw material for deciding how
StackChan should react later (presence gating, gesture reflexes):

    PaHUB2 mux @0x70
      ├─ ch3 → TMOS PIR  (STHS34PF80 @0x5A) — presence/motion/temp
      └─ ch2 → Gesture   (PAJ7620U2 @0x73) — 9-way hand gestures

The reads go through the same MCP ``i2c_*`` tools that POST /control/i2c
exposes (firmware Port A bus, 400 kHz). This module is pure logic: it
takes a ``dispatch(name, arguments)`` coroutine — wired to
``_dispatch_mcp_tool(..., gateway)`` by the HTTP layer — and returns
JSON-serializable dicts. No HTTP, no gateway import (avoids a cycle).

TMOS register map / coherent 16-bit reads / presence-vs-motion gating
mirror the field-verified ``scratch/tmos_probe.py``. The PAJ7620
gesture-mode init array has no firmware/library support in this repo;
it is ported from the public RevEng_PAJ7620 driver and is expected to
need on-device tuning (gesture direction depends on mount orientation).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

# A dispatch coroutine: (logical tool name, arguments) -> device content
# list. The HTTP layer passes ``lambda n, a: _dispatch_mcp_tool(n, a, gateway)``.
DispatchFn = Callable[[str, dict[str, Any]], Awaitable[list[Any]]]

#: Serialises Port A I2C transactions. The dashboard sensor tab
#: (GET /control/sensors) and the presence monitor both read TMOS through
#: this module; the control plane bypasses the single-flight device queue,
#: so two reads can interleave on the bus. Without a lock a mux
#: channel-select from one read lands between another read's select and
#: its register fetch — tearing the bytes (reading ch2's data as ch3's,
#: etc.). Held across each *single-sensor* read/init so a select and its
#: follow-up reads are atomic. ``read_all`` / ``init_all`` do not take it
#: directly: they call the per-sensor functions, which do (no re-entrancy).
_i2c_lock = asyncio.Lock()

# ---- I2C topology -----------------------------------------------------
MUX_ADDR = 0x70  # PCA9548A (PaHUB2). Channel select = write [1 << ch].
TMOS_ADDR = 0x5A
TMOS_CH = 3
GESTURE_ADDR = 0x73
GESTURE_CH = 2

# ---- TMOS PIR (STHS34PF80) register map (subset; see tmos_probe.py) ----
TMOS_WHO_AM_I = 0x0F
TMOS_WHO_AM_I_EXPECTED = 0xD3
TMOS_CTRL1 = 0x20  # bits[3:0]=ODR, bit4=BDU
TMOS_FUNC_STATUS = 0x25  # bit2=PRES_FLAG, bit1=MOT_FLAG, bit0=TAMB_SHOCK_FLAG
TMOS_TOBJECT_L = 0x26  # int16 raw object IR
TMOS_TAMBIENT_L = 0x28  # int16 ambient, LSB = 1/100 degC
TMOS_TPRESENCE_L = 0x3A  # int16 presence value (vs PRESENCE_THS, default 200)
TMOS_TMOTION_L = 0x3C  # int16 motion value (vs MOTION_THS, default 200)
TMOS_ODR_CODE_4HZ = 0x05
# CTRL1 = BDU (bit4) on for coherent L/H + ODR code. Default embedded
# presence/motion thresholds (200) left untouched.
TMOS_CTRL1_VALUE = 0x10 | TMOS_ODR_CODE_4HZ
# Presence is the occupancy signal (motion false-fires while empty); a
# room-scale presence steadily reads ~770 vs ~0 empty, threshold 200.
PRESENCE_THRESHOLD = 200

# ---- Gesture unit (PAJ7620U2) -----------------------------------------
GESTURE_BANK_SEL = 0xEF
GESTURE_BANK0 = 0x00
GESTURE_PART_ID_L = 0x00  # bank0: 0x00=LSB(0x20), 0x01=MSB(0x76) -> 0x7620
GESTURE_PART_ID_EXPECTED = 0x7620
GESTURE_RESULT_0 = 0x43  # bit flags below
GESTURE_RESULT_1 = 0x44  # bit0 = wave

# GES_RESULT_0 bit -> name. Direction depends on physical mount; verify
# and remap on-device if up/down or left/right come out swapped.
GESTURE_BITS: tuple[tuple[int, str], ...] = (
    (0x01, "up"),
    (0x02, "down"),
    (0x04, "left"),
    (0x08, "right"),
    (0x10, "forward"),
    (0x20, "backward"),
    (0x40, "clockwise"),
    (0x80, "counterclockwise"),
)
GESTURE_WAVE_BIT = 0x01  # in GES_RESULT_1

# Gesture-mode initialization, ported verbatim from RevEng_PAJ7620's
# initRegisterArray (PAJ7620U2 v0.8 docs). Each entry is (register,
# value); 0xEF selects the register bank. The trailing bank0 / 0x41=0xFF
# / 0x42=0x01 enables the gesture interrupt flags. Kept at module top so
# it is easy to tweak during bring-up.
GESTURE_INIT_REGISTERS: tuple[tuple[int, int], ...] = (
    (0xEF, 0x00), (0x41, 0x00), (0x42, 0x00), (0x37, 0x07),
    (0x38, 0x17), (0x39, 0x06), (0x42, 0x01), (0x46, 0x2D),
    (0x47, 0x0F), (0x48, 0x3C), (0x49, 0x00), (0x4A, 0x1E),
    (0x4C, 0x22), (0x51, 0x10), (0x5E, 0x10), (0x60, 0x27),
    (0x80, 0x42), (0x81, 0x44), (0x82, 0x04), (0x8B, 0x01),
    (0x90, 0x06), (0x95, 0x0A), (0x96, 0x0C), (0x97, 0x05),
    (0x9A, 0x14), (0x9C, 0x3F), (0xA5, 0x19), (0xCC, 0x19),
    (0xCD, 0x0B), (0xCE, 0x13), (0xCF, 0x64), (0xD0, 0x21),
    (0xEF, 0x01), (0x02, 0x0F), (0x03, 0x10), (0x04, 0x02),
    (0x25, 0x01), (0x27, 0x39), (0x28, 0x7F), (0x29, 0x08),
    (0x3E, 0xFF), (0x5E, 0x3D), (0x65, 0x96), (0x67, 0x97),
    (0x69, 0xCD), (0x6A, 0x01), (0x6D, 0x2C), (0x6E, 0x01),
    (0x72, 0x01), (0x73, 0x35), (0x74, 0x00), (0x77, 0x01),
    (0xEF, 0x00), (0x41, 0xFF), (0x42, 0x01),
)


class SensorError(RuntimeError):
    """An I2C transaction to a Port A sensor failed or returned junk."""


# ---- device payload helpers -------------------------------------------


def _payload(content: list[Any]) -> Any:
    """Extract the JSON payload (or raw text) from a device tool result.

    Mirrors ``http_server._device_tool_payload`` but kept local to avoid
    importing the HTTP module (which imports this one).
    """
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text
    return None


def _s16(lo: int, hi: int) -> int:
    """Two little-endian bytes -> signed 16-bit int."""
    val = (hi << 8) | lo
    return val - 0x10000 if val >= 0x8000 else val


# ---- async I2C primitives (dispatch-injected) -------------------------


async def _write(dispatch: DispatchFn, addr: int, data: list[int]) -> None:
    payload = _payload(await dispatch("i2c_write", {"addr": addr, "bytes": data}))
    if isinstance(payload, dict) and "error" in payload:
        raise SensorError(str(payload["error"]))


async def _read(
    dispatch: DispatchFn, addr: int, reg: int, n: int
) -> list[int]:
    """Coherent register read: set pointer ``reg`` then read ``n`` bytes in
    ONE transaction (auto-increment). Splitting L/H across transactions
    tears across the sensor's ODR update and yields garbage."""
    payload = _payload(
        await dispatch(
            "i2c_write_read",
            {"addr": addr, "write_bytes": [reg], "n_bytes": n},
        )
    )
    if isinstance(payload, dict):
        if "error" in payload:
            raise SensorError(str(payload["error"]))
        data = payload.get("bytes")
        if isinstance(data, list) and len(data) >= n:
            return [int(b) for b in data[:n]]
    raise SensorError(f"unexpected read payload from 0x{addr:02X}: {payload!r}")


async def _mux_select(dispatch: DispatchFn, ch: int) -> None:
    """Select a PCA9548A channel (bitmask). Re-asserted each poll for
    robustness, per tmos_probe.py."""
    await _write(dispatch, MUX_ADDR, [1 << ch])


# ---- TMOS PIR ---------------------------------------------------------


async def init_tmos(dispatch: DispatchFn) -> dict[str, Any]:
    """Enable the embedded presence/motion algorithm (ODR 4 Hz, BDU)."""
    async with _i2c_lock:
        await _mux_select(dispatch, TMOS_CH)
        who = (await _read(dispatch, TMOS_ADDR, TMOS_WHO_AM_I, 1))[0]
        await _write(dispatch, TMOS_ADDR, [TMOS_CTRL1, TMOS_CTRL1_VALUE])
        return {"ok": who == TMOS_WHO_AM_I_EXPECTED, "who_am_i": who}


async def read_tmos(dispatch: DispatchFn) -> dict[str, Any]:
    """Live presence/motion/temperature snapshot. Occupancy = presence."""
    async with _i2c_lock:
        await _mux_select(dispatch, TMOS_CH)
        fstat = (await _read(dispatch, TMOS_ADDR, TMOS_FUNC_STATUS, 1))[0]
        pres_flag = bool((fstat >> 2) & 1)
        mot_flag = bool((fstat >> 1) & 1)
        shk_flag = bool(fstat & 1)
        presence = _s16(*await _read(dispatch, TMOS_ADDR, TMOS_TPRESENCE_L, 2))
        motion = _s16(*await _read(dispatch, TMOS_ADDR, TMOS_TMOTION_L, 2))
        obj = _s16(*await _read(dispatch, TMOS_ADDR, TMOS_TOBJECT_L, 2))
        amb = _s16(*await _read(dispatch, TMOS_ADDR, TMOS_TAMBIENT_L, 2))
    return {
        "present": pres_flag or presence > PRESENCE_THRESHOLD,
        "presence": presence,
        "motion": motion,
        "pres_flag": pres_flag,
        "mot_flag": mot_flag,
        "shk_flag": shk_flag,
        "object_raw": obj,
        "ambient_c": round(amb / 100.0, 2),
    }


# ---- Gesture unit -----------------------------------------------------


def _decode_gesture(raw0: int, raw1: int) -> str | None:
    for bit, name in GESTURE_BITS:
        if raw0 & bit:
            return name
    if raw1 & GESTURE_WAVE_BIT:
        return "wave"
    return None


async def init_gesture(dispatch: DispatchFn) -> dict[str, Any]:
    """Wake the PAJ7620 (it NACKs the first access while asleep), confirm
    the part id, then write the gesture-mode init array."""
    async with _i2c_lock:
        await _mux_select(dispatch, GESTURE_CH)
        part_id: int | None = None
        for _ in range(2):  # first read may NACK (sleep); second wakes + reads
            try:
                await _write(dispatch, GESTURE_ADDR, [GESTURE_BANK_SEL, GESTURE_BANK0])
                lo, hi = await _read(dispatch, GESTURE_ADDR, GESTURE_PART_ID_L, 2)
                part_id = (hi << 8) | lo
                break
            except SensorError:
                continue
        for reg, val in GESTURE_INIT_REGISTERS:
            await _write(dispatch, GESTURE_ADDR, [reg, val])
        return {"ok": part_id == GESTURE_PART_ID_EXPECTED, "part_id": part_id}


async def read_gesture(dispatch: DispatchFn) -> dict[str, Any]:
    """Read the latched gesture flags (reading clears them)."""
    async with _i2c_lock:
        await _mux_select(dispatch, GESTURE_CH)
        await _write(dispatch, GESTURE_ADDR, [GESTURE_BANK_SEL, GESTURE_BANK0])
        raw0, raw1 = await _read(dispatch, GESTURE_ADDR, GESTURE_RESULT_0, 2)
    return {"gesture": _decode_gesture(raw0, raw1), "raw0": raw0, "raw1": raw1}


# ---- combined read / init ---------------------------------------------


async def init_all(dispatch: DispatchFn) -> dict[str, Any]:
    """Initialize both sensors. Errors are surfaced per sensor so one
    failing unit does not block the other."""
    out: dict[str, Any] = {}
    try:
        out["tmos"] = await init_tmos(dispatch)
    except SensorError as exc:
        out["tmos"] = {"ok": False, "error": str(exc)}
    try:
        out["gesture"] = await init_gesture(dispatch)
    except SensorError as exc:
        out["gesture"] = {"ok": False, "error": str(exc)}
    return out


async def read_all(dispatch: DispatchFn) -> dict[str, Any]:
    """Live snapshot of both sensors; per-sensor error on partial failure
    (a NACK on one sensor must not drop the other's reading)."""
    out: dict[str, Any] = {}
    try:
        out["tmos"] = await read_tmos(dispatch)
    except SensorError as exc:
        out["tmos"] = {"error": str(exc)}
    try:
        out["gesture"] = await read_gesture(dispatch)
    except SensorError as exc:
        out["gesture"] = {"error": str(exc)}
    return out
