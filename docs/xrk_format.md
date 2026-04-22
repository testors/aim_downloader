# AiM XRK/XRZ Internal Format Spec

This document is the consolidated final XRK/XRZ format
notes. Network transfer and download procedure are documented in
[aim_wifi_protocol.md](aim_wifi_protocol.md). This
file focuses on the internal binary structure of downloaded `xrz` / `xrk`
session files.

Confidence labels:

- Confirmed: byte-level validation plus CSV comparison reproduced the value
- Strong inference: pattern match is very strong, but only one session was used
- Unresolved: plausible candidate only

## 1. High-Level Conclusions

### 1.1 File layering

For the analyzed session:

```text
xrz  --zlib inflate-->  raw
xrk  = raw + 120B export footer
```

- `xrz` is a zlib-deflated file (`78 01`)
- inflating `xrz` yields the `raw` body byte-for-byte
- the app-exported `xrk` is simply `raw` plus a 120-byte footer

### 1.2 XRK export footer

The footer that exists only in `xrk` uses the same frame wrapper as the main
body and contains four metadata tags:

- `RCR = "Driver"`
- `VEH = "Car"`
- `VTY = "Type"`
- `NTE = "Memo"`

So the app export flow appears to be:

1. inflate `xrz`
2. keep the body unchanged
3. append four user-readable metadata frames

### 1.3 Top-level structure

`raw` is not a flat sample array. It has two layers:

1. metadata / schema tag-frame region
2. body sample stream region

Observed layout at the start of the sample:

| Off | Tag | Len | Cls | Meaning |
| ---: | :---: | ---: | :---: | --- |
| `0x000000` | `CNF` | 10878 | `0x01` | channel/group schema container |
| `0x002a92` | `RCR` | 1 | `0x01` | racer (empty) |
| `0x002aa7` | `VEH` | 1 | `0x01` | vehicle (empty) |
| `0x002abc` | `CMP` | 1 | `0x01` | championship (empty) |
| `0x002ad1` | `VTY` | 1 | `0x01` | vehicle type (empty) |
| `0x002ae6` | `NDV` | 1 | `0x01` | unresolved |
| `0x002afb` | `RACM` | 1 | `0x01` | racer mode (empty) |
| `0x002b10` | `VET` | 1 | `0x01` | unresolved |
| `0x002b25` | `SRC` | 128 | `0x01` | source block |
| `0x002bb9` | `iSLV` | 64 | `0x01` | slave/internal info |
| `0x002c0d` | `HWNF` | 27 | `0x01` | hardware info |
| `0x002c3c` | `ENF` | 181 | `0x01` | engine/vehicle info |
| `0x002d05` | `RACM` | 6 | `0x01` | racer mode value |
| `0x002d1f` | `TRK ` | 96 | `0x02` | track info |
| `0x002d93` | `PDLT` | 18 | `0x01` | unresolved |
| `0x002db9` | `TMD` | 11 | `0x01` | unresolved |
| `0x002dd8` | `TMT` | 9 | `0x01` | unresolved |
| `0x002df5` | `ODO` | 384 | `0x01` | odometer / summary |
| `0x002f89` | - | - | - | body sample stream starts |
| `0x009441` | `CNF` | 158 | `0x01` | second CNF redefining `iGPS` |

From body start to EOF, the file parses cleanly as a sequence of:

- tag frames (`<h...>...<...>`)
- parenthesized sample records (`S`, `M`, `G`)

with no unexplained gaps in the observed sample.

## 2. Common Frame Wrapper

The entire file body uses the same wrapper, both for top-level frames and for
frames nested inside `CNF`.

```text
offset  size  meaning
0       2     "<h"  (0x3c 0x68)
2       4     TAG   (4 ASCII, NUL-padded if needed)
6       4     len   (u32 LE)
10      1     class byte
11      1     '>'   (0x3e)
12      len   payload
12+len  1     '<'   (0x3c)
13+len  4     TAG again
17+len  2     chk   (u16 LE)
19+len  1     '>'   (0x3e)
```

