# AiM Data Logger Wi-Fi Protocol Spec

It combines the initial reverse-engineering notes, byte-level revalidation, and
real-device validation results confirmed on 2026-04-22.

Scope:

- target workflow: connect -> list sessions -> download files
- target device family observed: AiM SOLO2DL
- real-device validation: another SOLO2DL unit on 2026-04-22

Conventions:

- unless stated otherwise, all integers are little-endian
- "firmware-dependent" means the capture layout was not stable across units

Important constraints discovered on real hardware:

- AiM AP IP is not fixed at `10.0.0.1`. On tested devices it could be
  `10.0.0.1`, `11.0.0.1`, `12.0.0.1`, or `14.0.0.1`.
- Implementations must not hardcode the AP IP. Use the source IP address from
  UDP discovery replies.
- A short post-connect settle delay matters. Sending the hello frame
  immediately after TCP connect can fail on some units.
- Stable sessions matched the vendor app only when UDP `aim-ka` was sent
  continuously before and during the TCP session.

## 1. What Is Common vs Firmware-Dependent

| Item | Stability |
| --- | --- |
| UDP port `36002`, probe bytes `"aim-ka"` | Common |
| UDP reply source port = `36002` | Common |
| AP IP is one of `10/11/12/14.0.0.1` | Confirmed on real hardware |
| UDP payload length / `version` / `idn` offset | Firmware-dependent; do not use as a filter |
| TCP port `2000`, outer `<h...>` / `<...>` framing | Common |
| Checksum = `sum(payload) & 0xffff` | Common |
| About `0.4s` settle after TCP connect and before hello | Important on real hardware |
| 8-byte hello (`06 08` / `06 09`) exchange | Common and required |
| 64-byte `cmd=0x10/0x01` plus 68-byte time sync bootstrap | Required for list / download / device-info; not needed for delete-only sessions (see §10) |
| Request `status=0x00000001` marker | Required on some firmware; safest to always send |
| Continuous UDP `aim-ka` while TCP is active | Common to stable implementations and vendor app |
| List prep `0x51/0x02` twice, then `dev.ria` probe | Common, with two cache/fresh paths |
| Download chunk payload size `32704` | Common in observed captures; other models unverified |

## 2. Network Layout

| Port | Protocol | Direction | Purpose |
| --- | --- | --- | --- |
| `36002` | UDP | bidirectional | discovery / keep-alive |
| `2000` | TCP | device listens | metadata, list, file download |

### 2.1 Observed vendor app session split

| Local Port | Role | Total Traffic |
| --- | --- | --- |
| `8835` | session init + metadata + list | about 75 KB |
| `6830` | actual `.xrz` download | about 1.8 MB |

The vendor app behaves as "one TCP connection per task". A CLI can safely
follow the same rule, although a single long-lived session also worked in
practice for list + download.

### 2.2 Unrelated broadcasts

Traffic such as `10.0.0.255:22222`, `10.0.0.255:3289`, and
`255.255.255.255:10004` came from unrelated vendor discovery protocols and can
be ignored.

## 3. UDP Discovery on Port 36002

### 3.1 Client -> device

The client repeatedly sends the 6-byte ASCII payload `"aim-ka"`:

```text
61 69 6d 2d 6b 61
```

Observed cadence was roughly once per second.

This traffic acts both as discovery and as a keep-alive/"I am connected"
signal.

Important: both the stable CLI implementation and the vendor app kept this UDP
traffic flowing from before the TCP connection started until the TCP session
ended.

### 3.2 Device -> client

Observed reply length was often `236` bytes, but the internal payload layout
was not stable across devices.

Observed capture layout on SOLO2DL #504986:

```text
offset  size  observed meaning (capture-specific)
0       4     0x000000ec    ; 236 payload length
4       4     0x00000002    ; version/type
8       4     device IP in network byte order
12      4     reserved
16      4     0x0000001e    ; likely model code
20      104   device name ASCII
124     3     "idn"
127     109   bytes matching the iMST block prefix
```

