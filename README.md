# aim-downloader

A pure-Python toolkit for downloading and converting telemetry data from AiM data loggers over Wi-Fi.

Reverse-engineered from network captures of an AiM SOLO2DL. No vendor SDK required.

## Tools

| Script | Purpose |
|---|---|
| `aim.py` | List, download, and delete sessions from the logger over Wi-Fi |
| `xrk2csv.py` | Convert `.xrk` / `.xrz` session files to CSV |
| `xrz2xrk.py` | Convert `.xrz` (compressed) to `.xrk` (export format) |

## Requirements

Python 3.10+, no third-party dependencies.

## aim.py — Wi-Fi Downloader

Connect your PC to the AiM logger's Wi-Fi AP, then run one of the subcommands below.

The logger's AP IP varies by device (`10.0.0.1`, `11.0.0.1`, `12.0.0.1`, `14.0.0.1`). By default, `aim.py` auto-discovers it via UDP. Pass `--host` to skip discovery.

### discover

Probe the network for AiM loggers:

```
python aim.py discover
python aim.py discover --host 10.0.0.1
python aim.py discover --timeout 5
```

### list

Show all recorded sessions on the device:

```
python aim.py list
python aim.py list --host 10.0.0.1
python aim.py list --json          # JSON array output
python aim.py list --raw-csv       # raw CSV from device
```

Example output:

```
#  name           size      date        hour   laps  best(ms)  track
-  -------------  --------  ----------  -----  ----  --------  -----
1  a_7064.xrz     1.5MB     21/04/2026  18:24  23    117612    Inje NCK
2  a_7065.xrz     0.9MB     22/04/2026  10:11  14    119340    Inje NCK
```

### download

Download one or more sessions by name, short name, or index:

```
python aim.py download a_7064.xrz
python aim.py download a_7064          # .xrz extension is optional
python aim.py download 1               # 1-based index from the list
python aim.py download --all -o ./data
python aim.py download a_7064.xrz a_7065.xrz -o ./sessions
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--host IP` | auto | Logger IP address |
| `-o DIR` | `.` | Output directory |
| `--all` | off | Download every session |
| `--force` | off | Overwrite files even if size matches |
| `--quiet` | off | Suppress progress bar |
| `--timeout SEC` | 30 | TCP timeout in seconds |
| `-v` | off | Trace every TCP frame |

Files are written atomically via a `.part` temp file. Existing files whose size already matches are skipped automatically.

### delete

Delete one or more sessions by name, short name, or index:

```
python aim.py delete a_7064.xrz
python aim.py delete a_7064
python aim.py delete 1
python aim.py delete --all
python aim.py delete a_7064.xrz a_7065.xrz -y
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--host IP` | auto | Logger IP address |
| `--all` | off | Delete every session |
| `-y`, `--yes` | off | Skip the confirmation prompt |
| `--timeout SEC` | 30 | TCP timeout in seconds |
| `-v` | off | Trace every TCP frame |

`delete` first resolves the targets from the current session list, then opens a fresh delete-only TCP session (`hello -> delete -> list verify`) to match the protocol captured from the vendor app.

### info

Dump the device info block (firmware, Wi-Fi config, system flags):

```
python aim.py info
python aim.py info --host 10.0.0.1
```

> **Note:** The output may include the device Wi-Fi password in plaintext (`wf_pwd`).

---

## xrk2csv.py — Telemetry to CSV

Convert a downloaded `.xrz` or `.xrk` file into a flat CSV with ~68 columns.

```
python xrk2csv.py session.xrz
python xrk2csv.py session.xrz -o output.csv
python xrk2csv.py session.xrk
```

The output CSV contains one row per master clock tick (10 Hz base rate) with columns including:

| Column group | Channels |
|---|---|
| Time | `Time` (seconds from session start, anchored to `LAP` when present) |
| GPS | Speed, Lat/Lon, Altitude, Heading, Slope, LatAcc, LonAcc, Gyro, Radius, PosAccuracy, SpdAccuracy, Nsat |
| IMU | InlineAcc, LateralAcc, VerticalAcc, RollRate, PitchRate, YawRate, MagnetomX/Y/Z |
| Drivetrain | RPM, Gear, Speed, Wheel speeds (FL/FR/RL/RR) |
| Chassis | Long/Lat Acc, Yaw Rate, Steering Angle, Brake P F/R |
| Engine | Eng T, Oil T, Amb T, Gear T, Throttle, Pedal Pos, Eng Load, Eng Torque |
| Fuel | Fuel used, Fuel km, Fuel Raw, Ambient P |
| Electrical | Internal Battery, External Voltage, Battery Volt, Current IBS |
| Status | ABS, ASC, DSC, Brake, Eng Mode, Clutch Sw, Fuel Lamp, Hi Beam, Indicator lights, Eng Heat St |
| Distance | Distance on GPS Speed, Distance on Vehicle Speed |

The parser keeps CHS metadata (units, calibrated flag, interpolation hints,
display ranges, source ids), repairs the known GPS `~65533 ms` tick jump when
it appears, and decodes newer expansion-device `(c)` channels used by shock
potentiometers and IMUs on newer AiM loggers.

---

## xrz2xrk.py — XRZ to XRK Converter

Add an export-style footer to a `.xrz` file so it can be opened as `.xrk`:

```
python xrz2xrk.py session.xrz
python xrz2xrk.py session.xrz -o session.xrk
python xrz2xrk.py session.xrz --racer "Driver" --vehicle "Car" --vehicle-type "GT3" --note "Qualifying"
python xrz2xrk.py session.xrz --force    # overwrite existing output
```

The XRK footer appended by this tool is functionally identical to what the AiM Race Studio export produces: `raw_body + RCR + VEH + VTY + NTE frames (120 B)`.

Both `xrk2csv.py` and `xrz2xrk.py` accept truncated `.xrz` inputs as long as
zlib can still recover a structurally valid partial session.

---

## Full workflow example

```bash
# 1. Connect PC to the AiM logger Wi-Fi AP

# 2. Discover the logger IP
python aim.py discover

# 3. List available sessions
python aim.py list

# 4. Download all sessions
python aim.py download --all -o ./sessions

# 5. (Optional) Delete old sessions from the logger
python aim.py delete a_7064.xrz

# 6. Convert to CSV
python xrk2csv.py sessions/a_7064.xrz

# 7. (Optional) Export to XRK format
python xrz2xrk.py sessions/a_7064.xrz --racer "Driver" --vehicle "SOLO2DL"
```

---

## Protocol & Format Documentation

- [`docs/wifi_protocol.md`](docs/wifi_protocol.md) — AiM Wi-Fi protocol spec (UDP discovery, TCP framing, command catalog, session list / file download flow). Validated on real AiM SOLO2DL hardware.
- [`docs/xrk_format.md`](docs/xrk_format.md) — XRK/XRZ binary format spec (frame layout, channel schema, GPS / IMU / CAN sample records, CSV mapping table).

---

## Known limitations

- Tested against AiM SOLO2DL firmware only. Other AiM models likely work but are unverified.
- IMU channels (accelerometer, gyroscope, magnetometer) are decoded as raw counts. Engineering-unit scaling and calibration matrices are not yet resolved.
- Download resume from an arbitrary offset is not implemented.