- total frame size = `20 + len`
- confirmed checksum:

```text
checksum = sum(payload bytes) & 0xffff
```

This is not a CRC.

## 3. The CNF Container

### 3.1 CNF1

The first `CNF` frame (`len=10878`, `cls=0x01`) defines the main schema.

Nested contents:

| Inner Tag | Count | Role |
| --- | ---: | --- |
| `CHS` | 65 | channel schema |
| `CDE` | 65 | channel descriptor |
| `GRP` | 22 | channel list for `G` records |

### 3.2 CNF2

The second `CNF` frame found later in the body (`len=158`) contains:

- `CHS` x 1
- `CDE` x 1

Its key meaning is not "new channel added". It redefines:

```text
ch64 = iGPS
```

So the set of unique channel IDs in this session remains:

```text
0 .. 64   (65 channels total)
```

## 4. CHS Records

Each `CHS` payload is exactly `112` bytes.

| Offset | Size | Meaning | Status |
| ---: | ---: | --- | --- |
| `0x00` | 12 | zero padding | observed |
| `0x0c` | 4 | per-channel opaque id/hash | unresolved |
| `0x10` | 4 | source family | strong inference |
| `0x14` | 4 | unit family | strong inference |
| `0x18` | 8 | short name ASCII | confirmed |
| `0x20` | 32 | long name ASCII | confirmed |
| `0x40` | 4 | `period_us` | confirmed |
| `0x44` | 4 | export-slot style value | observed |
| `0x48` | 4 | stored value width | confirmed |
| `0x4c` | 4 | `"@AIM"` or zero | observed |
| `0x50` | 4 | `codec_a` | strong inference |
| `0x54` | 4 | `codec_b` | strong inference |
| `0x58` | 4 | value behaving like `channel_idx * 2` | observed |
| `0x5c` | 4 | constant `2` | observed |
| `0x60` | 4 | constant `0` | observed |
| `0x64` | 4 | constant `1.0f` | observed |
| `0x68` | 4 | opaque calibration/flags | unresolved |
| `0x6c` | 4 | trailing opaque value | unresolved |

### 4.1 `period_us`

Offset `0x40` matches sample period directly:

- `1000000` -> 1 Hz
- `100000` -> 10 Hz
- `50000` -> 20 Hz
- `20000` -> 50 Hz
- `0` -> tag-frame based / special handling

### 4.2 `value width`

Offset `0x48` matches the on-disk payload width:

- `2` -> short / half / int16 family
- `4` -> float32 family
- `8` -> enum (`u16 code + ascii[6]`)

### 4.3 Codec hints

The exact names are unresolved, but they correlate strongly with storage
format:

| Channel family | Width | Codec A | Codec B | Actual record type |
| --- | ---: | ---: | ---: | --- |
| `DistL`, `DistLI` | 4 | 3 | 1 | `S` |
| `MClk`, `IBat`, `VBat` | 2/4 | 2 or 4099 | 1 | `S` |
| CAN float channels | 4 | 2 | 4 | `G` |
| enum channels | 8 | 2 | 4 | `G` |
| `Magnetom*` | 2 | 2 | 6 | `M` |
| `InlineAcc` / `LateralAcc` / `VerticalAcc` | 2 | 6 | 6 | `M` |

### 4.4 Full channel list