Real-device validation on another unit showed that bytes after offset 124 could
be all zero, with no `idn` signature at all. Therefore:

- do not parse discovery based on internal payload structure
- do not require `idn`
- do not trust the encoded IP field more than the packet source IP

### 3.3 Discovery implementation rules

Use only these filters:

- UDP reply source port must be `36002`
- payload must not be your own `"aim-ka"` echo

The authoritative device IP is the source IP address from `recvfrom()`.

The client should bind its UDP socket to `:36002`. Some firmware replies to the
sender's ephemeral port; others reply only to port `36002`. Binding there
covers both cases and matches the vendor app behavior.

## 4. TCP Framing on Port 2000

All control and data traffic shares the same outer frame format.

### 4.1 Outer frame

```text
offset  size  value
0       2     "<h"                       (0x3c 0x68)
2       4     TAG (4 ASCII bytes)
6       4     length (u32 LE)
10      2     "\x00>"                   header terminator
12      N     payload
12+N    1     "<"
13+N    4     TAG again
17+N    2     checksum (u16 LE)
19+N    1     ">"
```

- total frame size = `20 + length`
- observed outer tags:
  - `STCP`: status/control/data carrier
  - `STNC`: "start new command" from client

### 4.2 Inner blocks inside STCP payloads

Inner blocks use the same layout except the inner header terminator is `"a>"`
instead of `"\x00>"`.

Observed inner tags include:

- `iMST`
- `iHW `
- `iUSR`
- `iPTH`
- `iLCK`
- `iSST`
- `iLTS`
- `iPRL`

The nested `USR ` block inside `iUSR` is a variant format that uses:

- 10 ASCII digits for length
- 6 ASCII hex digits for checksum

### 4.3 Checksum

For both outer and inner frames:

```text
checksum = sum(payload bytes) & 0xffff
```

Only payload bytes are included. Tags, lengths, and markers are excluded.

### 4.4 Stream reassembly is mandatory

TCP segment boundaries do not align with protocol frame boundaries:

- a segment may contain only part of a frame
- a segment may contain multiple frames
- a parser must scan for `<h`, parse `length`, and buffer until `20 + length`
  bytes are available

Never assume one `recv()` call equals one frame.

## 5. The 64-Byte Command Header

Most control packets use an outer payload of exactly `64` bytes.

```text
offset  size  name
0       8     session/reserved
8       2     cmd
10      2     sub
12      4     flags/reserved
16      4     size
20      4     server_tok
24      4     status
28      4     reserved
32      32    arg
```

Field behavior:

- `size`
  - request: `0` or a fixed command-specific value
  - response: data length that will follow
- `server_tok`
  - observed as `0x00007fc0` in server replies
- `arg`
  - null-terminated ASCII path up to 32 bytes, or raw flag bytes

### 5.1 Status codes at offset 24

| Value | Meaning |
| --- | --- |
| `0x00000001` | client request marker; always send it |
| `0x00000a01` | received / ack-only |
| `0x00000a09` | pending |
| `0x00000a11` | ready; `size` is valid |
| `0x00000a1d` | empty / none |

The top byte `0x0a` acts as a status response marker. The low byte carries the
actual status value.

Real-device validation showed that some firmware returns no reply at all if the
client request frame does not set `status=0x00000001`.

### 5.2 Data-bearing STCP frames

`status=0x0a11` means "ready to send data", not "data is already in this same
frame".

For observed body-bearing commands such as:

- `0x10/0x01` device info
- `0x24/0x02` session list CSV
- `0x02/0x04` file read

the server typically sends:

1. `0xa11 size=N`
2. waits for a 4-byte client ACK with offset `0`
3. then sends the actual data frame

Data STCP payload layout:

```text
offset  size  meaning
0       4     data_offset (u32 LE)
4       N-4   actual data bytes
```

So the `size` field in the 64-byte status header matches `outer_length - 4` of
the data frame.

### 5.3 Four-byte STCP control frames

Short 4-byte `STCP` payloads are used for flow control:

- client ACK: "send me data starting at this offset"
- server delimiter: often `00 00 00 00`, especially around time sync and status
  interleaving

Parsers must treat these as a separate short-frame case rather than trying to
decode them as 64-byte status headers.

## 6. Connection Establishment

### 6.1 8-byte hello

The first two TCP frames are:

```text
C -> S : STCP 8B  00 00 00 00 06 08 00 00
S -> C : STCP 8B  00 00 00 00 06 09 00 00
```

- byte 4 is fixed at `0x06`
- byte 5 toggles `0x08` -> `0x09`
- without this hello exchange, later commands are not accepted

Important real-device finding: some units fail if the hello is sent
immediately after `connect()`. Waiting about `0.4s` first matched the vendor
capture timing and made sessions reliable.

### 6.2 Mandatory bootstrap: `0x10/0x01` plus 68-byte time sync

Real-device validation on 2026-04-22 showed that if the client sends list prep
(`0x51/0x02`) immediately after hello, without the bootstrap sequence, the
device may remain silent.

Observed interleaved bootstrap order:

```text
C -> S : STNC 64B   cmd=0x10/0x01, status=req, arg[0]=0x01
S -> C : STCP 64B   cmd=0x10/0x01, status=0xa01
C -> S : STCP 68B   time-sync
S -> C : STCP 4B    payload=0
S -> C : STCP 64B   cmd=0x10/0x01, status=0xa11, size=3225
C -> S : STCP 4B    payload=0
S -> C : STCP 3229B offset(4B=0) + 3225B device-info block
```

Deadlock rule:

- do not wait for `0xa11` before sending time sync
- once `0xa01` is received, send the 68-byte time sync immediately
- only then wait for `0xa11`

If the client waits for `0xa11` first, both sides can block forever.

The 2026-04-24 delete capture showed that this bootstrap is only
required for `0x10/0x01`-class flows (device info, list, download).
A vendor-app session that issued only delete commands went straight
from hello to `0x06/0x04` and worked. See §10.

### 6.3 The 68-byte time sync payload

```text
offset  size  meaning
0       12    zero padding
12      4     UTC year
16      4     UTC month
20      4     UTC day
24      4     UTC hour
28      4     UTC minute
32      4     zero
36      8     zero padding
44      4     local year
48      4     local month
52      4     local day
56      4     local hour
60      4     local minute
64      4     zero
```

Observed example:

- UTC: `2026-04-21 09:24`
- local: `2026-04-21 18:24` (KST)

### 6.4 Receive-loop requirement

During bootstrap, a short 4-byte `STCP payload=0` frame may appear between
64-byte status frames. A correct receive loop must skip it and continue.

## 7. Command Catalog

Notation is `cmd / sub`, both in hex.

| cmd | sub | u32 LE | Role | Arg bytes | Response |
| --- | --- | --- | --- | --- | --- |
| `0x10` | `0x01` | `0x00010010` | full device info | `0x01` + zero padding | 3225B nested block stream |
| `0x06` | `0x01` | `0x00010006` | light state ping | none | `size=0` |
| `0x06` | `0x04` | `0x00040006` | file delete | path up to 32B | `size=0`; arg echoes path |
| `0x02` | `0x04` | `0x00040002` | file read | path up to 32B | `size` + chunked stream |
| `0x24` | `0x02` | `0x00020024` | session list | none | 13490B CSV |
| `0x24` | `0x06` | `0x00060024` | system info page | none | 320B binary |
| `0x28` | `0x06` | `0x00060028` | unknown | none | always empty |
| `0x51` | `0x02` | `0x00020051` | list prep | `0xffffffff` | `size=0` |
| `0x03` | `0x02` | `0x00020003` | category/page fetch | none | 410B |
| `0x53` | `0x02` | `0x00020053` | category marker pair | none | 12B |
| `0x02` | `0x02` | `0x00020002` | config dump `hhh` | none | 8588B |
| `0x08` | `0x02` | `0x00020008` | config dump `hhi` | none | 1828B |
| `0x09` | `0x02` | `0x00020009` | config dump `hhf` | none | 4284B |

