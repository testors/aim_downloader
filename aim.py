#!/usr/bin/env python3
"""aim — CLI for AiM data logger over Wi-Fi.

Subcommands:
  discover                         UDP probe on :36002 to find loggers.
  list      [--host IP]            Show recorded sessions (from dev.ria / 0x24-02).
  download  NAME... [-o DIR]       Download one or more sessions by name.
  download  --all   [-o DIR]       Download every session in the list.
  delete    NAME...                Delete one or more sessions by name.
  delete    --all                  Delete every session in the list.
  info      [--host IP]            Dump device info block (cmd 0x10/01).

See docs/wifi_protocol.md for the protocol spec this implements.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional


# IMPORTANT: AiM AP IP is configurable on-device; do not hardcode 10.0.0.1.
KNOWN_AP_HOSTS = ("10.0.0.1", "11.0.0.1", "12.0.0.1", "14.0.0.1")
DISCOVERY_BROADCAST = "255.255.255.255"
DEFAULT_DISCOVERY_HOSTS = KNOWN_AP_HOSTS + (DISCOVERY_BROADCAST,)
AUTO_DISCOVERY_TIMEOUT = 2.0
TCP_PORT = 2000
UDP_PORT = 36002
DISCOVERY_PROBE = b"aim-ka"

HDR_SIZE = 64
CMD_OFFSET = 8
SIZE_OFFSET = 16
STATUS_OFFSET = 24
ARG_OFFSET = 32

STATUS_REQUEST = 0x00000001
STATUS_RECEIVED = 0x00000a01
STATUS_PENDING = 0x00000a09
STATUS_READY = 0x00000a11
STATUS_EMPTY = 0x00000a1d

CHUNK_DATA_MAX = 32704
OPEN_RETRIES = 3
OPEN_RETRY_DELAY = 1.0
CLOSE_DRAIN_TIMEOUT = 1.0
CLOSE_COOLDOWN = 0.3
# Match the vendor app's steady "aim-ka" rhythm during active TCP sessions.
KEEPALIVE_INTERVAL = 0.8
KEEPALIVE_PRIME_DELAY = 0.1
# IMPORTANT: some loggers return only a broken 20B pseudo-frame if hello is
# sent immediately after TCP connect. A short post-connect settle delay makes
# the bootstrap reliable and matches the vendor capture timing.
CONNECT_SETTLE_DELAY = 0.4

CMD_DEVINFO = (0x10, 0x01)
CMD_SYNC_PING = (0x06, 0x01)
CMD_FILE_DELETE = (0x06, 0x04)
CMD_FILE_READ = (0x02, 0x04)
CMD_LIST = (0x24, 0x02)
CMD_LIST_PREP = (0x51, 0x02)
DEVINFO_REQ_SIZE = HDR_SIZE

RECORDED_DIR = "1:/mem"
LIST_CACHE_PATH = "0:/tkk/dev.ria"


# ---------------------------------------------------------------------------
# frame codec
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """Application-level framing/protocol error."""


def _abortive_close(sock: socket.socket) -> None:
    """Close with TCP RST to clear stuck logger-side sessions promptly."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def wrap_frame(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise ValueError("tag must be 4 bytes")
    hdr = b"<h" + tag + len(payload).to_bytes(4, "little") + b"\x00>"
    chk = (sum(payload) & 0xFFFF).to_bytes(2, "little")
    return hdr + payload + b"<" + tag + chk + b">"


class FrameReader:
    """Reads STCP/STNC frames from a TCP stream, handling segment/frame mismatch."""

    def __init__(self, sock: socket.socket, on_recv=None):
        self.sock = sock
        self.buf = bytearray()
        # optional callback(chunk: bytes) invoked for every raw recv()
        self.on_recv = on_recv

    def _recv_more(self, n: int = 65536) -> None:
        chunk = self.sock.recv(n)
        if not chunk:
            raise ConnectionError("connection closed by peer")
        if self.on_recv is not None:
            self.on_recv(chunk)
        self.buf.extend(chunk)

    def _need(self, size: int) -> None:
        while len(self.buf) < size:
            try:
                self._recv_more()
            except socket.timeout:
                if self.buf:
                    sys.stderr.write(
                        f"  [aim] timed out with {len(self.buf)}B partial data "
                        f"in buffer: {bytes(self.buf).hex()}\n"
                    )
                raise

    def _seek_frame_start(self) -> None:
        """Discard leading garbage until buffer starts with '<h'.

        TCP segment boundaries do not match protocol frame boundaries. If parsing
        ever gets out of sync, search forward for the next frame marker instead
        of failing permanently. Preserve a trailing lone '<' so a split '<h'
        marker can still be completed by the next recv().
        """
        while True:
            idx = self.buf.find(b"<h")
            if idx == 0:
                return
            if idx > 0:
                del self.buf[:idx]
                return
            if self.buf[-1:] == b"<":
                del self.buf[:-1]
            else:
                del self.buf[:]
            self._recv_more()

    def read(self) -> tuple[bytes, bytes]:
        while True:
            self._need(12)
            self._seek_frame_start()
            self._need(12)
            tag = bytes(self.buf[2:6])
            plen = int.from_bytes(self.buf[6:10], "little")
            if self.buf[10:12] != b"\x00>":
                del self.buf[:1]
                continue

            total = 12 + plen + 8  # '<' + tag(4) + chk(2) + '>'
            self._need(total)

            payload = bytes(self.buf[12:12 + plen])
            off = 12 + plen
            if self.buf[off:off + 1] != b"<" or self.buf[off + 1:off + 5] != tag:
                del self.buf[:1]
                continue
            chk = int.from_bytes(self.buf[off + 5:off + 7], "little")
            if self.buf[off + 7:off + 8] != b">":
                del self.buf[:1]
                continue
            expected = sum(payload) & 0xFFFF
            if chk != expected:
                raise ProtocolError(f"checksum mismatch: got {chk:#06x}, want {expected:#06x}")

            del self.buf[:total]
            return tag, payload


# ---------------------------------------------------------------------------
# 64B command header
# ---------------------------------------------------------------------------

