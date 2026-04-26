from __future__ import annotations

import bisect
import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path


WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
G_STD = 9.80665

LINEAR_CHANNEL_IDS = {12, 13, 33, 34, 39, 44, 45, 47, 50}
CHANNEL_SCALE = {44: 0.001}

UNIT_MAP = {
    1: ("%", 2),
    3: ("g", 2),
    4: ("deg", 1),
    5: ("deg/s", 1),
    6: ("", 0),
    9: ("Hz", 0),
    11: ("", 0),
    12: ("mm", 0),
    14: ("bar", 2),
    15: ("rpm", 0),
    16: ("km/h", 0),
    17: ("C", 1),
    18: ("ms", 0),
    19: ("Nm", 0),
    20: ("km/h", 0),
    21: ("mV", 1),
    22: ("l", 1),
    24: ("l/s", 0),
    26: ("time?", 0),
    27: ("A", 0),
    30: ("lambda", 2),
    31: ("gear", 0),
    33: ("%", 2),
    43: ("kg", 3),
}

DECODER_TABLE = {
    0: ("i", False),
    1: ("H", True),
    3: ("i", False),
    4: ("h", False),
    6: ("f", True),
    8: ("i", False),
    11: ("h", False),
    12: ("i", False),
    13: ("B", False),
    15: ("H", False),
    20: ("H", True),
    22: ("i", False),
    24: ("i", False),
    26: ("i", False),
    27: ("i", False),
    31: ("i", False),
    32: ("i", False),
    33: ("i", False),
    37: ("i", False),
    38: ("i", False),
    39: ("i", False),
}

FUNCTION_MAP = {
    (0, 0x01): "Percent",
    (0, 0x03): "Acceleration",
    (0, 0x04): "Angle",
    (0, 0x05): "Angular Rate",
    (0, 0x0B): "Number",
    (0, 0x0C): "Distance",
    (0, 0x0E): "Pressure",
    (0, 0x0F): "Engine RPM",
    (0, 0x10): "Rear Wheel Speed",
    (0, 0x11): "Temperature",
    (0, 0x12): "Time",
    (0, 0x15): "Voltage",
    (0, 0x91): "Exhaust Temperature",
    (0, 0x9A): "Lap Time",
    (1, 0x95): "Battery Voltage",
    (2, 0x9A): "Total Odometer",
    (3, 0x1A): "Reset Odometer",
    (5, 0x1A): "Best Lap Time",
    (5, 0x9A): "Rolling Lap Time",
    (6, 0x06): "Gear",
    (6, 0x1F): "Gear",
    (9, 0x1E): "Lambda",
    (11, 0x91): "Oil Temperature",
    (13, 0x84): "Steering Angle",
    (14, 0x81): "Percentage Throttle Load",
    (16, 0x91): "Water Temperature",
    (17, 0x03): "Inline Acceleration",
    (17, 0x05): "Roll Rate",
    (17, 0x83): "Lateral Acceleration",
    (17, 0x85): "Pitch Rate",
    (18, 0x03): "Vertical Acceleration",
    (18, 0x05): "Yaw Rate",
    (21, 0x12): "Master Clock",
    (26, 0x21): "Device Brightness",
    (27, 0x92): "Best Run Diff",
    (28, 0x12): "Prev Lap Diff",
    (28, 0x92): "Ref Lap Diff",
    (35, 0x92): "Best Today Diff",
    (128, 0x10): "Vehicle Speed",
    (128, 0x91): "Intake Air Temperature",
    (128, 0x9A): "Lap Time",
    (129, 0x9A): "GPS Time",
    (130, 0x0E): "Brake Circuit Pressure",
    (144, 0x91): "Water Temperature",
    (145, 0x03): "Inline Acceleration",
    (145, 0x83): "Lateral Acceleration",
    (146, 0x05): "Yaw Rate",
    (169, 0x8C): "LF Shock Position",
}

FUNCTION_MAP_OVERRIDE = {
    (0, 0x11, 1): "Device Temperature",
}

SIGNED_INT32_DECODERS = {0, 3, 8, 12, 22, 24, 26, 27, 31, 32, 33, 37, 38, 39}


@dataclass
class Frame:
    tag: str
    length: int
    cls: int
    payload_start: int
    next_offset: int