For a list/download CLI, only these four are essential:

- `0x10/0x01` bootstrap init
- `0x51/0x02` list prep
- `0x24/0x02` list fetch
- `0x02/0x04` file read

About the `size` field on `0x10/0x01` requests:

- observed in the original capture as `0x40`
- real-device validation showed that some units also accept `size=0`
- the safest choice is still to send `0x40`

### 7.1 Non-CLI commands worth preserving

- `0x24/0x06`: 320-byte response beginning with ASCII `"System"`
- `0x28/0x06`: observed only as `0xa1d` empty
- `0x03/0x02` and `0x53/0x02`: appear to be paired category/page operations
- `0x02/0x02`, `0x08/0x02`, `0x09/0x02`: configuration dumps with signatures
  like `hhh\x01`, `hhi\x01`, `hhf\x01`

## 8. Session List Protocol

### 8.1 Observed call sequence

```text
C -> S : STNC 0x51/0x02 arg=0xffffffff
S -> C : STCP status=0xa09
S -> C : STCP status=0xa11 size=0

C -> S : STNC 0x51/0x02 again
S -> C : STCP status=0xa09
S -> C : STCP status=0xa11 size=0

C -> S : STNC 0x02/0x04 path="0:/tkk/dev.ria"
S -> C : STCP status=0xa09
S -> C : STCP status=0xa1d

C -> S : STNC 0x24/0x02
S -> C : STCP status=0xa09
S -> C : STCP status=0xa11 size=13490
C -> S : STCP 4B payload=0
S -> C : STCP 13494B offset(0) + CSV(13490B)
```

### 8.2 The two possible paths

`0:/tkk/dev.ria` appears to be a list cache file.

Observed and validated path:

- `dev.ria` returns `0xa1d`
- client falls back to `0x24/0x02`

Likely but still unverified path:

- if `dev.ria` exists, it may return `0xa11` with the CSV directly
- in that case `0x24/0x02` would not be needed

Safe implementation strategy:

- always run the full observed sequence
- if `dev.ria` returns data, use it and stop
- if it returns `0xa1d`, continue to `0x24/0x02`

### 8.3 Why prep runs twice

Best current hypothesis: `0xffffffff` acts as an "invalidate everything" marker,
and the two calls reset both cached state and fresh CSV generation state.

### 8.4 CSV format

Observed header:

```text
name,size,date,hour,nlap,nbest,best,pilota,track_name,veicolo,campionato,venue_type,mode,trk_type,motivolap,maxvel,device,track_lat,track_lon,test_dur,pname,ptype,ptime,pdist,pmaxv,
```

Key fields:

| Column | Meaning |
| --- | --- |
| `name` | file name such as `a_7064.xrz` or `.hrz` |
| `size` | compressed file size in bytes |
| `date` | `DD/MM/YYYY` |
| `hour` | local device time |
| `nlap` | lap count |
| `nbest` | best lap index |
| `best` | best lap time in ms |
| `pilota` | driver name |
| `track_name` | e.g. `Inje NCK`, `Yongin` |
| `mode`, `trk_type`, `motivolap` | session metadata |
| `track_lat`, `track_lon` | fixed-point degrees at `1e-7` scale |

Files listed here map to:

```text
1:/mem/<name>
```

### 8.5 List data is just another streamed body

The 13494-byte `STCP` body has the same shape as a normal file chunk:

```text
offset(u32 LE = 0) + CSV bytes
```

So a generic "pull a streamed body after `0xa11 size=N`" implementation can
serve both list and file download.

## 9. File Download Protocol

### 9.1 Sequence

```text
C -> S : STNC 0x02/0x04 path="1:/mem/a_7064.xrz"
S -> C : STCP status=0xa09
S -> C : STCP status=0xa11 size=1603995

C -> S : STCP 4B next_offset=0
S -> C : STCP 32708B offset=0      + 32704B data
C -> S : STCP 4B next_offset=32704
S -> C : STCP 32708B offset=32704  + 32704B data
...
C -> S : STCP 4B next_offset=1602496
S -> C : STCP 1503B  offset=1602496 + 1499B data
```