def make_cmd(cmd: int, sub: int, *, path: str = "", arg_tail: bytes = b"",
             status: int = STATUS_REQUEST, size: int = 0) -> bytes:
    hdr = bytearray(HDR_SIZE)
    struct.pack_into("<HH", hdr, CMD_OFFSET, cmd, sub)
    struct.pack_into("<I", hdr, SIZE_OFFSET, size)
    struct.pack_into("<I", hdr, STATUS_OFFSET, status)
    if path:
        encoded = path.encode("ascii") + b"\x00"
        if len(encoded) > 32:
            raise ValueError(f"path too long: {path!r}")
        hdr[ARG_OFFSET:ARG_OFFSET + len(encoded)] = encoded
    elif arg_tail:
        if len(arg_tail) > 32:
            raise ValueError("arg_tail too long")
        hdr[ARG_OFFSET:ARG_OFFSET + len(arg_tail)] = arg_tail
    return bytes(hdr)


def parse_status(payload: bytes) -> tuple[int, int, int, int]:
    """Returns (cmd, sub, size, status) from a 64B command header response."""
    cmd, sub = struct.unpack_from("<HH", payload, CMD_OFFSET)
    size = struct.unpack_from("<I", payload, SIZE_OFFSET)[0]
    status = struct.unpack_from("<I", payload, STATUS_OFFSET)[0]
    return cmd, sub, size, status


def _make_timesync_payload(now: Optional[time.struct_time] = None) -> bytes:
    """68B STCP payload per spec §9: UTC block then local block.

    Layout (u32 LE unless noted):
      0..11   zero padding
      12      UTC year
      16      UTC month (1..12)
      20      UTC day   (1..31)
      24      UTC hour  (0..23)
      28      UTC minute
      32      0 (seconds reserved)
      36..43  zero padding
      44      local year
      48      local month
      52      local day
      56      local hour
      60      local minute
      64      0
    """
    epoch = time.time() if now is None else time.mktime(now)
    utc = time.gmtime(epoch)
    loc = time.localtime(epoch)
    pl = bytearray(68)
    def put(off: int, val: int) -> None:
        struct.pack_into("<I", pl, off, val)
    put(12, utc.tm_year)
    put(16, utc.tm_mon)
    put(20, utc.tm_mday)
    put(24, utc.tm_hour)
    put(28, utc.tm_min)
    put(44, loc.tm_year)
    put(48, loc.tm_mon)
    put(52, loc.tm_mday)
    put(56, loc.tm_hour)
    put(60, loc.tm_min)
    return bytes(pl)


# ---------------------------------------------------------------------------
# UDP discovery
# ---------------------------------------------------------------------------


class _UdpKeepalive:
    """Periodic UDP `aim-ka` sender for the lifetime of a TCP session.

    This is not just discovery traffic: the vendor app keeps it flowing while
    TCP is active, and stable real-device sessions matched that behavior.
    """

    def __init__(self, host: str, trace) -> None:
        self.host = host
        self._trace = trace
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", UDP_PORT))
        except OSError as e:
            self._trace(f"udp keepalive bind :{UDP_PORT} failed ({e}); using ephemeral port")
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aim-keepalive", daemon=True)
        self._thread.start()
        self._trace(f"udp keepalive started -> {self.host}:{UDP_PORT}")

    def stop(self) -> None:
        thread = self._thread
        sock = self._sock
        self._thread = None
        self._sock = None
        if thread is None:
            return
        self._stop.set()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread.join(timeout=1.0)
        self._stop.clear()
        self._trace("udp keepalive stopped")

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                self._sock.sendto(DISCOVERY_PROBE, (self.host, UDP_PORT))
            except OSError as e:
                if not self._stop.is_set():
                    self._trace(f"udp keepalive send failed: {e}")
            if self._stop.wait(KEEPALIVE_INTERVAL):
                break


@dataclass
class DiscoveredDevice:
    addr: str               # IP the datagram was received from (authoritative)
    reply_len: int          # raw UDP payload length
    declared_len: Optional[int]   # first 4B if it happens to encode length, else None
    version: Optional[int]        # bytes 4..8 if present, else None
    raw: bytes

    def short(self) -> str:
        parts = [self.addr]
        extras = []
        if self.reply_len:
            extras.append(f"{self.reply_len}B reply")
        if self.version is not None:
            extras.append(f"version={self.version}")
        if extras:
            return f"{self.addr}  ({', '.join(extras)})"
        return parts[0]


@dataclass
class FileReadResult:
    data: bytes
    ready_size: int


def parse_discovery(data: bytes, addr: str) -> DiscoveredDevice:
    """Best-effort parse. Never rejects — firmware byte layouts vary by model.

    Only these assumptions survive across devices:
      - the datagram came from UDP :36002 on the device (caller checks)
      - its source IP is the device IP (the `addr` argument)
    Everything else (length prefix, version, `idn` marker, ...) is advisory.
    """
    declared = None
    version = None
    if len(data) >= 4:
        d = int.from_bytes(data[0:4], "little")
        if d == len(data):
            declared = d
    if len(data) >= 8:
        version = int.from_bytes(data[4:8], "little")
    return DiscoveredDevice(
        addr=addr,
        reply_len=len(data),
        declared_len=declared,
        version=version,
        raw=data,
    )