| id | short | long name | width | period_us | codec(a,b) | Notes |
| ---: | :---: | --- | ---: | ---: | :---: | --- |
| 0 | MClk | Master Clk | 4 | 100000 | (4099,1) | session master tick in ms |
| 1 | LAP | Lap Time | 20 | 0 | (2,1) | lap struct |
| 2 | PreT | Predictive Time | 4 | 1000000 | (3,1) | |
| 3 | +-BR | Best Run Diff | 4 | 1000000 | (3,1) | |
| 4 | +-BT | Best Today Diff | 4 | 1000000 | (3,1) | |
| 5 | +-PL | Prev Lap Diff | 4 | 1000000 | (3,1) | |
| 6 | +-RL | Ref Lap Diff | 4 | 1000000 | (3,1) | |
| 7 | RollT | Roll Time | 12 | 0 | (1,1) | |
| 8 | BestT | Best Time | 60 | 0 | (1,1) | |
| 9 | MgX | MagnetomX | 2 | 20000 | (2,6) | |
| 10 | MgY | MagnetomY | 2 | 20000 | (2,6) | |
| 11 | MgZ | MagnetomZ | 2 | 20000 | (2,6) | |
| 12 | IBat | Internal Battery | 2 | 1000000 | (2,1) | binary16 / 1000 |
| 13 | VBat | External Voltage | 2 | 1000000 | (2,1) | binary16 / 1000 |
| 14 | DistL | Distance Lap | 4 | 100000 | (3,1) | float32 meters |
| 15 | DistLI | Distance Lap Int | 4 | 100000 | (3,1) | float32 meters |
| 16 | RPM | RPM | 2 | 50000 | (2,1) | `M` block, `cnt=2` |
| 17 | InlA | InlineAcc | 2 | 20000 | (6,6) | |
| 18 | LatA | LateralAcc | 2 | 20000 | (6,6) | |
| 19 | VerA | VerticalAcc | 2 | 20000 | (6,6) | |
| 20 | Roll | RollRate | 2 | 20000 | (6,6) | |
| 21 | Ptch | PitchRate | 2 | 20000 | (6,6) | |
| 22 | YawR | YawRate | 2 | 20000 | (6,6) | |
| 23 | rpm | RPM | 4 | 100000 | (2,4) | CAN float |
| 24 | Gear | Gear | 8 | 100000 | (2,4) | enum |
| 25 | Spd | Speed | 4 | 100000 | (2,4) | |
| 26 | WSRL | Wheel_Speed_RL | 4 | 100000 | (2,4) | |
| 27 | WSRR | Wheel_Speed_RR | 4 | 100000 | (2,4) | |
| 28 | WSFL | Wheel_Speed_FL | 4 | 100000 | (2,4) | |
| 29 | WSFR | Wheel_Speed_FR | 4 | 100000 | (2,4) | |
| 30 | Accx | Long_Acc | 4 | 100000 | (2,4) | |
| 31 | Accy | Lat_Acc | 4 | 100000 | (2,4) | |
| 32 | Yaw | Yaw_Rate | 4 | 100000 | (2,4) | |
| 33 | ECT | Eng_T | 4 | 100000 | (2,4) | |
| 34 | OILT | Oil_T | 4 | 100000 | (2,4) | |
| 35 | AAT | Amb_T | 4 | 100000 | (2,4) | |
| 36 | Geat | Gear_T | 4 | 100000 | (2,4) | |
| 37 | BRKF | Brake_P_F | 4 | 100000 | (2,1) | |
| 38 | BRKR | Brake_P_R | 4 | 100000 | (2,1) | |
| 39 | BARO | Ambient_P | 4 | 100000 | (2,4) | |
| 40 | STRA | Steering_Angle | 4 | 100000 | (2,4) | |
| 41 | TPS | Throttle | 4 | 100000 | (2,4) | |
| 42 | PPS | Pedal_Pos | 4 | 100000 | (2,4) | |
| 43 | Load | Eng_Load | 4 | 100000 | (2,4) | |
| 44 | Odo | Odometer | 4 | 100000 | (2,3) | |
| 45 | KmFue | Fuel_km | 4 | 100000 | (2,4) | |
| 46 | Vb | Battery_Volt | 4 | 100000 | (2,4) | |
| 47 | Fuel | Fuel_used | 4 | 100000 | (2,4) | |
| 48 | GbxTq | Gbx_Torque | 4 | 100000 | (2,4) | |
| 49 | TRQ | Eng_Torque | 4 | 100000 | (2,4) | |
| 50 | Curr | Current_IBS | 4 | 100000 | (2,1) | |
| 51 | ABS | ABS | 4 | 100000 | (2,4) | |
| 52 | ASC | ASC | 4 | 100000 | (2,4) | |
| 53 | Brake | Brake | 4 | 100000 | (2,4) | |
| 54 | FRaw | Fuel_Raw_ul | 4 | 100000 | (2,4) | |
| 55 | Ind_L | Indicator_lights | 8 | 100000 | (2,4) | enum |
| 56 | Flamp | Fuel_Lamp | 8 | 100000 | (2,4) | enum |
| 57 | HiBm | Hi_Beam | 8 | 100000 | (2,4) | enum |
| 58 | Mode | Eng_Mode | 8 | 100000 | (2,4) | enum |
| 59 | DSC | DSC | 8 | 100000 | (2,4) | enum |
| 60 | Clut | Clutch_Sw | 8 | 100000 | (2,4) | enum |
| 61 | RPMM | Rpm_MAX | 4 | 100000 | (2,4) | |
| 62 | Heat | Eng_Heat_St | 8 | 100000 | (2,4) | enum |
| 63 | StrtRec | StrtRec | 2 | 0 | (515,6) | record-state flag |
| 64 | iGPS | iGPS | 56 | 100000 | (2,255) | GPS tag-frame based |

