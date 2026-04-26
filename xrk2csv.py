#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import struct
import sys
from pathlib import Path

from aim_telemetry import (
    G_STD,
    LINEAR_CHANNEL_IDS,
    ChannelInfo,
    NumericResampler,
    build_session,
    read_session_bytes,
    scale_channel_value,
    unwrap_angles,
)


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


def format_number(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


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
    time_origin_ms: int,
) -> tuple[list[str], list[list[str]]]:
    channel_resamplers = {cid: NumericResampler(points) for cid, points in channel_samples.items()}

    def ch_linear(cid: int, tick: int) -> float | None:
        resampler = channel_resamplers.get(cid)
        value = None if resampler is None else resampler.linear(tick)
        channel = channels.get(cid)
        return scale_channel_value(channel or cid, value)

    def ch_step(cid: int, tick: int) -> float | None:
        resampler = channel_resamplers.get(cid)
        value = None if resampler is None else resampler.step(tick)
        channel = channels.get(cid)
        return scale_channel_value(channel or cid, value)

    def ch_value(cid: int, tick: int) -> float | None:
        channel = channels.get(cid)
        if (channel is not None and channel.interpolate) or cid in LINEAR_CHANNEL_IDS:
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
            format_number((tick - time_origin_ms) / 1000.0, 3),
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
        session = build_session(raw)
        gps_rows = build_gps_resampled(session.gps_frames, session.timeline)
        headers, rows = build_output_rows(
            session.channels,
            session.channel_samples,
            gps_rows,
            session.timeline,
            session.time_origin_ms,
        )

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