@dataclass
class ChannelInfo:
    cid: int
    short_name: str
    long_name: str
    period_us: int
    data_size: int
    hardware_id: int
    source_channel_id: int
    hardware_ref: int
    unit_type: int
    calibrated: bool
    units: str
    dec_pts: int
    display_format: int
    config_flags: int
    source_type: int
    decoder_type: int
    interpolate: bool
    function: str
    device_tag: str
    cal_value_1: float
    cal_value_2: float
    display_range_min: float
    display_range_max: float


@dataclass
class LapInfo:
    segment: int
    lap_num: int
    duration_ms: int
    end_time_ms: int


@dataclass
class TrackInfo:
    name: str
    sf_lat: float
    sf_lon: float


@dataclass
class PendingExpansionMessage:
    variant: str
    channel_field: int
    timecode: int | None
    data: bytes


@dataclass
class SessionData:
    channels: dict[int, ChannelInfo]
    groups: dict[int, list[int]]
    channel_samples: dict[int, list[tuple[int, float]]]
    gps_frames: list[dict[str, float]]
    timeline: list[int]
    time_origin_ms: int
    laps: list[LapInfo] = field(default_factory=list)
    track: TrackInfo | None = None
    warnings: list[str] = field(default_factory=list)
    gps_timing_fixed: bool = False


class NumericResampler:
    def __init__(self, points: list[tuple[int, float]]) -> None:
        self.points = collapse_points(points)
        self.ticks = [t for t, _ in self.points]
        self.values = [v for _, v in self.points]

    def linear(self, tick: int) -> float | None:
        if not self.points:
            return None
        if tick <= self.ticks[0]:
            return self.values[0]
        if tick >= self.ticks[-1]:
            return self.values[-1]
        idx = bisect.bisect_right(self.ticks, tick) - 1
        t0 = self.ticks[idx]
        t1 = self.ticks[idx + 1]
        if t1 == t0:
            return self.values[idx]
        v0 = self.values[idx]
        v1 = self.values[idx + 1]
        frac = (tick - t0) / (t1 - t0)
        return v0 * (1.0 - frac) + v1 * frac

    def step(self, tick: int) -> float | None:
        if not self.points:
            return None
        idx = bisect.bisect_right(self.ticks, tick) - 1
        if idx < 0:
            return self.values[0]
        return self.values[idx]


def looks_like_zlib(data: bytes) -> bool:
    if len(data) < 2:
        return False
    cmf, flg = data[0], data[1]
    if (cmf & 0x0F) != 0x08:
        return False
    return ((cmf << 8) | flg) % 31 == 0


def normalize_input_format(path: Path, data: bytes, requested: str) -> str:
    if requested != "auto":
        return requested

    suffix = path.suffix.lower()
    if suffix == ".xrk":
        raise ValueError("input already looks like .xrk; use .xrz/.raw or override --input-format")
    if suffix == ".xrz":
        return "xrz"
    if suffix == ".raw":
        return "raw"
    if looks_like_zlib(data):
        return "xrz"
    return "raw"


def _nullterm(data: bytes, encoding: str = "ascii") -> str:
    zero = data.find(0)
    if zero >= 0:
        data = data[:zero]
    return data.decode(encoding, errors="replace")


def _resolve_units(unit_type_byte: int) -> tuple[str, int]:
    base_unit, dec_pts = UNIT_MAP.get(unit_type_byte & 0x7F, ("", 0))
    if unit_type_byte & 0x80 and base_unit == "mV":
        return "V", dec_pts
    return base_unit, dec_pts


def _resolve_function(display_format: int, unit_type_byte: int, config_flags: int) -> str:
    override = FUNCTION_MAP_OVERRIDE.get((display_format, unit_type_byte, config_flags))
    if override is not None:
        return override
    return FUNCTION_MAP.get((display_format, unit_type_byte), "")


def _decompress_zlib_bytes(data: bytes, source: str) -> bytes:
    deco = zlib.decompressobj()
    out = bytearray()
    for start in range(0, len(data), 8192):
        chunk = data[start:start + 8192]
        try:
            out.extend(deco.decompress(chunk))
        except zlib.error as exc:
            out.extend(deco.flush())
            if out:
                return bytes(out)
            raise ValueError(f"failed to zlib-decompress {source}: {exc}") from exc
    try:
        out.extend(deco.flush())
    except zlib.error as exc:
        if out:
            return bytes(out)
        raise ValueError(f"failed to zlib-decompress {source}: {exc}") from exc
    return bytes(out)