### 9.2 Chunk rules

- max data payload per chunk: `32704` bytes
- outer payload size at max chunk: `32708` bytes
- last chunk carries only remaining bytes
- number of chunks = `ceil(file_size / 32704)`
- this is a pull model: the server sends the next chunk only after the client
  transmits the next offset

### 9.3 Resume capability

Only sequential `offset=0 -> ... -> end` transfers were observed.
Resuming from an arbitrary offset is plausible but remains unverified.

### 9.4 Download completion

Transfer ends when accumulated data length matches the announced `size`.

The capture showed plain TCP FIN rather than an explicit application-level
close frame. Whether some firmware requires an explicit close frame is still
unverified.

### 9.5 XRZ and HRZ files

- `.xrz`: full session with laps
- `.hrz`: shorter session / hot-lap variant
- both appear to use the same container family
- they begin with zlib bytes such as `78 01`
- the CLI can safely save the received bytes as-is

## 10. File Delete Protocol

### 10.1 Sequence

```text
C -> S : STNC 0x06/0x04 path="1:/mem/a_6994.xrz"
S -> C : STCP status=0xa09  arg="1:/mem/a_6994.xrz"
S -> C : STCP status=0xa11  size=0  arg="1:/mem/a_6994.xrz"
```

The `0xa11` reply with `size=0` signals completion. No body stream
follows. The client must not send an offset ACK afterwards.

### 10.2 Distinguishing features

Two things make delete responses different from every other observed
command:

- both `0xa09` and `0xa11` echo the requested path back in the
  arg field of the 64-byte command header. List, download, and
  device-info responses leave that field empty. Path echo is a
  useful matching signal when several deletes are in flight on the
  same connection.
- there is no body stream. `size=0` plus the `0xa11` status is the
  whole result.

### 10.3 Multiple-file deletion

There is no batch or wildcard delete command. Each `0x06/0x04`
request carries exactly one path in its 32-byte arg field.

To delete N files, the vendor app issues N sequential requests on
one TCP connection, waiting for each `0xa11` before sending the
next. Pipelining (sending the next request before `0xa11` arrives)
was not observed and is unverified.

### 10.4 Session-level requirements

The vendor app delete capture started a fresh TCP connection and
went straight from hello to delete:

```text
TCP SYN/ACK
~0.4s settle
C -> S : STCP 8B hello   06 08
S -> C : STCP 8B hello   06 09
C -> S : delete a_6994.xrz   ->  0xa09 / 0xa11
C -> S : delete a_6993.xrz   ->  0xa09 / 0xa11
C -> S : 0x24/0x02 list      ->  CSV without those rows
TCP FIN
```

Compared to the list and download flows, two steps are notably
absent:

- no `0x10/0x01` bootstrap, no 68-byte time sync. Hello alone is
  enough for delete on this firmware.
- no `0x51/0x02` list-prep before the post-delete `0x24/0x02`.
  Delete appears to invalidate the device-side `dev.ria` cache
  internally, so a plain list call returns the updated CSV.

### 10.5 Verification

The CSV returned by `0x24/0x02` shrinks by the deleted rows. In one
captured session, deleting a single file reduced the list CSV from
12620 to 12501 bytes, matching the row width of a single entry.
Clients can call `0x24/0x02` after a delete batch to confirm the
targeted files are gone.

### 10.6 Unverified edge cases

- behaviour when the path does not exist; likely `0xa1d` empty by
  analogy with other commands, but not captured
- delete on a locked memory partition (`lck_mem=1`)
- delete during an active recording session
- whether the device queues pipelined requests or rejects them
- whether other AiM models accept `0x06/0x04` with the same arg
  format

## 11. Device-Info Block From `0x10/0x01`

After stripping the outer 4-byte offset prefix, the device-info body contains a
sequence of nested inner blocks:

| Tag | Length | Format | Meaning |
| --- | ---: | --- | --- |
| `iMST` | 128 | binary | master ID / firmware / serial-related |
| `iHW ` | 27 | ASCII `k=v|` | wireless hardware info |
| `iUSR` | 124 | nested `USR ` + ASCII | user fields |
| `iPTH` | 1123 | ASCII path definitions | virtual filesystem layout |
| `iLCK` | 47 | ASCII `k=v|` | lock states |
| `iSST` | 223 | ASCII `k=v|` | system feature flags |
| `iLTS` | 81 | ASCII `k=v|` | last test time |
| `iPRL` | 1312 | CSV | parameter/options list |

### 11.1 `iPTH`: key block

`iPTH` is a CRLF-separated list of:

```text
key=fs1:path1,fs2:path2.N|
```

Observed meanings:

- `fs=0`: internal flash / settings resources
- `fs=1`: recording storage flash

Important entries:

```text
recorded=1:/mem,1:/mem.N|
logs=1:/logs,1:/logs.N|
lapinfo=1:/mem/linfo,1:/mem/linfo.N|
dwnsm=1:/mem/dwnsm,1:/mem/dwnsm.N|
tkk=0:/tkk,0:/tkk.N|
```

Conclusion: recorded session files live under `1:/mem/<name>`.

### 11.2 `iHW `

Observed example:

```text
WiFi=WF121|Reg=eu|Rev=01|
```

### 11.3 `iPRL`

`iPRL` exposes Wi-Fi parameters such as:

- `wf_ssid`
- `wf_pwd`
- `wf_ip`
- `wf_nm`
- `wf_gw`

Treat this as sensitive output because passwords may appear in plaintext on
some devices.

Observed examples:

```text
WiFi,wf_ssid,SSID,s,AiM-SOLO2DL-504986,
WiFi,wf_pwd,Pwd,s,,
WiFi,wf_chan,Channel,i,1,-1,1,11,
WiFi,wf_mode,Mode,e,ap,ap,ap,in,
WiFi,wf_ip,IP,e,10.0.0.1,10.0.0.1,10.0.0.1,11.0.0.1,12.0.0.1,
WiFi,wf_nm,Net Mask,s,255.255.255.240,
WiFi,wf_gw,Gateway,s,0.0.0.0,
WiFi,wf_dhcp,DHCP Server,b,1,1,
```

`wf_ip` in the capture listed `10/11/12.0.0.1`, while real-device testing also
confirmed `14.0.0.1` as a valid AP address.

### 11.4 `iMST`

Partial interpretation of the first bytes:

```text
0   3   "idn"
3   1   0x01
4   2   0x0038 = 56
6   2   0x021e
8   2   0x01ff
12  6   MAC-related bytes
```

The byte sequence matches the tail of some discovery replies, but full field
decoding still needs more cross-device captures.

### 11.5 `iLCK`

Observed block:

```text
lck_fw=0|
lck_cfg=0|
lck_mem=0|
lck_trk=0|
```

All observed values were unlocked (`0`).

### 11.6 `iSST`

Observed values include:

```text
dbg_sim=0|
dbg_srec=0|
sys_mem=0|
sys_nnw=0|
sys_cer=0|
sys_nfc=1|
sys_atp=1|
sys_tkk=1|
sys_smv=1|
sys_sgs=1|
sys_tel=0|
sys_KLine=262400|
sys_RS232=262400|
sys_CAN=262400|
sys_lmsu_ok=1|
sys_predreflap=1|
```

These appear to be system feature flags and interface clock/baud settings.

### 11.7 `iLTS`

Observed block:

```text
lt_nm=|
lt_y=2026|
lt_mo=4|
lt_d=21|
lt_h=18|
lt_m=24|
lt_s=23|
lt_sz=0|
```

This looks like "last test" metadata.

### 11.8 `iUSR` / `USR `

`iUSR` is a normal inner frame, but the nested `USR ` block uses the ASCII
length/checksum variant:

```text
<h iUSR ... "a>">
  <h USR "0000000092" "a>">
    device=|
    pilota=|
    veicolo=|
    campionato=|
    venue_type=|
    desired_racem=|
    vehicle_type=|
  <USR 008273>
<iUSR ...>
```

Observed values were all empty.

## 12. CLI Implementation Guide

### 12.1 Minimal flow

Per TCP session:

1. Start UDP keep-alive on `:36002`
2. Discover device IP from the UDP reply source address
3. Connect to `<device_ip>:2000`
4. Wait about `0.4s`
5. Exchange the 8-byte hello
6. Run the mandatory bootstrap:
   - send `0x10/0x01`
   - when `0xa01` arrives, immediately send the 68-byte time sync
   - skip any `0xa09` and short 4-byte delimiter frames
   - wait for `0xa11 size=N`
   - ACK with offset `0`
   - optionally drain the device-info block
7. For list:
   - send `0x51/0x02` twice with `0xffffffff`
   - probe `0:/tkk/dev.ria`
   - if it returns data, use it
   - if it returns `0xa1d`, send `0x24/0x02`
8. For download:
   - send `0x02/0x04` with `1:/mem/<name>`
   - ignore intermediate `0xa01` and `0xa09`
   - on `0xa11 size=N`, start offset-ACK pull loop
   - append data until `N` bytes have been received
9. Save received bytes as `.xrz` / `.hrz` exactly as returned

Always send `status=0x00000001` in every 64-byte client request.

### 12.2 State-machine tips

- `0xa01`: intermediate ack
- `0xa09`: pending; continue waiting
- `0xa11`: ready; `size` becomes valid
- `0xa1d`: empty / no result; switch to the alternate path rather than retrying

### 12.3 Frame parser pseudocode

```python
def recv_frame(sock_buf: bytearray, sock) -> tuple[bytes, bytes]:
    while True:
        idx = sock_buf.find(b"<h")
        if idx == -1:
            del sock_buf[:]
        elif idx > 0:
            del sock_buf[:idx]
        if len(sock_buf) < 12:
            sock_buf += sock.recv(65536)
            continue
        tag = bytes(sock_buf[2:6])
        plen = int.from_bytes(sock_buf[6:10], "little")
        total = 12 + plen + 8
        if len(sock_buf) < total:
            sock_buf += sock.recv(65536)
            continue
        payload = bytes(sock_buf[12:12 + plen])
        assert sock_buf[10:12] == b"\x00>"
        assert sock_buf[12 + plen:13 + plen] == b"<"
        assert sock_buf[13 + plen:17 + plen] == tag
        chk = int.from_bytes(sock_buf[17 + plen:19 + plen], "little")
        assert sock_buf[19 + plen:20 + plen] == b">"
        assert chk == sum(payload) & 0xffff
        del sock_buf[:total]
        return tag, payload
```

Key parser rules:

- search with `find(b"<h")` and realign
- do not assume the frame starts at buffer offset 0
- detect EOF when `recv()` returns `b""`
- in production code, prefer explicit error handling over `assert`

### 12.4 Frame builder

```python
def wrap_frame(tag: bytes, payload: bytes) -> bytes:
    assert len(tag) == 4
    hdr = b"<h" + tag + len(payload).to_bytes(4, "little") + b"\x00>"
    chk = (sum(payload) & 0xffff).to_bytes(2, "little")
    return hdr + payload + b"<" + tag + chk + b">"
```

### 12.5 64-byte command builder

```python
import struct

STATUS_REQUEST = 0x00000001

def make_cmd64(cmd: int, sub: int, *, path: str = "", arg_tail: bytes = b"",
               status: int = STATUS_REQUEST, size: int = 0) -> bytes:
    hdr = bytearray(64)
    struct.pack_into("<HH", hdr, 8, cmd, sub)
    struct.pack_into("<I", hdr, 16, size)
    struct.pack_into("<I", hdr, 24, status)
    if path:
        b = path.encode("ascii") + b"\x00"
        assert len(b) <= 32
        hdr[32:32 + len(b)] = b
    elif arg_tail:
        assert len(arg_tail) <= 32
        hdr[32:32 + len(arg_tail)] = arg_tail
    return bytes(hdr)

init_frame = make_cmd64(0x10, 0x01, arg_tail=b"\x01", size=0x40)
```

