"""Unit tests for the Port A sensor logic (sensors.py).

These exercise the pure read/decode/init logic with a fake ``dispatch``
coroutine (the same shape the HTTP layer wires to _dispatch_mcp_tool),
so no ESP32 or HTTP stack is involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from stackchan_mcp import sensors


def make_dispatch(
    reg_map: dict[tuple[int, int], list[int]] | None = None,
    *,
    fail_addr: int | None = None,
    calls: list[tuple[str, dict]] | None = None,
):
    """Build a fake dispatch returning programmable register bytes.

    ``reg_map`` is keyed by (i2c_addr, register). ``fail_addr`` makes
    every transaction to that address surface a device error.
    """
    reg_map = reg_map or {}

    async def dispatch(name: str, args: dict[str, Any]) -> list[Any]:
        if calls is not None:
            calls.append((name, dict(args)))
        addr = args.get("addr")
        if fail_addr is not None and addr == fail_addr:
            return [{"type": "text", "text": json.dumps({"error": "ESP_ERR_TIMEOUT"})}]
        if name == "i2c_write_read":
            reg = args["write_bytes"][0]
            data = reg_map.get((addr, reg), [0] * args["n_bytes"])
            return [{"type": "text", "text": json.dumps({"ok": True, "bytes": data})}]
        # i2c_write (and anything else) just acks.
        return [{"type": "text", "text": json.dumps({"ok": True})}]

    return dispatch


# ---- pure helpers -----------------------------------------------------


def test_s16_signed_conversion() -> None:
    assert sensors._s16(0xC8, 0x00) == 200
    assert sensors._s16(0x00, 0x80) == -32768
    assert sensors._s16(0xFF, 0x7F) == 32767
    assert sensors._s16(0x00, 0x00) == 0


def test_decode_gesture_maps_bits() -> None:
    assert sensors._decode_gesture(0x01, 0x00) == "up"
    assert sensors._decode_gesture(0x08, 0x00) == "right"
    assert sensors._decode_gesture(0x80, 0x00) == "counterclockwise"
    assert sensors._decode_gesture(0x00, 0x01) == "wave"
    assert sensors._decode_gesture(0x00, 0x00) is None


def test_init_array_is_complete_and_valid() -> None:
    # Ported verbatim from RevEng_PAJ7620; guard against a truncation typo.
    assert len(sensors.GESTURE_INIT_REGISTERS) == 55
    for reg, val in sensors.GESTURE_INIT_REGISTERS:
        assert 0 <= reg <= 0xFF
        assert 0 <= val <= 0xFF
    # First entry selects bank 0; last enables the gesture interrupt flag.
    assert sensors.GESTURE_INIT_REGISTERS[0] == (0xEF, 0x00)
    assert sensors.GESTURE_INIT_REGISTERS[-1] == (0x42, 0x01)


# ---- TMOS -------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tmos_present_by_flag() -> None:
    # PRES flag (bit2) set -> present even if the presence value is low.
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x04],
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x0A, 0x00],  # 10
        (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TAMBIENT_L): [0xB8, 0x0B],  # 3000 -> 30.00 C
    }
    out = await sensors.read_tmos(make_dispatch(reg_map))
    assert out["present"] is True
    assert out["pres_flag"] is True
    assert out["presence"] == 10
    assert out["ambient_c"] == 30.0


@pytest.mark.asyncio
async def test_read_tmos_present_by_presence_ignores_motion() -> None:
    # No PRES flag, presence > 200 -> present. High motion alone (with a
    # low presence) must NOT count as occupancy.
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x02],  # MOT flag only
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x2C, 0x01],  # 300
        (0x5A, sensors.TMOS_TMOTION_L): [0xF4, 0x01],  # 500
    }
    out = await sensors.read_tmos(make_dispatch(reg_map))
    assert out["present"] is True
    assert out["pres_flag"] is False
    assert out["mot_flag"] is True
    assert out["motion"] == 500


@pytest.mark.asyncio
async def test_read_tmos_empty_room_not_present() -> None:
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x02],  # motion fires while empty
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x0A, 0x00],  # 10
        (0x5A, sensors.TMOS_TMOTION_L): [0xF4, 0x01],  # 500 (ignored)
    }
    out = await sensors.read_tmos(make_dispatch(reg_map))
    assert out["present"] is False


@pytest.mark.asyncio
async def test_read_tmos_selects_mux_ch3_first() -> None:
    calls: list[tuple[str, dict]] = []
    await sensors.read_tmos(make_dispatch(calls=calls))
    assert calls[0] == ("i2c_write", {"addr": 0x70, "bytes": [1 << 3]})


@pytest.mark.asyncio
async def test_init_tmos_writes_ctrl1_and_checks_who_am_i() -> None:
    calls: list[tuple[str, dict]] = []
    reg_map = {(0x5A, sensors.TMOS_WHO_AM_I): [0xD3]}
    out = await sensors.init_tmos(make_dispatch(reg_map, calls=calls))
    assert out == {"ok": True, "who_am_i": 0xD3}
    assert ("i2c_write", {"addr": 0x5A, "bytes": [0x20, 0x15]}) in calls


# ---- Gesture ----------------------------------------------------------


@pytest.mark.asyncio
async def test_read_gesture_decodes_and_selects_ch2() -> None:
    calls: list[tuple[str, dict]] = []
    reg_map = {(0x73, sensors.GESTURE_RESULT_0): [0x08, 0x00]}  # right
    out = await sensors.read_gesture(make_dispatch(reg_map, calls=calls))
    assert out["gesture"] == "right"
    assert calls[0] == ("i2c_write", {"addr": 0x70, "bytes": [1 << 2]})
    # bank-0 select precedes the result read.
    assert ("i2c_write", {"addr": 0x73, "bytes": [0xEF, 0x00]}) in calls


@pytest.mark.asyncio
async def test_init_gesture_writes_full_array_and_part_id() -> None:
    calls: list[tuple[str, dict]] = []
    # part id 0x7620 = LSB 0x20, MSB 0x76 (auto-increment from reg 0x00).
    reg_map = {(0x73, sensors.GESTURE_PART_ID_L): [0x20, 0x76]}
    out = await sensors.init_gesture(make_dispatch(reg_map, calls=calls))
    assert out["ok"] is True
    assert out["part_id"] == 0x7620
    init_writes = [
        tuple(a["bytes"]) for n, a in calls if n == "i2c_write" and a["addr"] == 0x73
    ]
    for reg, val in sensors.GESTURE_INIT_REGISTERS:
        assert (reg, val) in init_writes


# ---- combined ---------------------------------------------------------


@pytest.mark.asyncio
async def test_read_all_surfaces_partial_gesture_error() -> None:
    reg_map = {
        (0x5A, sensors.TMOS_FUNC_STATUS): [0x04],
        (0x5A, sensors.TMOS_TPRESENCE_L): [0x00, 0x03],
        (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
        (0x5A, sensors.TMOS_TAMBIENT_L): [0x00, 0x00],
    }
    out = await sensors.read_all(make_dispatch(reg_map, fail_addr=0x73))
    assert out["tmos"]["present"] is True
    assert "error" in out["gesture"]
