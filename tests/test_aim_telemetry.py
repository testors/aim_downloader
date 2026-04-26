from __future__ import annotations

import struct
import unittest
import zlib

from aim_telemetry import (
    build_session,
    compute_time_origin_ms,
    decode_session_bytes,
    repair_gps_timecodes,
)


def wrap_frame(tag: str, payload: bytes, cls: int = 0x00) -> bytes:
    tag_bytes = tag.encode("ascii").ljust(4, b"\0")
    checksum = (sum(payload) & 0xFFFF).to_bytes(2, "little")
    return b"<h" + tag_bytes + len(payload).to_bytes(4, "little") + bytes((cls,)) + b">" + payload + b"<" + tag_bytes + checksum + b">"


def make_chs(
    cid: int,
    *,
    short_name: str,
    long_name: str,
    sample_period_us: int,
    data_size: int,
    decoder_type: int,
    unit_type_byte: int = 0,
    display_format: int = 0,
    config_flags: int = 0,
    source_type: int = 0,
    hardware_id: int = 0,
    source_channel_id: int = 0,
    hardware_ref: int = 0,
) -> bytes:
    payload = bytearray(112)
    struct.pack_into("<H", payload, 0, cid)
    struct.pack_into("<H", payload, 4, hardware_id)
    struct.pack_into("<H", payload, 6, source_channel_id)
    struct.pack_into("<I", payload, 8, hardware_ref)
    payload[12] = unit_type_byte
    payload[13] = display_format
    struct.pack_into("<H", payload, 14, config_flags)
    payload[16] = source_type
    payload[20] = decoder_type
    payload[24:32] = short_name.encode("ascii").ljust(8, b"\0")
    payload[32:56] = long_name.encode("ascii").ljust(24, b"\0")
    struct.pack_into("<I", payload, 64, sample_period_us)
    payload[72] = data_size
    return wrap_frame("CHS", bytes(payload))


def s_record(tick: int, cid: int, data: bytes) -> bytes:
    return b"(S" + struct.pack("<IH", tick, cid) + data + b")"


def c_record_v1(channel_field: int, tick: int, data: bytes) -> bytes:
    return b"(c" + bytes((0x00,)) + struct.pack("<H", channel_field) + b"\x84\x06" + struct.pack("<I", tick) + data + b")"


def c_record_v2(channel_field: int, tick: int, raw0: int, raw1: int) -> bytes:
    return b"(c" + bytes((0x00,)) + struct.pack("<H", channel_field) + b"\x84\x08" + struct.pack("<I", tick) + struct.pack("<HH", raw0, raw1) + b")"


def c_record_v3(channel_field: int, raw0: int) -> bytes:
    return b"(c" + bytes((0x01,)) + struct.pack("<H", channel_field) + b"\x84\x02" + struct.pack("<H", raw0) + b")"


def half(value: float) -> int:
    return struct.unpack("<H", struct.pack("<e", value))[0]


class AimTelemetryTests(unittest.TestCase):
    def test_decode_session_bytes_salvages_truncated_zlib(self) -> None:
        original = b"telemetry-block-" * 256
        compressed = zlib.compress(original)
        truncated = compressed[:-3]
        recovered = decode_session_bytes(truncated, source="truncated.xrz", compressed=True)
        self.assertTrue(recovered)
        self.assertTrue(original.startswith(recovered))

    def test_repair_gps_timecodes_fixes_65533_gap(self) -> None:
        gps_frames = [
            {"tick": 1000.0},
            {"tick": 1040.0},
            {"tick": 66573.0},
            {"tick": 66613.0},
        ]
        gnfi_ticks = [1000, 1040, 1080, 1120]
        self.assertTrue(repair_gps_timecodes(gps_frames, gnfi_ticks))
        self.assertEqual([int(frame["tick"]) for frame in gps_frames], [1000, 1040, 1080, 1120])

    def test_compute_time_origin_prefers_first_lap(self) -> None:
        from aim_telemetry import LapInfo

        channel_samples = {
            0: [(17000, 17000.0), (18000, 18000.0)],
            12: [(17100, 1.0)],
        }
        laps = [LapInfo(segment=0, lap_num=1, duration_ms=5000, end_time_ms=21000)]
        self.assertEqual(compute_time_origin_ms(channel_samples, laps), 16000)

    def test_build_session_decodes_expansion_messages_and_metadata(self) -> None:
        cnf_payload = b"".join(
            [
                make_chs(
                    0,
                    short_name="MClk",
                    long_name="Master Clk",
                    sample_period_us=2000,
                    data_size=4,
                    decoder_type=0,
                    display_format=21,
                    unit_type_byte=0x12,
                ),
                make_chs(
                    12,
                    short_name="IBat",
                    long_name="Internal Battery",
                    sample_period_us=1000000,
                    data_size=2,
                    decoder_type=1,
                    unit_type_byte=0x95,
                    display_format=1,
                ),
                make_chs(
                    32,
                    short_name="LEGACY",
                    long_name="Legacy Exp",
                    sample_period_us=2000,
                    data_size=2,
                    decoder_type=20,
                ),
                make_chs(
                    100,
                    short_name="SHOCK",
                    long_name="Shock Pot",
                    sample_period_us=2000,
                    data_size=2,
                    decoder_type=20,
                    source_type=1,
                    hardware_id=1,
                    source_channel_id=7,
                    hardware_ref=11,
                ),
            ]
        )
        raw = bytearray(wrap_frame("CNF", cnf_payload))
        for tick in (1000, 1002, 1004, 1006, 1008):
            raw.extend(s_record(tick, 0, struct.pack("<I", tick)))
        raw.extend(s_record(1000, 12, struct.pack("<H", half(1234.0))))
        raw.extend(c_record_v1(32 << 3, 1002, struct.pack("<H", half(60.0))))
        raw.extend(c_record_v2(0x14, 1004, half(10.0), half(20.0)))
        raw.extend(c_record_v2(0x10, 1008, half(30.0), half(40.0)))
        raw.extend(c_record_v3(0x14, half(50.0)))

        session = build_session(bytes(raw))

        battery = session.channels[12]
        self.assertTrue(battery.calibrated)
        self.assertEqual(battery.units, "V")
        self.assertTrue(battery.interpolate)
        self.assertEqual(session.time_origin_ms, 1000)
        self.assertEqual(session.channel_samples[32], [(1002, 60.0)])
        self.assertEqual(
            session.channel_samples[100],
            [(1000, 20.0), (1002, 10.0), (1004, 40.0), (1006, 50.0), (1008, 30.0)],
        )


if __name__ == "__main__":
    unittest.main()
