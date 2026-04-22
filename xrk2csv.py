#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path


WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
G_STD = 9.80665

ENUM_CHANNEL_IDS = {24, 55, 56, 57, 58, 59, 60, 62}
M_HALF_FLOAT_CHANNEL_IDS = {9, 10, 11, 17, 18, 19, 20, 21, 22}
LINEAR_CHANNEL_IDS = {12, 13, 33, 34, 39, 44, 45, 47, 50}
CHANNEL_SCALE = {44: 0.001}


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
    width: int
    codec_a: int
    codec_b: int


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert AiM XRK/XRZ/RAW telemetry to a practical CSV subset."
    )
    parser.add_argument("input", type=Path, help="Input .xrk, .xrz, or decompressed raw file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output CSV path (default: <input stem>.csv)",
    )
    return parser.parse_args()


def looks_like_zlib(data: bytes) -> bool:
    if len(data) < 2:
        return False
    cmf, flg = data[0], data[1]
    if (cmf & 0x0F) != 0x08:
        return False
    return ((cmf << 8) | flg) % 31 == 0


def read_session_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".xrz" or looks_like_zlib(data):
        try:
            return zlib.decompress(data)
        except zlib.error as exc:
            raise ValueError(f"failed to zlib-decompress {path}: {exc}") from exc
    return data


def parse_frame(buf: bytes, offset: int) -> Frame | None:
    if offset + 20 > len(buf) or buf[offset : offset + 2] != b"<h":
        return None
    tag = buf[offset + 2 : offset + 6]
    length = struct.unpack_from("<I", buf, offset + 6)[0]
    cls = buf[offset + 10]
    if buf[offset + 11] != 0x3E:
        return None
    end = offset + 12 + length
    if end + 8 > len(buf):
        return None
    if buf[end] != 0x3C or buf[end + 1 : end + 5] != tag or buf[end + 7] != 0x3E:
        return None
    payload = buf[offset + 12 : end]
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