## 5. CDE and GRP Records

### 5.1 CDE

- payload length = `6 B`
- structure:

```text
<u16 channel_id><4B opaque value>
```

The first `u16` matches the `CHS` channel id one-to-one. The trailing 4 bytes
look like a channel-specific hash/signature but are not required for parsing.

### 5.2 GRP

Confirmed payload layout:

```text
<u16 group_id><u16 count><u16 channel_id[count]>
```

Observed groups:

| gid | channels | Notes |
| ---: | --- | --- |
| 100 | 23, 48, 49 | rpm + GbxTq + TRQ |
| 101 | 42 | PPS |
| 102 | 37, 38 | BRKF + BRKR |
| 103 | 51, 52, 53 | ABS + ASC + Brake |
| 104 | 30 | Accx |
| 105 | 31 | Accy |
| 106 | 32 | Yaw |
| 107 | 25 | Spd |
| 108 | 50 | Curr |
| 109 | 57 | HiBm |
| 110 | 55 | Ind_L |
| 111 | 26, 27, 28, 29 | four wheel speeds |
| 112 | 46 | Vb |
| 113 | 54 | FRaw |
| 114 | 35 | AAT |
| 115 | 40 | STRA |
| 116 | 44, 45, 47, 56 | Odo + Fuel_km + Fuel_used + Fuel_Lamp |
| 117 | 41, 43 | TPS + Load |
| 118 | 36 | Geat |
| 119 | 58, 59 | Eng_Mode + DSC |
| 120 | 24, 33, 34, 60, 61 | Gear + ECT + OILT + Clut + RPMM |
| 121 | 39, 62 | BARO + Heat |

## 6. Body Sample Stream

From offset `0x002f89` to EOF, two kinds of content are interleaved:

1. parenthesized records: `S`, `M`, `G`
2. tag frames: `GPS`, `GNFI`, `LAP`, `GPSR`, `ODO`, `CNF`

Observed counts in the sample session:

- `S`: `63,091`
- `M`: `24,366`
- `G`: `71,475`
- tag frames: `5,411`
  - `GPS`: `5,130`
  - `GNFI`: `254`
  - `LAP`: `23`
  - `ODO`: `2`
  - `CNF`: `1`
  - `GPSR`: `1`

### 6.1 `S` records

`S` is a single-channel record with two payload widths.

#### `S4`

```text
( 'S' <u32 tick> <u16 ch_id> <4-byte value_bits> )
```

- total length = `13 B`

Main observed channels:

- `ch0 = MClk`
- `ch14 = DistL`
- `ch15 = DistLI`
- `ch2..6 = lap helper values`

Confirmed interpretation:

- `ch0`
  - `value_bits == tick`
  - master session timebase
  - first value `1102643`, last value `1615593`
- `ch14`, `ch15`
  - IEEE754 float32 LE
  - distance in meters