def discover(timeout: float = 2.0,
             hosts: Optional[Iterable[str]] = None,
             verbose: bool = False) -> list[DiscoveredDevice]:
    """Broadcast + unicast `aim-ka` probe, collect replies from UDP :36002.

    Filter is intentionally minimal: any UDP reply whose *source port* is 36002
    and whose payload is not our own probe echo is treated as an AiM device.
    Payload layout is firmware-specific — do not filter on it.
    """
    hosts = list(dict.fromkeys(hosts if hosts else DEFAULT_DISCOVERY_HOSTS))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Device replies from src port 36002. Some firmwares deliver to the sender's
    # ephemeral port; others unicast back to :36002 regardless. Binding to 36002
    # covers both cases (matches vendor-app behaviour).
    try:
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as e:
        if verbose:
            sys.stderr.write(f"warning: bind to :{UDP_PORT} failed ({e}); using ephemeral port\n")
    sock.settimeout(0.4)

    found: dict[str, DiscoveredDevice] = {}
    deadline = time.monotonic() + timeout
    next_probe = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_probe:
                for h in hosts:
                    try:
                        sock.sendto(DISCOVERY_PROBE, (h, UDP_PORT))
                    except OSError as e:
                        if verbose:
                            sys.stderr.write(f"send to {h}: {e}\n")
                next_probe = now + 0.8
            try:
                data, (addr, src_port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if verbose:
                sys.stderr.write(f"rx {len(data)}B from {addr}:{src_port}: "
                                 f"{data[:32].hex()}\n")
            # Only hard filter: reply must come from the AiM UDP port,
            # and must not be our own probe echoed back on broadcast.
            if src_port != UDP_PORT:
                continue
            if data == DISCOVERY_PROBE:
                continue
            found[addr] = parse_discovery(data, addr)
    finally:
        sock.close()
    return list(found.values())


def auto_discover_host(timeout: float = AUTO_DISCOVERY_TIMEOUT,
                       verbose: bool = False) -> str:
    """Resolve the active logger IP without assuming 10.0.0.1 is fixed."""
    devices = discover(timeout=timeout, hosts=DEFAULT_DISCOVERY_HOSTS, verbose=verbose)
    addrs = sorted({d.addr for d in devices})
    if not addrs:
        known = ", ".join(KNOWN_AP_HOSTS)
        raise ConnectionError(
            "no AiM device found via auto-discovery "
            f"(known AP IPs: {known}); pass --host explicitly or run `discover`"
        )
    if len(addrs) > 1:
        raise ConnectionError(
            "multiple AiM devices found via auto-discovery: "
            + ", ".join(addrs)
            + "; pass --host explicitly"
        )
    return addrs[0]


# ---------------------------------------------------------------------------
# TCP session
# ---------------------------------------------------------------------------

_STATUS_NAMES = {
    STATUS_REQUEST: "req",
    STATUS_RECEIVED: "recv",
    STATUS_PENDING: "pending",
    STATUS_READY: "ready",
    STATUS_EMPTY: "empty",
}


def _summarize_frame(tag: bytes, payload: bytes) -> str:
    n = len(payload)
    tag_txt = tag.decode("ascii", errors="replace")
    if tag == b"STCP" and n == 68 and payload[:12] == b"\x00" * 12 and payload[36:44] == b"\x00" * 8:
        return "STCP 68B  time-sync"
    if n >= HDR_SIZE and tag in (b"STCP", b"STNC") and payload[:8] == b"\x00" * 8:
        cmd, sub, size, status = parse_status(payload)
        st = _STATUS_NAMES.get(status, f"{status:#010x}")
        s = f"{tag_txt} {n}B  cmd=0x{cmd:02x}/0x{sub:02x} size={size} status={st}"
        arg = payload[ARG_OFFSET:].rstrip(b"\x00")
        if arg:
            try:
                s += f" arg={arg.decode('ascii')!r}"
            except UnicodeDecodeError:
                s += f" arg=hex:{payload[ARG_OFFSET:ARG_OFFSET+8].hex()}"
        return s
    if n == 4:
        return f"{tag_txt} 4B  offset/ack={int.from_bytes(payload, 'little')}"
    if n == 8:
        return f"{tag_txt} 8B  hex={payload.hex()}"
    preview = payload[:16].hex()
    more = "..." if n > 16 else ""
    if n >= 4 and tag == b"STCP":
        off = int.from_bytes(payload[:4], "little")
        return f"{tag_txt} {n}B  data_offset={off} body[{min(12, n-4)}]={payload[4:16].hex()}{more}"
    return f"{tag_txt} {n}B  hex={preview}{more}"


class AimSession:
    """One TCP connection to the logger. Designed for short-lived per-task use."""

    def __init__(self, host: Optional[str] = None, port: int = TCP_PORT,
                 timeout: float = 15.0, verbose: bool = False,
                 bootstrap: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.bootstrap = bootstrap
        self.sock: Optional[socket.socket] = None
        self.reader: Optional[FrameReader] = None
        self.device_info: bytes = b""
        self.keepalive: Optional[_UdpKeepalive] = None

    def __enter__(self) -> "AimSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> None:
        last_exc: Optional[BaseException] = None
        if self.host is None:
            self.host = auto_discover_host(
                timeout=min(self.timeout, AUTO_DISCOVERY_TIMEOUT),
                verbose=self.verbose,
            )
            self._trace(f"auto-discovered host {self.host}")
        plans = ("none",) if not self.bootstrap else (
            "direct",
            "ping_then_init",
            "vendor_full",
        )
        for attempt in range(1, OPEN_RETRIES + 1):
            try:
                plan = plans[min(attempt - 1, len(plans) - 1)]
                self._trace(
                    f"connect tcp {self.host}:{self.port} "
                    f"(timeout {self.timeout}s, attempt {attempt}/{OPEN_RETRIES}, "
                    f"bootstrap={plan})"
                )
                # IMPORTANT: keep the UDP side alive before and during TCP.
                self._start_keepalive()
                if KEEPALIVE_PRIME_DELAY > 0:
                    time.sleep(KEEPALIVE_PRIME_DELAY)
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self.sock.settimeout(self.timeout)
                # TCP_NODELAY: avoid Nagle coalescing tiny frames (hello, ACKs, etc).
                try:
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                if CONNECT_SETTLE_DELAY > 0:
                    # IMPORTANT: some units reject an immediate hello/init
                    # burst after connect and only emit a malformed 20B
                    # `<hSTCP len=8> ... <STCP 0000>` response.
                    self._trace(f"post-connect settle {CONNECT_SETTLE_DELAY:.1f}s")
                    time.sleep(CONNECT_SETTLE_DELAY)
                on_recv = None
                if self.verbose:
                    def on_recv(chunk: bytes) -> None:
                        self._trace(f"raw recv {len(chunk)}B: {chunk[:64].hex()}"
                                    f"{'...' if len(chunk) > 64 else ''}")
                self.reader = FrameReader(self.sock, on_recv=on_recv)
                self._hello()
                if self.bootstrap:
                    # Some firmwares require a full session init (device-info +
                    # time-sync) before they accept application commands.
                    self._bootstrap(plan)
                return
            except (ProtocolError, ConnectionError, socket.timeout, OSError) as e:
                last_exc = e
                self._trace(f"bootstrap failed: {e}")
                self.close(abort=True)
                if attempt >= OPEN_RETRIES:
                    raise
                time.sleep(OPEN_RETRY_DELAY)
        if last_exc is not None:
            raise last_exc

    def close(self, *, abort: bool = False) -> None:
        keepalive = self.keepalive
        self.keepalive = None
        if self.sock is not None:
            sock = self.sock
            self.sock = None
            self.reader = None
            peer_closed = False
            if not abort:
                try:
                    # Graceful TCP teardown first: send FIN, then give the
                    # logger a brief chance to respond with EOF/FIN.
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    abort = True
                else:
                    prev_timeout = sock.gettimeout()
                    try:
                        sock.settimeout(CLOSE_DRAIN_TIMEOUT)
                        while True:
                            chunk = sock.recv(65536)
                            if not chunk:
                                peer_closed = True
                                break
                            if self.verbose:
                                self._trace(
                                    f"close drain {len(chunk)}B: {chunk[:64].hex()}"
                                    f"{'...' if len(chunk) > 64 else ''}"
                                )
                    except (socket.timeout, OSError):
                        pass
                    finally:
                        try:
                            sock.settimeout(prev_timeout)
                        except OSError:
                            pass
                    if not peer_closed:
                        abort = True
            if abort:
                _abortive_close(sock)
                self._trace("tcp closed (rst)")
            else:
                try:
                    sock.close()
                except OSError:
                    pass
                self._trace("tcp closed")
            if keepalive is not None:
                keepalive.stop()
                keepalive = None
            if CLOSE_COOLDOWN > 0:
                time.sleep(CLOSE_COOLDOWN)
        if keepalive is not None:
            keepalive.stop()

    def reset(self, delay: float = 0.2) -> None:
        """Close and reopen the TCP session to recover from a bad protocol state."""
        self._trace("reset tcp session")
        self.close(abort=True)
        if delay > 0:
            time.sleep(delay)
        self.open()

    def _trace(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write(f"  [aim] {msg}\n")
            sys.stderr.flush()

    def _start_keepalive(self) -> None:
        if self.keepalive is None:
            self.keepalive = _UdpKeepalive(self.host, self._trace)
            self.keepalive.start()

    def _send(self, tag: bytes, payload: bytes) -> None:
        assert self.sock is not None
        if self.verbose:
            self._trace(f"tx {_summarize_frame(tag, payload)}")
        self.sock.sendall(wrap_frame(tag, payload))

    def _recv_frame(self) -> tuple[bytes, bytes]:
        assert self.reader is not None
        tag, pl = self.reader.read()
        if self.verbose:
            self._trace(f"rx {_summarize_frame(tag, pl)}")
        return tag, pl

    def _hello(self) -> None:
        # 8B STCP hello. Send only after the post-connect settle delay above;
        # some loggers are timing-sensitive here.
        self._send(b"STCP", bytes.fromhex("0000000006080000"))
        try:
            tag, pl = self._recv_frame()
        except socket.timeout:
            raise ConnectionError(
                "no hello reply from logger — the device is likely in a stuck "
                "state from a previous session. Power-cycle the logger and retry."
            )
        if tag != b"STCP" or len(pl) != 8 or pl[4:6] != b"\x06\x09":
            sys.stderr.write(f"warning: unexpected hello reply tag={tag!r} payload={pl.hex()}\n")

    def _init(self) -> None:
        """Device-info request + time-sync.

            C → init  (STNC cmd=0x10/01)
            S → 0xa01 RECEIVED  (64B ack-only; NOT yet ready)
            C → time-sync  (68B STCP)                    ← transitions server to READY
            S → empty 4B STCP                             ← ack of time-sync
            S → 0xa11 READY size=N
            C → ACK(0)
            S → N-byte data

        Important nuance: do not wait for READY before time-sync, but do follow
        the observed capture ordering and wait for the initial 0xa01 RECEIVED
        ack of the init request before sending the 68B time-sync.
        """
        size, status = self._run_init_handshake(
            *CMD_DEVINFO,
            arg_tail=b"\x01",
            size=DEVINFO_REQ_SIZE,
        )
        if status == STATUS_READY and size > 0:
            self.device_info = self._read_stream(size)

    def send_time_sync(self) -> None:
        """Public alias if caller wants to explicitly re-sync time mid-session."""
        self._send(b"STCP", _make_timesync_payload())

    def _sync_ping(self) -> None:
        """Lightweight state ping seen in vendor bootstrap before list activity."""
        self._send_stnc_cmd(*CMD_SYNC_PING, size=HDR_SIZE)
        self._wait_ready(expected_cmd=CMD_SYNC_PING)

    def _bootstrap(self, plan: str) -> None:
        if plan == "direct":
            self._init()
            return
        if plan == "ping_then_init":
            self._sync_ping()
            self._init()
            return
        if plan == "vendor_full":
            self._init()
            self._sync_ping()
            self._init()
            return
        raise ValueError(f"unknown bootstrap plan {plan!r}")

    def _send_stnc_cmd(self, cmd: int, sub: int, *, path: str = "",
                       arg_tail: bytes = b"", size: int = 0) -> None:
        self._send(b"STNC", make_cmd(cmd, sub, path=path, arg_tail=arg_tail, size=size))

    def _run_init_handshake(self, cmd: int, sub: int, *, arg_tail: bytes = b"",
                            size: int = 0) -> tuple[int, int]:
        expected = (cmd, sub)
        self._send_stnc_cmd(cmd, sub, arg_tail=arg_tail, size=size)
        size, status = self._wait_status(
            accept={STATUS_RECEIVED, STATUS_READY, STATUS_EMPTY},
            expected_cmd=expected,
        )
        if status == STATUS_RECEIVED:
            self._send(b"STCP", _make_timesync_payload())
            size, status = self._wait_ready(expected_cmd=expected)
        return size, status

    def _wait_status(self, *, accept: set[int],
                     expected_cmd: Optional[tuple[int, int]] = None) -> tuple[int, int]:
        """Wait for a status frame, skipping short delimiters/acks.

        Returns (size, status) once a status in `accept` is seen. `0xa01` and
        `0xa09` are treated as intermediate unless explicitly accepted.
        """
        exp_cmd = exp_sub = None
        if expected_cmd is not None:
            exp_cmd, exp_sub = expected_cmd
        while True:
            tag, pl = self._recv_frame()
            if tag != b"STCP":
                raise ProtocolError(f"unexpected non-STCP frame: tag={tag!r}")
            if len(pl) < HDR_SIZE:
                # short STCP (typically 4B payload=0) — device delimiter/ack
                continue
            cmd, sub, size, status = parse_status(pl)
            if expected_cmd is not None and (cmd, sub) != (exp_cmd, exp_sub):
                raise ProtocolError(
                    f"unexpected status for cmd=0x{cmd:02x}/0x{sub:02x}; "
                    f"want 0x{exp_cmd:02x}/0x{exp_sub:02x}"
                )
            if status in accept:
                return size, status
            if status in (STATUS_RECEIVED, STATUS_PENDING):
                continue
            raise ProtocolError(f"unexpected status {status:#010x}")

    def _wait_ready(self, *, expected_cmd: Optional[tuple[int, int]] = None) -> tuple[int, int]:
        """Consume intermediate frames (0xa01 received, 0xa09 pending, short
        delimiters) and return (size, status) when 0xa11/0xa1d arrives.

        Capture shows the server sometimes interleaves a 4B STCP with payload=0
        between status frames (acting as a delimiter or intermediate ack).
        Skip those rather than error out.
        """
        return self._wait_status(
            accept={STATUS_READY, STATUS_EMPTY},
            expected_cmd=expected_cmd,
        )

    def _read_stream(self, total: int,
                     progress: Optional[callable] = None) -> bytes:
        """Pull a data stream via offset ACKs until `total` bytes collected."""
        out = bytearray()
        while len(out) < total:
            # ACK = next offset we want (= current length)
            self._send(b"STCP", len(out).to_bytes(4, "little"))
            tag, pl = self._recv_frame()
            if tag != b"STCP" or len(pl) < 4:
                raise ProtocolError(f"bad data frame tag={tag!r} len={len(pl)}")
            offset = int.from_bytes(pl[:4], "little")
            data = pl[4:]
            if offset != len(out):
                raise ProtocolError(f"offset mismatch: got {offset} want {len(out)}")
            out.extend(data)
            if progress is not None:
                progress(len(out), total)
        return bytes(out)

    # ---- higher-level operations ----

    def read_file(self, path: str, *,
                  progress: Optional[callable] = None) -> bytes:
        """cmd=0x02/0x04 file read. Works for both regular files and dev.ria."""
        return self.read_file_result(path, progress=progress).data

    def read_file_result(self, path: str, *,
                         progress: Optional[callable] = None) -> FileReadResult:
        """Read a file and keep the device's READY size for diagnostics."""
        self._send_stnc_cmd(*CMD_FILE_READ, path=path)
        size, status = self._wait_ready(expected_cmd=CMD_FILE_READ)
        if status == STATUS_EMPTY:
            return FileReadResult(b"", size)
        if size == 0:
            return FileReadResult(b"", size)
        return FileReadResult(self._read_stream(size, progress=progress), size)

    def fetch_list_csv(self) -> str:
        """Reproduce the vendor-app list flow: prep x2 → dev.ria probe → 0x24/02."""
        # 1) list prep x2, arg tail = 0xFFFFFFFF
        prep_arg = b"\xff\xff\xff\xff"
        for _ in range(2):
            self._send_stnc_cmd(*CMD_LIST_PREP, arg_tail=prep_arg)
            size, status = self._wait_ready(expected_cmd=CMD_LIST_PREP)
            if status == STATUS_EMPTY:
                # unexpected but not fatal
                pass

        # 2) cache probe: dev.ria. If cached, CSV comes back here.
        cached = self.read_file(LIST_CACHE_PATH)
        if cached:
            return cached.decode("ascii", errors="replace")

        # 3) fresh fetch: cmd=0x24/02
        return self.fetch_plain_list_csv()

    def fetch_plain_list_csv(self) -> str:
        """Fetch the current CSV directly via 0x24/02 with no prep/cache probe."""
        self._send_stnc_cmd(*CMD_LIST)
        size, status = self._wait_ready(expected_cmd=CMD_LIST)
        if status == STATUS_EMPTY or size == 0:
            return ""
        blob = self._read_stream(size)
        return blob.decode("ascii", errors="replace")

    def delete_file(self, path: str) -> int:
        """Delete one file via 0x06/0x04 and return the terminal status code."""
        self._send_stnc_cmd(*CMD_FILE_DELETE, path=path)
        expected_path = path.encode("ascii")
        while True:
            tag, pl = self._recv_frame()
            if tag != b"STCP":
                raise ProtocolError(f"unexpected non-STCP frame: tag={tag!r}")
            if len(pl) < HDR_SIZE:
                # Delete should not stream a body, but preserve the usual
                # tolerance for short delimiter/ack frames.
                continue
            cmd, sub, size, status = parse_status(pl)
            if (cmd, sub) != CMD_FILE_DELETE:
                raise ProtocolError(
                    f"unexpected status for cmd=0x{cmd:02x}/0x{sub:02x}; "
                    f"want 0x{CMD_FILE_DELETE[0]:02x}/0x{CMD_FILE_DELETE[1]:02x}"
                )
            echoed = pl[ARG_OFFSET:].split(b"\x00", 1)[0]
            if echoed != expected_path and not (status == STATUS_EMPTY and echoed == b""):
                raise ProtocolError(
                    f"delete path echo mismatch: got {echoed!r} want {expected_path!r}"
                )
            if status in (STATUS_RECEIVED, STATUS_PENDING):
                continue
            if status in (STATUS_READY, STATUS_EMPTY):
                if size != 0:
                    raise ProtocolError(f"unexpected delete completion size {size}")
                return status
            raise ProtocolError(f"unexpected delete status {status:#010x}")

    def fetch_device_info(self) -> bytes:
        """Return the device-info block captured during session init.

        Already fetched during open(); no extra round-trip unless the session
        had no init (e.g. a future no_init flag) — in which case we request now.
        """
        if self.device_info:
            return self.device_info
        self._send_stnc_cmd(*CMD_DEVINFO, arg_tail=b"\x01", size=DEVINFO_REQ_SIZE)
        size, status = self._wait_ready(expected_cmd=CMD_DEVINFO)
        if status == STATUS_EMPTY or size == 0:
            return b""
        self.device_info = self._read_stream(size)
        return self.device_info


# ---------------------------------------------------------------------------
# session list parsing
# ---------------------------------------------------------------------------

@dataclass
class Session:
    name: str
    size: int
    date: str
    hour: str
    nlap: str
    nbest: str
    best: str
    pilota: str
    track_name: str
    raw: dict[str, str]

    @property
    def remote_path(self) -> str:
        return f"{RECORDED_DIR}/{self.name}"


def parse_session_list(csv_text: str) -> list[Session]:
    """Parse the CSV returned by fetch_list_csv()."""
    if not csv_text.strip():
        return []
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    header = [h.strip() for h in header]
    out: list[Session] = []
    for row in reader:
        if not row or not row[0]:
            continue
        # pad row to header length
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        d = dict(zip(header, row))
        try:
            size = int(d.get("size", "0") or "0")
        except ValueError:
            size = 0
        out.append(Session(
            name=d.get("name", ""),
            size=size,
            date=d.get("date", ""),
            hour=d.get("hour", ""),
            nlap=d.get("nlap", ""),
            nbest=d.get("nbest", ""),
            best=d.get("best", ""),
            pilota=d.get("pilota", ""),
            track_name=d.get("track_name", ""),
            raw=d,
        ))
    return out


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _find_session_by_name(sessions: list[Session], name: str) -> Optional[Session]:
    for s in sessions:
        if s.name == name:
            return s
    return None


def _session_sort_key(s: Session) -> Optional[tuple[int, int, int, int, int, int, str]]:
    try:
        day_s, month_s, year_s = s.date.split("/")
        hour_parts = s.hour.split(":")
        if len(hour_parts) not in (2, 3):
            return None
        hour_s, minute_s = hour_parts[:2]
        second_s = hour_parts[2] if len(hour_parts) == 3 else "0"
        return (
            int(year_s),
            int(month_s),
            int(day_s),
            int(hour_s),
            int(minute_s),
            int(second_s),
            s.name,
        )
    except ValueError:
        return None


def _find_latest_session(sessions: list[Session]) -> Optional[Session]:
    latest: Optional[tuple[tuple[int, int, int, int, int, int, str], Session]] = None
    for s in sessions:
        key = _session_sort_key(s)
        if key is None:
            continue
        if latest is None or key > latest[0]:
            latest = (key, s)
    return None if latest is None else latest[1]


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _safe_output_name(name: str) -> str:
    """Reject logger-provided names that would escape the output directory."""
    if not name or name in (".", ".."):
        raise ValueError("invalid empty or dot-only session name")
    if "/" in name or "\\" in name or name != os.path.basename(name):
        raise ValueError(f"unsafe session name {name!r}")
    return name


def render_session_table(sessions: list[Session]) -> str:
    if not sessions:
        return "(no sessions on device)"
    cols = [
        ("#", lambda i, s: str(i)),
        ("name", lambda i, s: s.name),
        ("size", lambda i, s: _fmt_size(s.size)),
        ("date", lambda i, s: s.date),
        ("hour", lambda i, s: s.hour),
        ("laps", lambda i, s: s.nlap),
        ("best(ms)", lambda i, s: s.best),
        ("track", lambda i, s: s.track_name),
    ]
    rows = [[name for name, _ in cols]]
    for i, s in enumerate(sessions, 1):
        rows.append([fn(i, s) for _, fn in cols])
    widths = [max(len(r[c]) for r in rows) for c in range(len(cols))]
    lines = []
    for idx, r in enumerate(rows):
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(r, widths)))
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _confirm(question: str, *, default: bool = False) -> bool:
    # Prompts are only safe in an interactive terminal; in batch mode, fall
    # back to the caller-provided default instead of blocking on stdin.
    if not sys.stdin.isatty():
        print(f"warn  {question} (stdin is not a TTY; treating as 'no')",
              file=sys.stderr)
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        sys.stderr.write(question + suffix)
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if answer == "":
            return default
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("please answer yes or no", file=sys.stderr)


def _validate_downloaded_session(name: str, data: bytes) -> None:
    from aim_telemetry import build_session, decode_session_bytes, looks_like_zlib

    # Reuse the telemetry parser as a structural integrity check rather than
    # inventing a second "is this log valid?" implementation in aim.py.
    raw = data
    if name.lower().endswith(".xrz") or looks_like_zlib(data):
        raw = decode_session_bytes(data, source=f"downloaded {name}", compressed=True)
    build_session(raw)


class _ProgressBar:
    def __init__(self, label: str, total: int, enabled: bool = True):
        self.label = label
        self.total = total
        self.enabled = enabled and sys.stderr.isatty()
        self._last = 0.0
        self._start = time.monotonic()

    def __call__(self, got: int, total: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if got < total and now - self._last < 0.1:
            return
        self._last = now
        pct = got / total * 100 if total else 100.0
        elapsed = now - self._start
        rate = got / elapsed if elapsed > 0 else 0
        bar_w = 30
        filled = int(bar_w * (got / total)) if total else bar_w
        bar = "#" * filled + "-" * (bar_w - filled)
        sys.stderr.write(
            f"\r{self.label} [{bar}] {pct:5.1f}%  "
            f"{_fmt_size(got)}/{_fmt_size(total)}  "
            f"{_fmt_size(int(rate))}/s"
        )
        sys.stderr.flush()
        if got >= total:
            sys.stderr.write("\n")
            sys.stderr.flush()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_discover(args: argparse.Namespace) -> int:
    hosts = [args.host] if args.host else None
    devices = discover(timeout=args.timeout, hosts=hosts, verbose=args.verbose)
    if not devices:
        print("no AiM device found (probed aim-ka to "
              f"{hosts or list(DEFAULT_DISCOVERY_HOSTS)} for {args.timeout}s)",
              file=sys.stderr)
        return 1
    for d in devices:
        print(d.short())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with AimSession(host=args.host, timeout=args.timeout, verbose=args.verbose) as sess:
        csv_text = sess.fetch_list_csv()
    sessions = parse_session_list(csv_text)

    if args.raw_csv:
        sys.stdout.write(csv_text)
        if not csv_text.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    if args.json:
        import json
        json.dump([s.raw for s in sessions], sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    print(render_session_table(sessions))
    return 0


def _resolve_targets(sessions: list[Session], names: list[str],
                     all_flag: bool) -> list[Session]:
    if all_flag:
        return sessions
    if not names:
        return []
    by_name = {s.name: s for s in sessions}
    # allow short names without extension, numeric index (#N), or exact name
    resolved: list[Session] = []
    for n in names:
        if n in by_name:
            resolved.append(by_name[n])
            continue
        # try adding .xrz / .hrz
        for ext in (".xrz", ".hrz"):
            if n + ext in by_name:
                resolved.append(by_name[n + ext])
                break
        else:
            # try by index (1-based)
            if n.isdigit():
                idx = int(n)
                if 1 <= idx <= len(sessions):
                    resolved.append(sessions[idx - 1])
                    continue
            print(f"error: no session matches {n!r}", file=sys.stderr)
            sys.exit(2)
    return resolved


def cmd_download(args: argparse.Namespace) -> int:
    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)

    # Use a single session for list + all downloads. Some firmwares stall if we
    # close and immediately reopen a second TCP connection — one session avoids
    # that class of failure entirely. (The vendor app uses two parallel sessions
    # but keeps the first open; single-session works equivalently for CLI use.)
    with AimSession(host=args.host, timeout=args.timeout, verbose=args.verbose) as sess:
        csv_text = sess.fetch_list_csv()
        sessions = parse_session_list(csv_text)

        if not sessions:
            print("no sessions on device", file=sys.stderr)
            return 1

        targets = _resolve_targets(sessions, args.names, args.all)
        if not targets:
            print("error: specify session name(s) or --all", file=sys.stderr)
            return 2

        active_target = _find_latest_session(sessions)
        total_ok = 0
        for s in targets:
            try:
                local_name = _safe_output_name(s.name)
            except ValueError as e:
                print(f"fail  {s.name}: {e}", file=sys.stderr)
                continue
            previous_size = s.size
            if active_target is not None and s.name == active_target.name:
                try:
                    # Only the latest session is expected to still be changing,
                    # so avoid refetching the list for every older target.
                    fresh = _find_session_by_name(parse_session_list(sess.fetch_list_csv()), s.name)
                except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as e:
                    print(f"fail  {s.name}: could not refresh list before download: {e}",
                          file=sys.stderr)
                    try:
                        sess.reset()
                    except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as reset_e:
                        print(f"error: could not recover TCP session after failure: {reset_e}",
                              file=sys.stderr)
                        return 1
                    continue
                if fresh is None:
                    print(f"fail  {s.name}: session no longer present in refreshed list",
                          file=sys.stderr)
                    continue
                s = fresh
            expected_size = s.size
            size_changed = expected_size != previous_size
            dst = os.path.join(out_dir, local_name)
            if os.path.exists(dst) and not args.force:
                st = os.stat(dst)
                if st.st_size == expected_size:
                    print(f"skip  {s.name}  (already exists, size matches)")
                    total_ok += 1
                    continue
                else:
                    print(f"warn  {s.name}  exists with different size "
                          f"({st.st_size} vs {expected_size}); use --force to overwrite",
                          file=sys.stderr)
                    continue
            if size_changed:
                print(f"warn  {s.name}: list size changed {previous_size} -> {expected_size}; "
                      "session appears to still be recording",
                      file=sys.stderr)
                if not _confirm(f"download {s.name} anyway?"):
                    print(f"skip  {s.name}  (user declined download while session is changing)",
                          file=sys.stderr)
                    continue

            progress = _ProgressBar(f"  {s.name}", expected_size, enabled=not args.quiet)
            try:
                read = sess.read_file_result(s.remote_path, progress=progress)
                data = read.data
            except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as e:
                print(f"fail  {s.name}: {e}", file=sys.stderr)
                try:
                    sess.reset()
                except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as reset_e:
                    print(f"error: could not recover TCP session after failure: {reset_e}",
                          file=sys.stderr)
                    return 1
                continue
            if len(data) != expected_size:
                try:
                    # If parsing still succeeds, the most likely explanation is
                    # that the logger appended/finalized the file mid-download.
                    _validate_downloaded_session(s.name, data)
                except (ImportError, OSError, ValueError, struct.error) as parse_e:
                    print(f"fail  {s.name}: size mismatch "
                          f"(got {len(data)}, list {expected_size}, ready {read.ready_size}); "
                          f"parse failed: {parse_e}",
                          file=sys.stderr)
                    continue
                print(f"warn  {s.name}: size mismatch "
                      f"(got {len(data)}, list {expected_size}, ready {read.ready_size}); "
                      "logger file changed during download",
                      file=sys.stderr)
                if not _confirm(f"save {s.name} anyway?"):
                    print(f"skip  {s.name}  (user declined saving log captured while session changed)",
                          file=sys.stderr)
                    continue
            if args.verbose:
                print(f"  [aim] download result {s.name}: "
                      f"got={len(data)} list={expected_size} ready={read.ready_size}",
                      file=sys.stderr)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            print(f"ok    {s.name}  →  {dst}  ({_fmt_size(len(data))})")
            total_ok += 1

        return 0 if total_ok == len(targets) else 1


def cmd_delete(args: argparse.Namespace) -> int:
    with AimSession(host=args.host, timeout=args.timeout, verbose=args.verbose) as sess:
        csv_text = sess.fetch_list_csv()
        sessions = parse_session_list(csv_text)
        host = sess.host

    if not sessions:
        print("no sessions on device", file=sys.stderr)
        return 1

    targets = _resolve_targets(sessions, args.names, args.all)
    if not targets:
        print("error: specify session name(s) or --all", file=sys.stderr)
        return 2

    if not args.yes:
        if len(targets) == 1:
            prompt = f"delete {targets[0].name} from device?"
        else:
            prompt = f"delete {len(targets)} sessions from device?"
        if not _confirm(prompt):
            print("cancelled", file=sys.stderr)
            return 0

    deleted_names: list[str] = []
    total_ok = 0
    with AimSession(host=host, timeout=args.timeout, verbose=args.verbose,
                    bootstrap=False) as sess:
        for s in targets:
            try:
                status = sess.delete_file(s.remote_path)
            except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as e:
                print(f"fail  {s.name}: {e}", file=sys.stderr)
                try:
                    sess.reset()
                except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as reset_e:
                    print(f"error: could not recover TCP session after failure: {reset_e}",
                          file=sys.stderr)
                    return 1
                continue
            if status == STATUS_EMPTY:
                print(f"fail  {s.name}: device reported file missing", file=sys.stderr)
                continue
            if args.verbose:
                print(f"  [aim] delete result {s.name}: status={status:#010x}",
                      file=sys.stderr)
            print(f"ok    {s.name}  deleted")
            deleted_names.append(s.name)
            total_ok += 1

        if deleted_names:
            try:
                remaining = parse_session_list(sess.fetch_plain_list_csv())
            except (ProtocolError, ConnectionError, socket.timeout, OSError, ValueError) as e:
                print(f"error: could not verify deleted sessions: {e}", file=sys.stderr)
                return 1
            remaining_names = {item.name for item in remaining}
            for name in deleted_names:
                if name in remaining_names:
                    print(f"fail  {name}: still present after delete verification",
                          file=sys.stderr)
                    total_ok -= 1

    return 0 if total_ok == len(targets) else 1


def cmd_info(args: argparse.Namespace) -> int:
    with AimSession(host=args.host, timeout=args.timeout, verbose=args.verbose) as sess:
        blob = sess.fetch_device_info()
    if not blob:
        print("no device info returned", file=sys.stderr)
        return 1

    # skip 4B offset prefix already stripped by _read_stream
    # blob is the raw 3225B inner block stream; dump inner tags + ASCII bodies
    i = 0
    n = len(blob)
    printed = 0
    while i + 12 <= n:
        if blob[i:i + 2] != b"<h":
            i += 1
            continue
        tag = blob[i + 2:i + 6]
        plen = int.from_bytes(blob[i + 6:i + 10], "little")
        term = blob[i + 10:i + 12]
        if term not in (b"a>", b"\x00>"):
            i += 1
            continue
        body_start = i + 12
        body_end = body_start + plen
        if body_end + 8 > n:
            break
        body = blob[body_start:body_end]
        tag_txt = tag.decode("ascii", errors="replace")
        print(f"--- {tag_txt}  ({plen}B) ---")
        # heuristic: ASCII if most bytes are printable
        printable = sum(1 for b in body if 32 <= b < 127 or b in (9, 10, 13))
        if plen > 0 and printable / plen > 0.8:
            sys.stdout.write(body.decode("ascii", errors="replace"))
            if not body.endswith(b"\n"):
                sys.stdout.write("\n")
        else:
            # hex dump first 128B
            preview = body[:128]
            print(preview.hex(" "))
            if len(body) > 128:
                print(f"... ({len(body) - 128} more bytes)")
        printed += 1
        i = body_end + 8  # skip trailer: '<' + tag(4) + chk(2) + '>'
    if printed == 0:
        print(f"(could not parse inner blocks; raw {len(blob)}B)")
        print(blob[:256].hex(" "))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aim",
        description="List, download, and delete driving records from an AiM data logger over Wi-Fi.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="UDP probe for AiM loggers on the network.")
    d.add_argument("--host", default=None,
                   help="Probe a single host/broadcast (default: 10/11/12/14.0.0.1 + 255.255.255.255)")
    d.add_argument("--timeout", type=float, default=2.0, help="Seconds to listen.")
    d.add_argument("-v", "--verbose", action="store_true",
                   help="Print every received UDP datagram.")
    d.set_defaults(func=cmd_discover)

    ls = sub.add_parser("list", help="List recorded sessions.")
    ls.add_argument("--host", default=None,
                    help="Logger IP. If omitted, auto-discover among known AP IPs.")
    ls.add_argument("--timeout", type=float, default=15.0)
    ls.add_argument("-v", "--verbose", action="store_true",
                    help="Trace every TCP frame (tx/rx) with decoded cmd/status.")
    fmt = ls.add_mutually_exclusive_group()
    fmt.add_argument("--raw-csv", action="store_true",
                     help="Print the raw CSV exactly as the device returned it.")
    fmt.add_argument("--json", action="store_true",
                     help="Print JSON array of row dicts.")
    ls.set_defaults(func=cmd_list)

    dl = sub.add_parser("download", help="Download session file(s).")
    dl.add_argument("--host", default=None,
                    help="Logger IP. If omitted, auto-discover among known AP IPs.")
    dl.add_argument("--timeout", type=float, default=30.0)
    dl.add_argument("-o", "--out", default=".", help="Output directory.")
    dl.add_argument("--all", action="store_true", help="Download every session.")
    dl.add_argument("--force", action="store_true",
                    help="Overwrite existing files even if size matches.")
    dl.add_argument("--quiet", action="store_true", help="Suppress progress bar.")
    dl.add_argument("-v", "--verbose", action="store_true",
                    help="Trace every TCP frame (tx/rx) with decoded cmd/status.")
    dl.add_argument("names", nargs="*",
                    help="Session names (a_7064.xrz), short names (a_7064), or 1-based indices.")
    dl.set_defaults(func=cmd_download)

    rm = sub.add_parser("delete", help="Delete session file(s) from the device.")
    rm.add_argument("--host", default=None,
                    help="Logger IP. If omitted, auto-discover among known AP IPs.")
    rm.add_argument("--timeout", type=float, default=30.0)
    rm.add_argument("--all", action="store_true", help="Delete every session.")
    rm.add_argument("-y", "--yes", action="store_true",
                    help="Delete without confirmation.")
    rm.add_argument("-v", "--verbose", action="store_true",
                    help="Trace every TCP frame (tx/rx) with decoded cmd/status.")
    rm.add_argument("names", nargs="*",
                    help="Session names (a_7064.xrz), short names (a_7064), or 1-based indices.")
    rm.set_defaults(func=cmd_delete)

    info = sub.add_parser("info", help="Dump device info block.")
    info.add_argument("--host", default=None,
                      help="Logger IP. If omitted, auto-discover among known AP IPs.")
    info.add_argument("--timeout", type=float, default=15.0)
    info.add_argument("-v", "--verbose", action="store_true",
                      help="Trace every TCP frame.")
    info.set_defaults(func=cmd_info)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ProtocolError as e:
        print(f"protocol error: {e}", file=sys.stderr)
        return 1
    except (ConnectionError, socket.timeout, OSError) as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