## 13. Validation Status

### 13.1 Confirmed on real devices

- UDP discovery payload layout is firmware-dependent; only source port `36002`
  is a safe filter
- AiM AP IP is not fixed; use the reply source IP
- about `0.4s` settle after TCP connect and before hello matters
- stable sessions matched the vendor app only when UDP `aim-ka` continued while
  TCP was active
- request `status=0x1` is required for some firmware
- bootstrap init plus time sync is mandatory for `0x10/0x01`-class flows
  (device info, list, download)
- bootstrap is NOT required for delete-only sessions; vendor app went
  hello -> `0x06/0x04` directly (verified 2026-04-24, see §10)
- time sync must be sent immediately after the first `0xa01`
- 4-byte `STCP payload=0` delimiters may appear between status frames
- `0x10/0x01 size=0x40` matched the capture, but some units also accepted `0`
- the two-step list path (`dev.ria` empty -> `0x24/0x02`) was reproduced on
  at least two firmware variants
- `0x06/0x04` file delete reproduced on the vendor app: response is
  `0xa09` then `0xa11 size=0`, both echoing the requested path in arg.
  No body stream and no `0x51/0x02` prep needed before the post-delete
  list (verified 2026-04-24)

### 13.2 Still unverified

1. Full field decoding of the 128-byte `iMST` block
2. Meaning of late words in the 64-byte init request/response pair
3. Whether list prep truly requires two calls on every firmware
4. The cache-hit one-step `dev.ria` path
5. Arbitrary download resume from a nonzero offset
6. Whether any firmware requires an explicit close frame
7. Whether other AiM devices use the same `32704` byte chunk size
8. Exact fixed-point scale of CSV `maxvel`
9. Whether UDP keep-alive is independently mandatory, or simply correlated with
   the correct session pattern
10. Whether all other models require time sync as part of bootstrap
11. Whether `0x06/0x04` accepts pipelined requests (next request before the
    previous `0xa11` arrives), or rejects them
12. Status returned for `0x06/0x04` against a non-existent path (likely
    `0xa1d` empty by analogy, but not captured)
13. Whether `0x06/0x04` works against locked memory (`lck_mem=1`) or during
    an active recording session

## 14. Evidence References

| Observation | Capture reference |
| --- | --- |
| UDP `"aim-ka"` requests | `connect.pcapng` frames 38, 162, 274, ... |
| UDP 236B replies | `connect.pcapng` frames 39, 163, 277, ... |
| TCP 8B hello | `download.pcapng` frames 21 and 23 |
| init `0x10/0x01` request | frame 24 |
| init `0xa01` reply | frame 27 |
| 68B time sync | frame 28 |
| 4B delimiter | frame 29 |
| init ready `0xa11 size=3225` | frames 31 and 33 |
| client ACK for device info | frame 34 |
| device-info body | frames 35 through 45 |
| `0x24/0x02` session list | around frame 332 |
| file download chunk stream | around frame 420 onward |
| `0x06/0x04` delete request, path-echo `0xa09` reply | `delete.pcapng` frames 78, 79+81 (reassembled) |
| `0x06/0x04` delete `0xa11 size=0` completion | `delete.pcapng` frames 83+85 (reassembled) |
| post-delete `0x24/0x02` list with shrunken CSV | `delete.pcapng` frames 89 through 130 |
| fresh TCP session: hello -> delete x2 -> list (no bootstrap) | `delete.pcapng` frames 183 through 254 |

Frame numbers are approximate references for manual re-checking with:

```bash
tshark -r download.pcapng -Y 'tcp.port==2000'
tshark -r delete.pcapng   -Y 'tcp.port==2000'
```