- `ch2..6`
  - predictive/lap-diff helper values
  - often `NaN` or small raw-bit patterns
  - direct CSV mapping still unresolved

#### `S2`

```text
( 'S' <u32 tick> <u16 ch_id> <u16 raw> )
```

- total length = `11 B`

Observed channels:

- `ch12 = IBat`
- `ch13 = VBat`
- `ch63 = StrtRec`

Confirmed interpretation:

- `ch12`, `ch13`
  - decode `raw` as IEEE754 binary16
  - CSV value = `half_float(raw) / 1000`
  - examples:
    - `0x6bca -> 3988.0 -> 3.988 V`
    - `0x7299 -> 13512.0 -> 13.512 V`
- `ch63`
  - 0/1 flag
  - mostly `1` during the session, `0` near the end
  - likely recording/start-state related

### 6.2 `M` records

Structure:

```text
( 'M' <u32 tick> <u16 ch_id> <u16 count> <i16[count]> )
```

- total length = `11 + 2 * count`

Confirmed channels and block sizes:

| ch | Meaning | count | Effective rate | Notes |
| ---: | --- | ---: | ---: | --- |
| 9 | MgX | 10 | 50 Hz | int16 raw |
| 10 | MgY | 10 | 50 Hz | int16 raw |
| 11 | MgZ | 10 | 50 Hz | int16 raw |
| 16 | RPM dup 1 | 2 | 20 Hz | all zero in this session |
| 17 | InlineAcc | 20 | 100 Hz | int16 raw |
| 18 | LateralAcc | 20 | 100 Hz | int16 raw |
| 19 | VerticalAcc | 20 | 100 Hz | int16 raw |
| 20 | RollRate | 10 | 50 Hz | int16 raw |
| 21 | PitchRate | 10 | 50 Hz | int16 raw |
| 22 | YawRate | 10 | 50 Hz | int16 raw |

Notes:

- `count * period_us` matches the block time span
- sample values are confirmed signed int16 raw values
- engineering-unit scaling and calibration are still unresolved

Observed examples show that a simple per-axis scalar is not sufficient, which
suggests calibration matrices or additional sensor conversion data.

### 6.3 `G` records

Structure:

```text
( 'G' <u32 tick> <u16 group_id> <typed payload> )
```

- total length = `9 + sum(CHS[ch].width for ch in group)`

Type rules:

- float channels (`width=4`): IEEE754 float32 LE
- enum channels (`width=8`):

```text
<u16 code><ascii[6] zero-padded label>
```

Because the label is fixed-width 6 bytes, long strings are truncated:

- `green -> "gree"`
- `ready -> "read"`
- `empty -> "empt"`
- `deactivated -> "deac"`

Confirmed examples:

| gid | First tick | Decoded meaning | CSV |
| ---: | ---: | --- | --- |
| 112 | 1102562 | `Vb = 14295.0` | `Battery_Volt = 14295` |
| 113 | 1102562 | `FRaw = 36505.0` | `Fuel_Raw_ul = 36505` |
| 100 | 1102617 | `rpm=2298.5, GbxTq=225.0, TRQ=225.5` | matches |
| 109 | 1102615 | `HiBm = (0,"off")` | 0 |
| 110 | 1102615 | `Ind_L = (0,"off")` | 0 |
| 116 | 1102617 | `Odo, Fuel_km, Fuel_used, Flamp=(3,"empt")` | matches |
| 120 | 1102618 | `Gear=(8,"4"), ECT=93, OILT=114, Clut=(0,"off"), RPMM=7500` | matches |
| 121 | 1102619 | `BARO=0.972, Heat=(1,"read")` | matches |

Practically, `G` decoding can be treated as confirmed.

## 7. Tag Frames in the Body

### 7.1 `GPS`

- tag: `GPS`
- class: `0x01`
- payload length: `56 B`
- count: `5,130`

Layout:

```text
14 x u32 LE
```