def decode_session_bytes(
    data: bytes, *, source: str = "session", compressed: bool | None = None
) -> bytes:
    if compressed is None:
        compressed = looks_like_zlib(data)
    if not compressed:
        return data
    return _decompress_zlib_bytes(data, source)


def read_raw_bytes(path: Path, input_format: str) -> tuple[bytes, str]:
    data = path.read_bytes()
    mode = normalize_input_format(path, data, input_format)
    return decode_session_bytes(data, source=str(path), compressed=(mode != "raw")), mode


def read_session_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    return decode_session_bytes(data, source=str(path), compressed=(suffix == ".xrz" or looks_like_zlib(data)))


def parse_frame(buf: bytes, offset: int) -> Frame | None:
    if offset + 20 > len(buf) or buf[offset:offset + 2] != b"<h":
        return None
    tag = buf[offset + 2:offset + 6]
    length = struct.unpack_from("<I", buf, offset + 6)[0]
    cls = buf[offset + 10]
    if buf[offset + 11] != 0x3E:
        return None
    end = offset + 12 + length
    if end + 8 > len(buf):
        return None
    if buf[end] != 0x3C or buf[end + 1:end + 5] != tag or buf[end + 7] != 0x3E:
        return None
    payload = buf[offset + 12:end]
    checksum = struct.unpack_from("<H", buf, end + 5)[0]
    if (sum(payload) & 0xFFFF) != checksum:
        return None
    return Frame(
        tag=tag.decode("ascii", "replace").rstrip("\x00"),
        length=length,
        cls=cls,
        payload_start=offset + 12,
        next_offset=end + 8,
    )


def collapse_points(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    if not points:
        return []
    points.sort()
    collapsed: list[tuple[int, float]] = []
    current_tick = points[0][0]
    current_value = points[0][1]
    for tick, value in points[1:]:
        if tick == current_tick:
            current_value = value
            continue
        collapsed.append((current_tick, current_value))
        current_tick = tick
        current_value = value
    collapsed.append((current_tick, current_value))
    return collapsed


def half_float_from_u16(raw16: int) -> float:
    return struct.unpack("<e", struct.pack("<H", raw16))[0]


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(6):
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + alt)))
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt


def ecef_velocity_to_enu(
    lat_deg: float, lon_deg: float, vx: float, vy: float, vz: float
) -> tuple[float, float, float]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    ve = -math.sin(lon) * vx + math.cos(lon) * vy
    vn = (
        -math.sin(lat) * math.cos(lon) * vx
        - math.sin(lat) * math.sin(lon) * vy
        + math.cos(lat) * vz
    )
    vu = (
        math.cos(lat) * math.cos(lon) * vx
        + math.cos(lat) * math.sin(lon) * vy
        + math.sin(lat) * vz
    )
    return vn, ve, vu


def unwrap_angles(values: list[float]) -> list[float]:
    if not values:
        return []
    out = [values[0]]
    for value in values[1:]:
        prev = out[-1]
        delta = ((value - prev + 180.0) % 360.0) - 180.0
        out.append(prev + delta)
    return out


def scale_channel_value(channel: ChannelInfo | int, value: float | None) -> float | None:
    if value is None:
        return None
    cid = channel if isinstance(channel, int) else channel.cid
    if isinstance(channel, ChannelInfo) and channel.calibrated and channel.unit_type == 21:
        value /= 1000.0
    return value * CHANNEL_SCALE.get(cid, 1.0)