def format_number(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def scale_channel_value(cid: int, value: float | None) -> float | None:
    if value is None:
        return None
    return value * CHANNEL_SCALE.get(cid, 1.0)


def build_session(raw: bytes) -> tuple[
    dict[int, ChannelInfo],
    dict[int, list[int]],
    dict[int, list[tuple[int, float]]],
    list[dict[str, float]],
    list[int],
]:
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

    def parse_cnf(frame: Frame, source: bytes) -> None:
        payload = source[frame.payload_start : frame.payload_start + frame.length]
        inner_offset = 0
        while True:
            inner = parse_frame(payload, inner_offset)
            if inner is None:
                break
            inner_payload = payload[inner.payload_start : inner.payload_start + inner.length]
            if inner.tag == "CHS":
                cid = struct.unpack_from("<I", inner_payload, 0)[0]
                channels[cid] = ChannelInfo(
                    cid=cid,
                    short_name=inner_payload[24:32].split(b"\0", 1)[0].decode("ascii", "replace"),
                    long_name=inner_payload[32:64].split(b"\0", 1)[0].decode("ascii", "replace"),
                    period_us=struct.unpack_from("<I", inner_payload, 64)[0],
                    width=struct.unpack_from("<I", inner_payload, 72)[0],
                    codec_a=struct.unpack_from("<I", inner_payload, 80)[0],
                    codec_b=struct.unpack_from("<I", inner_payload, 84)[0],
                )
            elif inner.tag == "GRP":
                gid, count = struct.unpack_from("<HH", inner_payload, 0)
                groups[gid] = list(struct.unpack_from("<" + "H" * count, inner_payload, 4))
            inner_offset = inner.next_offset

    parse_cnf(prefix_frames[0], raw)

    channel_samples: dict[int, list[tuple[int, float]]] = {}
    gps_frames: list[dict[str, float]] = []

    def add_sample(cid: int, tick: int, value: float) -> None:
        channel_samples.setdefault(cid, []).append((tick, value))

    current_offset = body_start
    while current_offset < len(raw):
        frame = parse_frame(raw, current_offset)
        if frame is not None:
            payload = raw[frame.payload_start : frame.payload_start + frame.length]
            if frame.tag == "CNF":
                parse_cnf(frame, raw)
            elif frame.tag == "GPS":
                words = list(struct.unpack("<14I", payload))

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
            current_offset = frame.next_offset
            continue

        if raw[current_offset : current_offset + 2] == b"(S":
            tick, cid = struct.unpack_from("<IH", raw, current_offset + 2)
            if current_offset + 13 <= len(raw) and raw[current_offset + 12] == 0x29:
                value_bits = raw[current_offset + 8 : current_offset + 12]
                if cid == 0:
                    add_sample(cid, tick, float(struct.unpack("<I", value_bits)[0]))
                else:
                    add_sample(cid, tick, float(struct.unpack("<f", value_bits)[0]))
                current_offset += 13
                continue
            if current_offset + 11 <= len(raw) and raw[current_offset + 10] == 0x29:
                raw16 = struct.unpack_from("<H", raw, current_offset + 8)[0]
                if cid in (12, 13):
                    add_sample(cid, tick, half_float_from_u16(raw16) / 1000.0)
                else:
                    add_sample(cid, tick, float(raw16))
                current_offset += 11
                continue

        if raw[current_offset : current_offset + 2] == b"(M":
            tick, cid, count = struct.unpack_from("<IHH", raw, current_offset + 2)
            record_len = 11 + 2 * count
            if current_offset + record_len <= len(raw) and raw[current_offset + record_len - 1] == 0x29:
                values = struct.unpack_from("<" + "H" * count, raw, current_offset + 10)
                ch = channels.get(cid)
                if ch is None:
                    raise ValueError(
                        f"unknown channel id {cid} in M record at offset {current_offset}"
                    )
                step_ms = ch.period_us // 1000
                for idx, raw16 in enumerate(values):
                    if cid == 16:
                        value = float(struct.unpack("<h", struct.pack("<H", raw16))[0])
                    elif cid in M_HALF_FLOAT_CHANNEL_IDS:
                        value = half_float_from_u16(raw16)
                    else:
                        value = float(raw16)
                    add_sample(cid, tick + idx * step_ms, value)
                current_offset += record_len
                continue

        if raw[current_offset : current_offset + 2] == b"(G":
            tick, gid = struct.unpack_from("<IH", raw, current_offset + 2)
            cids = groups.get(gid)
            if cids is not None:
                pos = current_offset + 8
                ok = True
                for cid in cids:
                    if cid in ENUM_CHANNEL_IDS:
                        if pos + 8 > len(raw):
                            ok = False
                            break
                        code = struct.unpack_from("<H", raw, pos)[0]
                        label = raw[pos + 2 : pos + 8].split(b"\0", 1)[0].decode("ascii", "replace")
                        if cid == 24 and label.isdigit():
                            add_sample(cid, tick, float(label))
                        else:
                            add_sample(cid, tick, float(code))
                        pos += 8
                    else:
                        if pos + 4 > len(raw):
                            ok = False
                            break
                        value = struct.unpack_from("<f", raw, pos)[0]
                        add_sample(cid, tick, float(value))
                        pos += 4
                if ok and pos < len(raw) and raw[pos] == 0x29:
                    current_offset = pos + 1
                    continue

        raise ValueError(f"Could not parse stream at offset {current_offset}")

    for cid, points in list(channel_samples.items()):
        channel_samples[cid] = collapse_points(points)

    timeline = [int(value) for _, value in channel_samples.get(0, [])]
    if not timeline:
        raise ValueError("No MClk timeline found")

    return channels, groups, channel_samples, gps_frames, timeline


def build_gps_resampled(gps_frames: list[dict[str, float]], timeline: list[int]) -> list[dict[str, float]]:
    if not gps_frames:
        raise ValueError("No GPS frames found")

    src_ticks = [int(frame["tick"]) for frame in gps_frames]

    linear_fields = {
        "lat": [frame["lat"] for frame in gps_frames],
        "lon": [frame["lon"] for frame in gps_frames],
        "alt": [frame["alt"] for frame in gps_frames],
        "vn": [frame["vn"] for frame in gps_frames],
        "ve": [frame["ve"] for frame in gps_frames],
        "vu": [frame["vu"] for frame in gps_frames],
    }
    step_fields = {
        "pos_acc_mm": [frame["pos_acc_mm"] for frame in gps_frames],
        "spd_acc_kmh": [frame["spd_acc_kmh"] for frame in gps_frames],
        "nsat": [frame["nsat"] for frame in gps_frames],
    }
    heading_unwrapped = unwrap_angles([frame["heading_deg"] for frame in gps_frames])

    linear_resamplers = {name: NumericResampler(list(zip(src_ticks, values))) for name, values in linear_fields.items()}
    step_resamplers = {name: NumericResampler(list(zip(src_ticks, values))) for name, values in step_fields.items()}
    heading_resampler = NumericResampler(list(zip(src_ticks, heading_unwrapped)))

    out: list[dict[str, float]] = []
    for tick in timeline:
        vn = linear_resamplers["vn"].linear(tick)
        ve = linear_resamplers["ve"].linear(tick)
        vu = linear_resamplers["vu"].linear(tick)
        assert vn is not None and ve is not None and vu is not None
        speed_ms = math.hypot(vn, ve)
        heading_u = heading_resampler.linear(tick)
        assert heading_u is not None
        out.append(
            {
                "lat": linear_resamplers["lat"].linear(tick),
                "lon": linear_resamplers["lon"].linear(tick),
                "alt": linear_resamplers["alt"].linear(tick),
                "vn": vn,
                "ve": ve,
                "vu": vu,
                "speed_kmh": speed_ms * 3.6,
                "heading_u": heading_u,
                "heading_deg": ((heading_u + 180.0) % 360.0) - 180.0,
                "slope_deg": math.degrees(math.atan2(vu, speed_ms)) if speed_ms else 0.0,
                "pos_acc_mm": step_resamplers["pos_acc_mm"].step(tick),
                "spd_acc_kmh": step_resamplers["spd_acc_kmh"].step(tick),
                "nsat": round(step_resamplers["nsat"].step(tick) or 0.0),
            }
        )

    if len(out) == 1:
        out[0]["lat_acc_g"] = 0.0
        out[0]["lon_acc_g"] = 0.0
        out[0]["gyro_deg_s"] = 0.0
        out[0]["radius_m"] = 10000.0
        return out

    for idx, sample in enumerate(out):
        if idx == 0:
            left_tick, left = timeline[idx], out[idx]
            right_tick, right = timeline[idx + 1], out[idx + 1]
        elif idx == len(out) - 1:
            left_tick, left = timeline[idx - 1], out[idx - 1]
            right_tick, right = timeline[idx], out[idx]
        else:
            left_tick, left = timeline[idx - 1], out[idx - 1]
            right_tick, right = timeline[idx + 1], out[idx + 1]

        dt = (right_tick - left_tick) / 1000.0
        if dt <= 0:
            sample["lon_acc_g"] = 0.0
            sample["lat_acc_g"] = 0.0
            sample["gyro_deg_s"] = 0.0
            sample["radius_m"] = 10000.0
            continue
        dvn = (right["vn"] - left["vn"]) / dt
        dve = (right["ve"] - left["ve"]) / dt
        dheading = (right["heading_u"] - left["heading_u"]) / dt
        speed_ms = math.hypot(sample["vn"], sample["ve"])

        if speed_ms < 1e-9:
            lon_acc_g = 0.0
            lat_acc_g = 0.0
        else:
            lon_acc_g = (sample["vn"] * dvn + sample["ve"] * dve) / (speed_ms * G_STD)
            lat_acc_g = (sample["vn"] * dve - sample["ve"] * dvn) / (speed_ms * G_STD)

        yaw_rate_deg_s = dheading
        if abs(math.radians(yaw_rate_deg_s)) < 1e-9:
            radius_m = 10000.0
        else:
            radius_m = min(10000.0, speed_ms / abs(math.radians(yaw_rate_deg_s)))

        sample["lon_acc_g"] = lon_acc_g
        sample["lat_acc_g"] = lat_acc_g
        sample["gyro_deg_s"] = yaw_rate_deg_s
        sample["radius_m"] = radius_m

    out[0]["lat_acc_g"] = 0.0
    out[0]["lon_acc_g"] = 0.0
    out[0]["gyro_deg_s"] = 0.0
    out[0]["radius_m"] = 10000.0

    return out


def build_output_rows(
    channels: dict[int, ChannelInfo],
    channel_samples: dict[int, list[tuple[int, float]]],
    gps_rows: list[dict[str, float]],
    timeline: list[int],
) -> tuple[list[str], list[list[str]]]:
    channel_resamplers = {cid: NumericResampler(points) for cid, points in channel_samples.items()}

    def ch_linear(cid: int, tick: int) -> float | None:
        resampler = channel_resamplers.get(cid)
        value = None if resampler is None else resampler.linear(tick)
        return scale_channel_value(cid, value)

    def ch_step(cid: int, tick: int) -> float | None:
        resampler = channel_resamplers.get(cid)
        value = None if resampler is None else resampler.step(tick)
        return scale_channel_value(cid, value)

    def ch_value(cid: int, tick: int) -> float | None:
        if cid in LINEAR_CHANNEL_IDS:
            return ch_linear(cid, tick)
        return ch_step(cid, tick)

    headers = [
        "Time",
        "GPS Speed",
        "GPS Nsat",
        "GPS LatAcc",
        "GPS LonAcc",
        "GPS Slope",
        "GPS Heading",
        "GPS Gyro",
        "GPS Altitude",
        "GPS PosAccuracy",
        "GPS SpdAccuracy",
        "GPS Radius",
        "GPS Latitude",
        "GPS Longitude",
        "MagnetomX",
        "MagnetomY",
        "MagnetomZ",
        "Internal Battery",
        "External Voltage",
        "RPM dup 1",
        "InlineAcc",
        "LateralAcc",
        "VerticalAcc",
        "RollRate",
        "PitchRate",
        "YawRate",
        "RPM dup 2",
        "Gear",
        "Speed",
        "Wheel Speed RL",
        "Wheel Speed RR",
        "Wheel Speed FL",
        "Wheel Speed FR",
        "Long Acc",
        "Lat Acc",
        "Yaw Rate",
        "Eng T",
        "Oil T",
        "Amb T",
        "Gear T",
        "Brake P F",
        "Brake P R",
        "Ambient P",
        "Steering Angle",
        "Throttle",
        "Pedal Pos",
        "Eng Load",
        "Odometer",
        "Fuel km",
        "Battery Volt",
        "Fuel used",
        "Gbx Torque",
        "Eng Torque",
        "Current IBS",
        "ABS",
        "ASC",
        "Brake",
        "Fuel Raw ul",
        "Indicator lights",
        "Fuel Lamp",
        "Hi Beam",
        "Eng Mode",
        "DSC",
        "Clutch Sw",
        "Rpm MAX",
        "Eng Heat St",
        "Distance on GPS Speed",
        "Distance on Vehicle Speed",
    ]

    rows: list[list[str]] = []
    first_tick = timeline[0]
    dist_gps = 0.0
    dist_vehicle = 0.0
    prev_gps_speed = None
    prev_vehicle_speed = None
    prev_tick = None

    for idx, tick in enumerate(timeline):
        gps = gps_rows[idx]
        if prev_tick is not None:
            dt = (tick - prev_tick) / 1000.0
            if prev_gps_speed is not None:
                dist_gps += 0.5 * (prev_gps_speed + gps["speed_kmh"]) * dt / 3.6
            vehicle_speed = ch_value(25, tick)
            if prev_vehicle_speed is not None and vehicle_speed is not None:
                dist_vehicle += 0.5 * (prev_vehicle_speed + vehicle_speed) * dt / 3.6
        else:
            vehicle_speed = ch_value(25, tick)

        row = [
            format_number((tick - first_tick) / 1000.0, 3),
            format_number(gps["speed_kmh"]),
            format_number(float(gps["nsat"]), 0),
            format_number(gps["lat_acc_g"]),
            format_number(gps["lon_acc_g"]),
            format_number(gps["slope_deg"]),
            format_number(gps["heading_deg"]),
            format_number(gps["gyro_deg_s"]),
            format_number(gps["alt"]),
            format_number(gps["pos_acc_mm"]),
            format_number(gps["spd_acc_kmh"]),
            format_number(gps["radius_m"]),
            format_number(gps["lat"], 8),
            format_number(gps["lon"], 8),
            format_number(ch_value(9, tick)),
            format_number(ch_value(10, tick)),
            format_number(ch_value(11, tick)),
            format_number(ch_value(12, tick)),
            format_number(ch_value(13, tick)),
            format_number(ch_value(16, tick)),
            format_number(ch_value(17, tick)),
            format_number(ch_value(18, tick)),
            format_number(ch_value(19, tick)),
            format_number(ch_value(20, tick)),
            format_number(ch_value(21, tick)),
            format_number(ch_value(22, tick)),
            format_number(ch_value(23, tick)),
            format_number(ch_value(24, tick)),
            format_number(vehicle_speed),
            format_number(ch_value(26, tick)),
            format_number(ch_value(27, tick)),
            format_number(ch_value(28, tick)),
            format_number(ch_value(29, tick)),
            format_number(ch_value(30, tick)),
            format_number(ch_value(31, tick)),
            format_number(ch_value(32, tick)),
            format_number(ch_value(33, tick)),
            format_number(ch_value(34, tick)),
            format_number(ch_value(35, tick)),
            format_number(ch_value(36, tick)),
            format_number(ch_value(37, tick)),
            format_number(ch_value(38, tick)),
            format_number(ch_value(39, tick)),
            format_number(ch_value(40, tick)),
            format_number(ch_value(41, tick)),
            format_number(ch_value(42, tick)),
            format_number(ch_value(43, tick)),
            format_number(ch_value(44, tick)),
            format_number(ch_value(45, tick)),
            format_number(ch_value(46, tick)),
            format_number(ch_value(47, tick)),
            format_number(ch_value(48, tick)),
            format_number(ch_value(49, tick)),
            format_number(ch_value(50, tick)),
            format_number(ch_step(51, tick)),
            format_number(ch_step(52, tick)),
            format_number(ch_step(53, tick)),
            format_number(ch_value(54, tick)),
            format_number(ch_step(55, tick)),
            format_number(ch_step(56, tick)),
            format_number(ch_step(57, tick)),
            format_number(ch_step(58, tick)),
            format_number(ch_step(59, tick)),
            format_number(ch_step(60, tick)),
            format_number(ch_value(61, tick)),
            format_number(ch_step(62, tick)),
            format_number(dist_gps),
            format_number(dist_vehicle),
        ]
        rows.append(row)
        prev_tick = tick
        prev_gps_speed = gps["speed_kmh"]
        prev_vehicle_speed = vehicle_speed

    return headers, rows


def main() -> int:
    args = parse_args()
    input_path = args.input
    output_path = args.output or input_path.with_suffix(".csv")
    try:
        raw = read_session_bytes(input_path)
        channels, groups, channel_samples, gps_frames, timeline = build_session(raw)
        gps_rows = build_gps_resampled(gps_frames, timeline)
        headers, rows = build_output_rows(channels, channel_samples, gps_rows, timeline)

        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(rows)} rows to {output_path}")
    print(f"Recovered {len(headers)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