| Word | Meaning | Unit / scale | Status |
| ---: | --- | --- | --- |
| 0 | local master tick | ms | confirmed |
| 1 | GPS time-of-week | ms | confirmed |
| 2 | local delta tick | ms | strong inference |
| 3 | session constant | - | unresolved |
| 4 | ECEF X | cm, i32 | confirmed |
| 5 | ECEF Y | cm, i32 | confirmed |
| 6 | ECEF Z | cm, i32 | confirmed |
| 7 | `GPS PosAccuracy / 10` | 10 mm units | confirmed |
| 8 | ECEF vX | cm/s, i32 | confirmed |
| 9 | ECEF vY | cm/s, i32 | confirmed |
| 10 | ECEF vZ | cm/s, i32 | confirmed |
| 11 | `GPS SpdAccuracy / 0.036` | 0.036 km/h units | confirmed |
| 12 high byte | `GPS Nsat` | count | confirmed |
| 12 low 24 bits | GPS flags / quality | - | unresolved |
| 13 | constant `0x00001000` | - | observed |

Example:

```text
word1  = 441503900 -> 02:38:23.900 UTC -> 11:38:23.900 KST
word4  = -311858199 cm
word5  =  394965550 cm
word6  =  390600675 cm
word7  = 517  -> PosAccuracy = 5170 mm
word8  = -172 cm/s
word9  = 1175 cm/s
word10 =   35 cm/s
word11 = 35   -> SpdAccuracy = 1.26 km/h
word12 = 0x09020103 -> high byte 0x09 = 9 satellites
```

Position reconstruction:

- interpret words 4/5/6 as WGS-84 ECEF in centimeters
- convert to geodetic latitude / longitude / altitude
- this matches the CSV to centimeter-level accuracy

Speed / heading / slope reconstruction:

```text
(vX, vY, vZ) = (w8, w9, w10)  [cm/s]
lat, lon     = ecef_to_llh(w4/100, w5/100, w6/100)

vE = -sin(lon)*vX + cos(lon)*vY
vN = -sin(lat)*cos(lon)*vX - sin(lat)*sin(lon)*vY + cos(lat)*vZ
vU =  cos(lat)*cos(lon)*vX + cos(lat)*sin(lon)*vY + sin(lat)*vZ

GPS Speed   = sqrt(vE^2 + vN^2) * 3.6 / 100
GPS Heading = atan2(vE, vN) * 180 / pi
GPS Slope   = atan2(vU, sqrt(vE^2 + vN^2)) * 180 / pi
```

First-sample comparison:

- computed: `60.275 km/h`, `-29.336 deg`, `1.383 deg`
- CSV: `60.293`, `-29.3385`, `1.3818`

### 7.2 `GNFI`

- tag: `GNFI`
- class: `0x00`
- payload length: `32 B`
- count: `254`

Structure:

```text
(a, b, c, d, e, 0, 0, 0)   ; 8 x u32 LE
```

First sample:

```text
(1103993, 0, 441506000, 1103765, 440401235, 0, 0, 0)
```

Observed characteristics:

- appears about every 2.02 seconds
- `c` increases by about 2000 every frame
- `a` and `d` also increase steadily
- `e` is nearly constant

Strong inference: this is a local-tick / GPS-absolute-time mapping or status
update record.

### 7.3 `LAP`

- tag: `LAP`
- class: `0x03`
- payload length: `20 B`
- count: `23`

Structure:

```text
(f0, f1, f2, f3, f4_tick)   ; 5 x u32 LE
```

Interpretation:

- `f4`: absolute master tick in ms
- `f1`: current lap/section elapsed ms
- `f2`: usually same as `f1`; at boundaries it keeps the last completed lap time
- `f0`: lap index plus event-class bits
- `f3`: usually `0x00000204`, with extra high bits on boundary/terminal events

The values line up directly with CSV beacon markers such as:

- `117.612`
- `261.487`
- `380.653`
- `510.881`
- `513.996`

using:

```text
(f4 - first_MClk) / 1000
```

### 7.4 `GPSR`