def _decode_sample_bytes(channel: ChannelInfo, data: bytes) -> float:
    if channel.data_size == 8 and len(data) >= 8:
        code = struct.unpack_from("<H", data)[0]
        label = _nullterm(data[2:8])
        if channel.cid == 24 and label.isdigit():
            return float(label)
        return float(code)

    decoder_type = channel.decoder_type
    if decoder_type in SIGNED_INT32_DECODERS:
        if channel.cid == 0:
            value = float(struct.unpack("<I", data)[0])
        else:
            value = float(struct.unpack("<i", data)[0])
    elif decoder_type in (1, 20):
        value = half_float_from_u16(struct.unpack("<H", data)[0])
    elif decoder_type in (4, 11):
        value = float(struct.unpack("<h", data)[0])
    elif decoder_type == 6:
        value = float(struct.unpack("<f", data)[0])
    elif decoder_type == 13:
        value = float(data[0])
    elif decoder_type == 15:
        value = float(struct.unpack("<H", data)[0])
    elif len(data) == 4:
        value = float(struct.unpack("<f", data)[0])
    elif len(data) == 2:
        value = float(struct.unpack("<H", data)[0])
    elif len(data) == 1:
        value = float(data[0])
    else:
        raise ValueError(f"unsupported sample decoder for channel {channel.cid}")
    scaled = scale_channel_value(channel, value)
    assert scaled is not None
    return scaled


def _decode_group_value(channel: ChannelInfo, data: bytes) -> float:
    if channel.data_size == 8:
        code = struct.unpack_from("<H", data)[0]
        label = _nullterm(data[2:8])
        if channel.cid == 24 and label.isdigit():
            return float(label)
        return float(code)
    return _decode_sample_bytes(channel, data)


def _parse_channel_info(inner_payload: bytes) -> ChannelInfo:
    cid = struct.unpack_from("<H", inner_payload, 0)[0]
    unit_type_byte = inner_payload[12]
    units, dec_pts = _resolve_units(unit_type_byte)
    display_format = inner_payload[13]
    config_flags = struct.unpack_from("<H", inner_payload, 14)[0]
    return ChannelInfo(
        cid=cid,
        short_name=_nullterm(inner_payload[24:32]),
        long_name=_nullterm(inner_payload[32:56]),
        period_us=struct.unpack_from("<I", inner_payload, 64)[0],
        data_size=inner_payload[72],
        hardware_id=struct.unpack_from("<H", inner_payload, 4)[0],
        source_channel_id=struct.unpack_from("<H", inner_payload, 6)[0],
        hardware_ref=struct.unpack_from("<I", inner_payload, 8)[0],
        unit_type=unit_type_byte & 0x7F,
        calibrated=bool(unit_type_byte & 0x80),
        units=units,
        dec_pts=dec_pts,
        display_format=display_format,
        config_flags=config_flags,
        source_type=inner_payload[16],
        decoder_type=inner_payload[20],
        interpolate=DECODER_TABLE.get(inner_payload[20], ("", False))[1],
        function=_resolve_function(display_format, unit_type_byte, config_flags),
        device_tag=_nullterm(inner_payload[76:80]),
        cal_value_1=struct.unpack_from("<f", inner_payload, 96)[0],
        cal_value_2=struct.unpack_from("<f", inner_payload, 100)[0],
        display_range_min=struct.unpack_from("<f", inner_payload, 104)[0],
        display_range_max=struct.unpack_from("<f", inner_payload, 108)[0],
    )


def _parse_lap_payload(payload: bytes) -> LapInfo | None:
    if len(payload) < 20:
        return None
    return LapInfo(
        segment=payload[1],
        lap_num=struct.unpack_from("<H", payload, 2)[0],
        duration_ms=struct.unpack_from("<I", payload, 4)[0],
        end_time_ms=struct.unpack_from("<I", payload, 16)[0],
    )


def _parse_track_payload(payload: bytes) -> TrackInfo | None:
    if len(payload) < 44:
        return None
    return TrackInfo(
        name=_nullterm(payload[:32]),
        sf_lat=struct.unpack_from("<i", payload, 36)[0] / 1e7,
        sf_lon=struct.unpack_from("<i", payload, 40)[0] / 1e7,
    )


