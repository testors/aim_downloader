#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aim_telemetry import read_raw_bytes


FOOTER_FIELDS = (
    ("RCR", "racer"),
    ("VEH", "vehicle"),
    ("VTY", "vehicle_type"),
    ("NTE", "note"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert AiM .xrz/.raw telemetry into export-style .xrk files."
    )
    parser.add_argument("input", type=Path, help="Input .xrz or decompressed raw file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output .xrk path (default: <input stem>.xrk)",
    )
    parser.add_argument(
        "--input-format",
        choices=("auto", "xrz", "raw"),
        default="auto",
        help="How to interpret the input bytes",
    )
    parser.add_argument("--racer", default="", help="Footer RCR value")
    parser.add_argument("--vehicle", default="", help="Footer VEH value")
    parser.add_argument("--vehicle-type", default="", help="Footer VTY value")
    parser.add_argument("--note", default="", help="Footer NTE value")
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding for footer payloads",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists",
    )
    return parser.parse_args()


def encode_tag(tag: str) -> bytes:
    encoded = tag.encode("ascii")
    if len(encoded) > 4:
        raise ValueError(f"tag must be at most 4 ASCII bytes: {tag!r}")
    return encoded.ljust(4, b"\0")


def wrap_frame(tag: str, payload: bytes, cls: int = 0x00) -> bytes:
    tag_bytes = encode_tag(tag)
    checksum = (sum(payload) & 0xFFFF).to_bytes(2, "little")
    header = b"<h" + tag_bytes + len(payload).to_bytes(4, "little") + bytes((cls,)) + b">"
    trailer = b"<" + tag_bytes + checksum + b">"
    return header + payload + trailer


def encode_footer_value(name: str, value: str, encoding: str) -> bytes:
    if "\0" in value:
        raise ValueError(f"{name} cannot contain NUL bytes")
    try:
        return value.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(f"could not encode {name!r} with {encoding}: {exc}") from exc


def build_export_footer(args: argparse.Namespace) -> bytes:
    footer = bytearray()
    for tag, attr in FOOTER_FIELDS:
        payload = encode_footer_value(attr, getattr(args, attr), args.encoding)
        footer.extend(wrap_frame(tag, payload, cls=0x00))
    return bytes(footer)


def main() -> int:
    args = parse_args()
    output_path = args.output or args.input.with_suffix(".xrk")

    if output_path.exists() and not args.force:
        print(
            f"Refusing to overwrite existing file: {output_path} (pass --force to replace it)",
            file=sys.stderr,
        )
        return 1

    try:
        raw, source_mode = read_raw_bytes(args.input, args.input_format)
        footer = build_export_footer(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output_bytes = raw + footer

    try:
        output_path.write_bytes(output_bytes)
    except OSError as exc:
        print(f"error: could not write {output_path}: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(output_bytes)} bytes to {output_path}")
    print(f"Body bytes: {len(raw)} ({source_mode})")
    print(f"Footer bytes: {len(footer)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