- tag: `GPSR`
- class: `0x01`
- payload length: `36 B`
- count: `1`

Appears once immediately after the second `CNF`.

Strong inference: receiver/config snapshot recorded when the `iGPS` channel is
redefined.

### 7.5 `ODO`

- payload length: `384 B`
- observed `3` times total

Visible labels include:

- `System`
- `Usr 1`
- `Usr 2`
- `Usr 3`
- `Usr 4`
- `Fuel Used`

This looks like a fixed-width summary snapshot rather than a streamed sample.

### 7.6 Mid-body `CNF`

The second `CNF` inside the body is not a new section boundary. It should be
consumed as an ordinary body tag frame that updates the schema.

## 8. Timebase

- first session `MClk` tick: `1,102,643 ms`
- last session `MClk` tick: `1,615,593 ms`
- difference: `512,950 ms` ~= `513.0 s`

Therefore:

- the device's monotonic millisecond clock is the master session time axis
- all `S/M/G` record ticks, `GPS.word0`, and `LAP.f4` share that same axis
- CSV `Time=0.000` corresponds to the first `MClk` tick

Also, `GPS.word1 = 441,503,900` is Friday `02:38:23.9 UTC`, which becomes
`11:38:23.9 KST` and matches the CSV session header time.

## 9. CSV <-> Raw Mapping Summary

Only confirmed mappings are listed here.

| CSV column | Source | Formula / interpretation |
| --- | --- | --- |
| Time | `MClk` `S` record | `(tick - first_MClk) / 1000` |
| Internal Battery | `ch12 S2` | `half_float(raw) / 1000` V |
| External Voltage | `ch13 S2` | same |
| Distance Lap | `ch14 S4` | `float32(raw)` m |
| Distance Lap Int | `ch15 S4` | same |
| RPM dup 1 | `ch16 M` | raw i16, all zero in this session |
| RPM dup 2 | `gid 100` | float32 |
| Speed | `gid 107` | float32 km/h |
| Wheel_Speed_RL/RR/FL/FR | `gid 111` | four float32 values |
| Long_Acc / Lat_Acc / Yaw_Rate | `gid 104/105/106` | float32 |
| Eng_T / Oil_T / Gear_T | `gid 120/118` | float32 |
| Amb_T | `gid 114` | float32 |
| Brake_P_F / Brake_P_R | `gid 102` | float32 |
| Ambient_P | `gid 121` | float32 |
| Steering_Angle | `gid 115` | float32 |
| Throttle / Pedal_Pos / Eng_Load | `gid 101/117` | float32 |
| Odometer / Fuel_km / Fuel_used | `gid 116` | float32 |
| Battery_Volt | `gid 112` | float32 mV |
| Gbx_Torque / Eng_Torque | `gid 100` | float32 |
| Current_IBS | `gid 108` | float32 |
| Fuel_Raw_ul | `gid 113` | float32 |
| Gear / Eng_Mode / DSC / Clutch_Sw / Fuel_Lamp / Hi_Beam / Indicator_lights / Eng_Heat_St | enum `G` records | numeric `u16` code; labels not directly used by CSV |
| GPS Latitude / Longitude / Altitude | `GPS.word4/5/6` | ECEF cm -> WGS-84 |
| GPS Speed | `GPS.word8/9/10` | ECEF velocity -> ENU -> km/h |
| GPS Heading | `GPS.word8/9/10` | `atan2(vE, vN)` |
| GPS Slope | `GPS.word8/9/10` | `atan2(vU, sqrt(vE^2+vN^2))` |
| GPS PosAccuracy | `GPS.word7` | `word7 * 10` mm |
| GPS SpdAccuracy | `GPS.word11` | `word11 * 0.036` km/h |
| GPS Nsat | `GPS.word12 >> 24` | count |
| Beacon Markers | `LAP.f4` | `(f4 - first_MClk) / 1000` |

## 10. Parser Skeleton for Implementation

