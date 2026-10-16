#!/usr/bin/env python3
"""List the files mapped into a crashed process, from a Linux ELF core's NT_FILE note.

REA's inspect_recorded_crash returns every note's raw bytes but does not decode
NT_FILE, which is what turns a runtime address into module+offset. This reads the
core (and optionally the binary's program headers) and never runs anything.

  core_mappings.py CORE [--binary BIN] [--addr 0x... ...]

Prints JSON: the mappings, and with --binary the binary's load bias plus, for each
--addr, the containing module and the link-time address to match against
inspect_binary_layout's function symbols (value <= linked < value + size).
Standard library only; ELF64 little-endian (x86-64 or AArch64 cores).
"""
import argparse
import json
import os
import struct
import sys

NT_FILE = 0x46494C45
PT_LOAD, PT_NOTE = 1, 4


def read_elf64(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        sys.exit(f"{path}: not an ELF64 little-endian file")
    e_type, = struct.unpack_from("<H", data, 16)
    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 54)
    phdrs = []
    for i in range(e_phnum):
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", data, e_phoff + i * e_phentsize)
        phdrs.append({"type": p_type, "offset": p_offset, "vaddr": p_vaddr, "filesz": p_filesz})
    return data, e_type, phdrs


def nt_file_mappings(core_path):
    data, e_type, phdrs = read_elf64(core_path)
    if e_type != 4:
        sys.exit(f"{core_path}: not a core file (e_type {e_type})")
    for ph in phdrs:
        if ph["type"] != PT_NOTE:
            continue
        pos, end = ph["offset"], ph["offset"] + ph["filesz"]
        while pos + 12 <= end:
            namesz, descsz, ntype = struct.unpack_from("<III", data, pos)
            desc = pos + 12 + ((namesz + 3) & ~3)
            if ntype == NT_FILE:
                count, page_size = struct.unpack_from("<QQ", data, desc)
                entries = [struct.unpack_from("<QQQ", data, desc + 16 + 24 * i) for i in range(count)]
                names = data[desc + 16 + 24 * count: desc + descsz].split(b"\0")
                return [{"start": s, "end": e, "file_offset": o * page_size,
                         "path": names[i].decode("utf-8", "replace")}
                        for i, (s, e, o) in enumerate(entries)]
            pos = desc + ((descsz + 3) & ~3)
    sys.exit(f"{core_path}: no NT_FILE note (mapped files were not recorded)")


def load_bias(mappings, binary_path):
    """Runtime base of the binary minus its link-time base, matched by file name."""
    name = os.path.basename(binary_path)
    own = [m for m in mappings if os.path.basename(m["path"]) == name]
    if not own:
        return None, f"no mapping of a file named {name!r} in the core"
    _data, _type, phdrs = read_elf64(binary_path)
    loads = [p for p in phdrs if p["type"] == PT_LOAD]
    # The mapping at file offset 0 corresponds to the PT_LOAD that covers offset 0.
    first = next((m for m in own if m["file_offset"] == 0), None)
    seg = next((p for p in loads if p["offset"] == 0), None)
    if first is None or seg is None:
        return None, "could not pair the binary's first segment with a mapping"
    return first["start"] - (seg["vaddr"] & ~0xFFF), None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("core")
    ap.add_argument("--binary", help="the exact binary that crashed")
    ap.add_argument("--addr", action="append", default=[], help="runtime address to place (repeatable)")
    args = ap.parse_args()

    mappings = nt_file_mappings(args.core)
    out = {"mappings": [dict(m, start=hex(m["start"]), end=hex(m["end"]), file_offset=hex(m["file_offset"]))
                        for m in mappings]}
    bias, why = (None, None)
    if args.binary:
        bias, why = load_bias(mappings, args.binary)
        out["load_bias"] = hex(bias) if bias is not None else None
        if why:
            out["load_bias_unknown"] = why
    placed = []
    for text in args.addr:
        addr = int(text, 16)
        m = next((m for m in mappings if m["start"] <= addr < m["end"]), None)
        entry = {"addr": hex(addr), "module": os.path.basename(m["path"]) if m else None}
        if m is None:
            entry["note"] = "not in any mapped file (heap, stack, anonymous or unmapped memory)"
        elif args.binary and bias is not None and entry["module"] == os.path.basename(args.binary):
            entry["linked"] = hex(addr - bias)
        else:
            base = min(x["start"] for x in mappings if x["path"] == m["path"])
            entry["module_offset"] = hex(addr - base)
        placed.append(entry)
    if placed:
        out["addresses"] = placed
    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