def _append_gps_frame(gps_frames: list[dict[str, float]], payload: bytes) -> None:
    if len(payload) < 56:
        return
    words = list(struct.unpack("<14I", payload[:56]))

    def si(index: int) -> int:
        return struct.unpack("<i", struct.pack("<I", words[index]))[0]

    tick = words[0]
    x, y, z = [si(i) / 100.0 for i in (4, 5, 6)]
    lat, lon, alt = ecef_to_llh(x, y, z)
    vx, vy, vz = [si(i) / 100.0 for i in (8, 9, 10)]
    vn, ve, vu = ecef_velocity_to_enu(lat, lon, vx, vy, vz)
    speed_ms = math.hypot(vn, ve)
    gps_frames.append(
        {
            "tick": float(tick),
            "tow_ms": float(words[1]),
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "vn": vn,
            "ve": ve,
            "vu": vu,
            "speed_kmh": speed_ms * 3.6,
            "heading_deg": math.degrees(math.atan2(ve, vn)),
            "slope_deg": math.degrees(math.atan2(vu, speed_ms)) if speed_ms else 0.0,
            "pos_acc_mm": float(words[7] * 10),
            "spd_acc_kmh": float(words[11]) * 0.036,
            "nsat": float((words[12] >> 24) & 0xFF),
        }
    )


def _append_gnfi_tick(gnfi_ticks: list[int], payload: bytes) -> None:
    if len(payload) < 4:
        return
    gnfi_ticks.append(struct.unpack_from("<I", payload, 0)[0])