```python
def parse_file(raw: bytes):
    off = 0
    chs = []
    grp = {}

    # 1) leading top-level tag frames
    while raw[off:off+2] == b"<h":
        tag, length, cls, payload, nxt = parse_frame(raw, off)
        if tag == "CNF":
            for t2, _, _, p2 in iter_inner_frames(payload):
                if t2 == "CHS":
                    chs.append(decode_chs(p2))
                elif t2 == "GRP":
                    g = decode_grp(p2)
                    grp[g.gid] = g.channels
        off = nxt
        if off >= 0x2F89:
            break

    # 2) body stream
    while off < len(raw):
        if raw[off:off+2] == b"<h":
            tag, length, cls, payload, nxt = parse_frame(raw, off)
            if tag == "CNF":
                # second CNF: ch64(iGPS) redefinition
                ...
            elif tag == "GPS":
                words = struct.unpack("<14I", payload)
                emit_gps(words)
            elif tag == "LAP":
                emit_lap(struct.unpack("<5I", payload))
            elif tag == "GNFI":
                emit_gnfi(struct.unpack("<8I", payload))
            elif tag == "GPSR":
                emit_gpsr(payload)
            elif tag == "ODO":
                emit_odo(payload)
            off = nxt
            continue

        assert raw[off] == 0x28  # '('
        typ = raw[off + 1]
        tick, key = struct.unpack_from("<IH", raw, off + 2)

        if typ == 0x53:  # 'S'
            w = infer_s_width_from_channel(chs[key])
            payload = raw[off + 8:off + 8 + w]
            assert raw[off + 8 + w] == 0x29
            emit_S(tick, key, payload)
            off += 9 + w
            continue

        if typ == 0x4D:  # 'M'
            cnt = struct.unpack_from("<H", raw, off + 8)[0]
            samples = struct.unpack_from(f"<{cnt}h", raw, off + 10)
            assert raw[off + 10 + 2 * cnt] == 0x29
            emit_M(tick, key, samples)
            off += 11 + 2 * cnt
            continue

        if typ == 0x47:  # 'G'
            channels = grp[key]
            size = sum(chs[c].width for c in channels)
            payload = raw[off + 8:off + 8 + size]
            assert raw[off + 8 + size] == 0x29
            emit_G(tick, key, channels, payload)
            off += 9 + size
            continue

        raise ParseError(f"unknown record type {typ:#x} at {off:#x}")
```

Core implementation rules:

- recover `CHS` and `GRP` from `CNF1` first
- then parse the body as a mixed stream of `S/M/G` records and tag frames
- allow the later `CNF2`
- decode `G` payloads using the group definition plus channel widths

## 11. Open Questions

1. IMU / magnetometer raw `i16` -> engineering-unit scaling and calibration
2. `GPS.word2`, `GPS.word3`, and the low 24 bits of `GPS.word12`
3. Exact formula for CSV `GPS LatAcc`, `GPS LonAcc`, `GPS Gyro`, `GPS Radius`
4. Full field decoding of `GNFI` and `GPSR`
5. Meaning of opaque `CHS` fields at `0x0c`, `0x10`, `0x14`, `0x58`, `0x68`,
   `0x6c`
6. Exact meaning of the trailing 4 bytes in each `CDE`
7. Metadata tags `SRC`, `iSLV`, `HWNF`, `ENF`, `PDLT`, `TMD`, `TMT`
8. Detailed coordinate/shape data inside the `TRK ` frame

## 12. Practical Takeaways

- after downloading `xrz`, zlib inflate gives the `raw` body
- `xrk` is `raw + 120B footer`, so an `xrz -> xrk` regenerator is practical
- once `CNF` has been decoded, the body `S/M/G` records parse cleanly
- CAN and enum data in `G` records map almost directly to CSV
- GPS position and velocity reconstruct well from ECEF + ECEF velocity
- `M` records are structurally solved, but calibration remains unresolved

That makes two immediate implementation targets realistic:

1. `xrz -> xrk` generator
2. `raw/xrk -> structured JSON/CSV` parser

Both can be started directly from this document.