def _estimate_interval_ms(ticks: list[int], default: int) -> int:
    gaps = sorted(gap for gap in (b - a for a, b in zip(ticks, ticks[1:])) if 0 < gap < 1000)
    if not gaps:
        return default
    return max(1, gaps[len(gaps) // 2])


def _repair_timecode_wrap(ticks: list[int]) -> list[int]:
    base = ticks[0] - (ticks[0] & 0xFFFF)
    wraps = 0
    repaired = [base + (ticks[0] & 0xFFFF)]
    for tick in ticks[1:]:
        candidate = base + wraps * 65536 + (tick & 0xFFFF)
        if candidate < repaired[-1]:
            wraps += 1
            candidate = base + wraps * 65536 + (tick & 0xFFFF)
        repaired.append(candidate)
    return repaired


def _gps_repair_matches_gnfi(original: list[int], candidate: list[int], gnfi_ticks: list[int]) -> bool:
    if len(gnfi_ticks) < 2:
        return True
    gnfi_span = gnfi_ticks[-1] - gnfi_ticks[0]
    original_span = original[-1] - original[0]
    candidate_span = candidate[-1] - candidate[0]
    return abs(candidate_span - gnfi_span) <= abs(original_span - gnfi_span)


def repair_gps_timecodes(gps_frames: list[dict[str, float]], gnfi_ticks: list[int]) -> bool:
    if len(gps_frames) < 2:
        return False
    ticks = [int(frame["tick"]) for frame in gps_frames]
    gaps = [next_tick - tick for tick, next_tick in zip(ticks, ticks[1:])]
    expected_dt = _estimate_interval_ms(ticks, 40)

    if any(60000 <= gap <= 70000 for gap in gaps):
        repaired = [ticks[0]]
        offset = 0
        for gap, tick in zip(gaps, ticks[1:]):
            if 60000 <= gap <= 70000:
                offset += gap - expected_dt
            repaired.append(tick - offset)
        if _gps_repair_matches_gnfi(ticks, repaired, gnfi_ticks):
            for frame, tick in zip(gps_frames, repaired):
                frame["tick"] = float(tick)
            return True

    if any(next_tick < tick for tick, next_tick in zip(ticks, ticks[1:])):
        repaired = _repair_timecode_wrap(ticks)
        if _gps_repair_matches_gnfi(ticks, repaired, gnfi_ticks):
            for frame, tick in zip(gps_frames, repaired):
                frame["tick"] = float(tick)
            return True

    return False


def compute_time_origin_ms(
    channel_samples: dict[int, list[tuple[int, float]]], laps: list[LapInfo]
) -> int:
    for lap in laps:
        if lap.segment == 0:
            return lap.end_time_ms - lap.duration_ms
    first_ticks = [points[0][0] for points in channel_samples.values() if points]
    if not first_ticks:
        raise ValueError("No channel timeline found")
    return min(first_ticks)


def _parse_expansion_message(
    raw: bytes, current_offset: int, channels: dict[int, ChannelInfo]
) -> tuple[PendingExpansionMessage, int] | None:
    if current_offset + 10 > len(raw) or raw[current_offset:current_offset + 2] != b"(c":
        return None
    unk1 = raw[current_offset + 2]
    channel_field = struct.unpack_from("<H", raw, current_offset + 3)[0]
    unk3 = raw[current_offset + 5]
    unk4 = raw[current_offset + 6]
    if unk3 != 0x84:
        return None

    if unk1 == 0x00 and unk4 == 0x06:
        cid = channel_field >> 3
        channel = channels.get(cid)
        if channel is None:
            return None
        record_len = 12 + channel.data_size
        if current_offset + record_len > len(raw) or raw[current_offset + record_len - 1] != 0x29:
            return None
        return (
            PendingExpansionMessage(
                variant="V1",
                channel_field=channel_field,
                timecode=struct.unpack_from("<I", raw, current_offset + 7)[0],
                data=bytes(raw[current_offset + 11:current_offset + 11 + channel.data_size]),
            ),
            record_len,
        )

    if unk1 == 0x00 and unk4 == 0x08:
        record_len = 16
        if current_offset + record_len > len(raw) or raw[current_offset + record_len - 1] != 0x29:
            return None
        return (
            PendingExpansionMessage(
                variant="V2",
                channel_field=channel_field,
                timecode=struct.unpack_from("<I", raw, current_offset + 7)[0],
                data=bytes(raw[current_offset + 11:current_offset + 15]),
            ),
            record_len,
        )

    if unk1 == 0x01 and unk4 == 0x02:
        record_len = 10
        if current_offset + record_len > len(raw) or raw[current_offset + record_len - 1] != 0x29:
            return None
        return (
            PendingExpansionMessage(
                variant="V3",
                channel_field=channel_field,
                timecode=None,
                data=bytes(raw[current_offset + 7:current_offset + 9]),
            ),
            record_len,
        )

    return None


def _resolve_expansion_channel_map(
    channels: dict[int, ChannelInfo], pending_expansion: list[PendingExpansionMessage]
) -> dict[int, int]:
    v2_fields = {message.channel_field for message in pending_expansion if message.variant == "V2"}
    v3_fields = {message.channel_field for message in pending_expansion if message.variant == "V3"}
    if not (v2_fields or v3_fields):
        return {}

    pairs: list[tuple[int, int]] = []
    orphans: list[int] = []
    processed: set[int] = set()
    for channel_field in sorted(v2_fields | v3_fields):
        if channel_field in processed:
            continue
        low = channel_field & 0xF
        if low in (0x0, 0x8):
            partner = channel_field | 0x4
            if partner in v2_fields:
                pairs.append((channel_field, partner))
                processed.add(channel_field)
                processed.add(partner)
                continue
        elif low in (0x4, 0xC):
            partner = channel_field & ~0x4
            if partner in v2_fields:
                continue
        orphans.append(channel_field)
        processed.add(channel_field)

    paired_candidates: list[tuple[tuple[int, int], int]] = []
    orphan_candidates: list[tuple[tuple[int, int], int]] = []
    for cid, channel in channels.items():
        if channel.decoder_type == 20 and channel.source_type == 1 and channel.hardware_id != 0:
            key = (channel.hardware_ref, channel.source_channel_id)
            period_ms = channel.period_us // 1000
            if period_ms <= 5:
                paired_candidates.append((key, cid))
            elif 5 < period_ms <= 15:
                orphan_candidates.append((key, cid))
    paired_candidates.sort()
    orphan_candidates.sort()

    mapping: dict[int, int] = {}
    for (base, plus4), (_, cid) in zip(pairs, paired_candidates):
        mapping[base] = cid
        mapping[plus4] = cid
    for channel_field, (_, cid) in zip(orphans, orphan_candidates):
        mapping[channel_field] = cid
    return mapping


def _emit_expansion_samples(
    pending_expansion: list[PendingExpansionMessage],
    channels: dict[int, ChannelInfo],
    add_sample,
    warnings: list[str],
) -> None:
    if not pending_expansion:
        return

    expansion_channel_map = _resolve_expansion_channel_map(channels, pending_expansion)
    pair_fields: dict[int, tuple[int, int]] = {}
    for channel_field, cid in expansion_channel_map.items():
        partner = channel_field ^ 0x4
        if expansion_channel_map.get(partner) == cid:
            pair_fields[cid] = (min(channel_field, partner), max(channel_field, partner))

    last_v2_base_tc: dict[int, int] = {}
    last_v2_plus4_tc: dict[int, int] = {}
    last_v2_base_pos: dict[int, int] = {}
    last_v2_plus4_pos: dict[int, int] = {}

    for pos, message in enumerate(pending_expansion):
        if message.variant == "V1":
            cid = message.channel_field >> 3
            channel = channels.get(cid)
            if channel is None or message.timecode is None:
                continue
            add_sample(cid, message.timecode, _decode_sample_bytes(channel, message.data))
            continue

        cid = expansion_channel_map.get(message.channel_field)
        if cid is None:
            warnings.append(f"unmapped expansion channel_field 0x{message.channel_field:04x}")
            continue
        channel = channels.get(cid)
        if channel is None:
            warnings.append(f"missing CHS for expansion channel {cid}")
            continue

        period_ms = max(channel.period_us // 1000, 1)
        if message.variant == "V2":
            if message.timecode is None:
                continue
            if cid in pair_fields:
                base, plus4 = pair_fields[cid]
                if message.channel_field == base:
                    sample_ticks = [message.timecode, message.timecode - 2 * period_ms]
                    last_v2_base_tc[cid] = message.timecode
                    last_v2_base_pos[cid] = pos
                else:
                    sample_ticks = [message.timecode - period_ms, message.timecode - 2 * period_ms]
                    last_v2_plus4_tc[cid] = message.timecode
                    last_v2_plus4_pos[cid] = pos
            else:
                sample_ticks = [message.timecode, message.timecode - 2 * period_ms]
            raw0, raw1 = struct.unpack("<HH", message.data)
            add_sample(cid, sample_ticks[0], _decode_sample_bytes(channel, struct.pack("<H", raw0)))
            add_sample(cid, sample_ticks[1], _decode_sample_bytes(channel, struct.pack("<H", raw1)))
            continue

        base_tc = last_v2_base_tc.get(cid)
        plus4_tc = last_v2_plus4_tc.get(cid)
        base_pos = last_v2_base_pos.get(cid, -1)
        plus4_pos = last_v2_plus4_pos.get(cid, -1)
        if base_tc is None and plus4_tc is None:
            warnings.append(f"unanchored V3 expansion sample for channel {cid}")
            continue
        if base_pos >= plus4_pos and base_tc is not None:
            sample_tick = base_tc - period_ms
        elif plus4_tc is not None:
            sample_tick = plus4_tc + period_ms
        else:
            sample_tick = base_tc - period_ms  # type: ignore[operator]
        add_sample(cid, sample_tick, _decode_sample_bytes(channel, message.data))


def build_session(raw: bytes) -> SessionData:
    prefix_frames: list[Frame] = []
    offset = 0
    while True:
        frame = parse_frame(raw, offset)
        if frame is None:
            break
        prefix_frames.append(frame)
        offset = frame.next_offset
    body_start = offset

    if not prefix_frames or prefix_frames[0].tag != "CNF":
        raise ValueError("Could not locate the primary CNF container")

    channels: dict[int, ChannelInfo] = {}
    groups: dict[int, list[int]] = {}
    channel_samples: dict[int, list[tuple[int, float]]] = {}
    gps_frames: list[dict[str, float]] = []
    gnfi_ticks: list[int] = []
    laps: list[LapInfo] = []
    track: TrackInfo | None = None
    warnings: list[str] = []
    pending_expansion: list[PendingExpansionMessage] = []

    def add_sample(cid: int, tick: int, value: float) -> None:
        channel_samples.setdefault(cid, []).append((tick, value))

    def parse_cnf(frame: Frame, source: bytes) -> None:
        payload = source[frame.payload_start:frame.payload_start + frame.length]
        inner_offset = 0
        while True:
            inner = parse_frame(payload, inner_offset)
            if inner is None:
                break
            inner_payload = payload[inner.payload_start:inner.payload_start + inner.length]
            if inner.tag == "CHS":
                channel = _parse_channel_info(inner_payload)
                channels[channel.cid] = channel
            elif inner.tag == "GRP":
                gid, count = struct.unpack_from("<HH", inner_payload, 0)
                groups[gid] = list(struct.unpack_from("<" + "H" * count, inner_payload, 4))
            inner_offset = inner.next_offset

    def consume_tag_frame(frame: Frame, source: bytes) -> None:
        nonlocal track
        payload = source[frame.payload_start:frame.payload_start + frame.length]
        if frame.tag == "CNF":
            parse_cnf(frame, source)
        elif frame.tag in ("GPS", "GPS1"):
            _append_gps_frame(gps_frames, payload)
        elif frame.tag == "GNFI":
            _append_gnfi_tick(gnfi_ticks, payload)
        elif frame.tag == "LAP":
            lap = _parse_lap_payload(payload)
            if lap is not None:
                laps.append(lap)
        elif frame.tag == "TRK" and track is None:
            track = _parse_track_payload(payload)

    for frame in prefix_frames:
        consume_tag_frame(frame, raw)

    current_offset = body_start
    while current_offset < len(raw):
        frame = parse_frame(raw, current_offset)
        if frame is not None:
            consume_tag_frame(frame, raw)
            current_offset = frame.next_offset
            continue

        if raw[current_offset:current_offset + 2] == b"(S":
            tick, cid = struct.unpack_from("<IH", raw, current_offset + 2)
            channel = channels.get(cid)
            if channel is not None:
                record_len = 9 + channel.data_size
                if current_offset + record_len <= len(raw) and raw[current_offset + record_len - 1] == 0x29:
                    payload = raw[current_offset + 8:current_offset + 8 + channel.data_size]
                    add_sample(cid, tick, _decode_sample_bytes(channel, payload))
                    current_offset += record_len
                    continue

        if raw[current_offset:current_offset + 2] == b"(M":
            tick, cid, count = struct.unpack_from("<IHH", raw, current_offset + 2)
            channel = channels.get(cid)
            if channel is not None:
                record_len = 11 + channel.data_size * count
                if current_offset + record_len <= len(raw) and raw[current_offset + record_len - 1] == 0x29:
                    step_ms = max(channel.period_us // 1000, 1)
                    payload_start = current_offset + 10
                    for idx in range(count):
                        start = payload_start + idx * channel.data_size
                        stop = start + channel.data_size
                        add_sample(
                            cid,
                            tick + idx * step_ms,
                            _decode_sample_bytes(channel, raw[start:stop]),
                        )
                    current_offset += record_len
                    continue

        if raw[current_offset:current_offset + 2] == b"(G":
            tick, gid = struct.unpack_from("<IH", raw, current_offset + 2)
            cids = groups.get(gid)
            if cids is not None:
                pos = current_offset + 8
                ok = True
                for cid in cids:
                    channel = channels.get(cid)
                    if channel is None or pos + channel.data_size > len(raw):
                        ok = False
                        break
                    add_sample(cid, tick, _decode_group_value(channel, raw[pos:pos + channel.data_size]))
                    pos += channel.data_size
                if ok and pos < len(raw) and raw[pos] == 0x29:
                    current_offset = pos + 1
                    continue

        expansion = _parse_expansion_message(raw, current_offset, channels)
        if expansion is not None:
            message, record_len = expansion
            pending_expansion.append(message)
            current_offset += record_len
            continue
        if raw[current_offset:current_offset + 2] == b"(c":
            warnings.append(f"unknown expansion message near offset {current_offset}")
            current_offset += 1
            continue

        warnings.append(f"unparsed byte 0x{raw[current_offset]:02x} at offset {current_offset}")
        current_offset += 1

    _emit_expansion_samples(pending_expansion, channels, add_sample, warnings)

    for cid, points in list(channel_samples.items()):
        channel_samples[cid] = collapse_points(points)

    timeline = [int(value) for _, value in channel_samples.get(0, [])]
    if not timeline:
        raise ValueError("No MClk timeline found")

    gps_timing_fixed = repair_gps_timecodes(gps_frames, gnfi_ticks)
    time_origin_ms = compute_time_origin_ms(channel_samples, laps)

    return SessionData(
        channels=channels,
        groups=groups,
        channel_samples=channel_samples,
        gps_frames=gps_frames,
        timeline=timeline,
        time_origin_ms=time_origin_ms,
        laps=laps,
        track=track,
        warnings=warnings,
        gps_timing_fixed=gps_timing_fixed,
    )
