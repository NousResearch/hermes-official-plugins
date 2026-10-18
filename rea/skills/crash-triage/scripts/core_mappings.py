#!/usr/bin/env python3
"""List the files mapped into a crashed process, from a Linux ELF core's NT_FILE note.

REA's inspect_recorded_crash returns every note's raw bytes but does not decode
NT_FILE, which is what turns a runtime address into module+offset. This reads the
core (and optionally the binary's program headers) and never runs anything.

  core_mappings.py CORE [--binary BIN] [--addr 0x... ...]

Prints JSON: the mappings, and with --binary
  - build_id: the binary's GNU build ID next to the one in the core's copy of its
    headers, and whether they match (a rebuilt binary gives wrong names);
  - the binary's load bias, found by file name or, for a renamed copy, by build ID;
  - for each --addr: the containing module, the link-time address to match
    against inspect_binary_layout's function symbols (value <= linked < value +
    size), its file offset, and the function that contains it from .eh_frame_hdr
    (named sub_<start> for stripped binaries) with the file offset of its entry,
    for REA's procedure_pseudo_code ("file-offset:0x...").
Standard library only; ELF64 little-endian (x86-64 or AArch64 cores).
"""
import argparse
import json
import os
import struct
import sys

NT_FILE = 0x46494C45
NT_GNU_BUILD_ID = 3
PT_LOAD, PT_NOTE = 1, 4
PT_GNU_EH_FRAME = 0x6474E550


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


def build_id_from_notes(notes):
    pos = 0
    while pos + 12 <= len(notes):
        namesz, descsz, ntype = struct.unpack_from("<III", notes, pos)
        desc = pos + 12 + ((namesz + 3) & ~3)
        if desc + descsz > len(notes):
            return None
        if ntype == NT_GNU_BUILD_ID and notes[pos + 12:pos + 12 + namesz] == b"GNU\0":
            return notes[desc:desc + descsz].hex()
        pos = desc + ((descsz + 3) & ~3)
    return None


def binary_build_id(data, phdrs):
    for ph in phdrs:
        if ph["type"] == PT_NOTE:
            found = build_id_from_notes(data[ph["offset"]:ph["offset"] + ph["filesz"]])
            if found:
                return found
    return None


def core_image_build_id(core, image_base):
    """Build ID from the core's copy of an image's ELF headers (Linux dumps each
    mapped ELF file's first page by default). None when that memory was not recorded."""
    data, _type, phdrs = core
    loads = [p for p in phdrs if p["type"] == PT_LOAD]

    def mem(addr, size):
        seg = next((p for p in loads if p["vaddr"] <= addr and addr + size <= p["vaddr"] + p["filesz"]), None)
        return None if seg is None else data[seg["offset"] + addr - seg["vaddr"]:][:size]

    ehdr = mem(image_base, 64)
    if ehdr is None or ehdr[:4] != b"\x7fELF":
        return None
    phoff, = struct.unpack_from("<Q", ehdr, 32)
    phentsize, phnum = struct.unpack_from("<HH", ehdr, 54)
    table = mem(image_base + phoff, phentsize * phnum)
    if table is None:
        return None
    entries = [struct.unpack_from("<IIQQQQ", table, i * phentsize) for i in range(phnum)]
    link_base = next((vaddr & ~0xFFF for t, _f, _o, vaddr, _p, _s in entries if t == PT_LOAD), None)
    if link_base is None:
        return None
    for t, _f, _o, vaddr, _p, size in entries:
        if t == PT_NOTE:
            notes = mem(image_base + vaddr - link_base, size)
            found = notes and build_id_from_notes(notes)
            if found:
                return found
    return None


def read_encoded(buf, pos, enc, field_addr, data_rel):
    fmt, app = enc & 0x0F, enc & 0x70
    codes = {0x03: ("<I", 4), 0x0B: ("<i", 4), 0x04: ("<Q", 8), 0x0C: ("<q", 8)}
    if fmt not in codes or app not in (0x00, 0x10, 0x30):
        return None
    value, = struct.unpack_from(codes[fmt][0], buf, pos)
    value += field_addr if app == 0x10 else data_rel if app == 0x30 else 0
    return value & (2 ** 64 - 1), codes[fmt][1]


def function_ranges(data, phdrs):
    """(start, end or None) link-time function ranges from .eh_frame_hdr's sorted
    table. Unwinding needs it, so it survives strip."""
    hdr_ph = next((p for p in phdrs if p["type"] == PT_GNU_EH_FRAME), None)
    if hdr_ph is None or hdr_ph["filesz"] < 12:
        return []
    hdr, addr = data[hdr_ph["offset"]:hdr_ph["offset"] + hdr_ph["filesz"]], hdr_ph["vaddr"]
    if hdr[0] != 1:
        return []
    frame = read_encoded(hdr, 4, hdr[1], addr + 4, addr)
    if frame is None:
        return []
    pos = 4 + frame[1]
    count = read_encoded(hdr, pos, hdr[2], addr + pos, addr)
    if count is None:
        return []
    pos += count[1]
    size = 4 if hdr[3] & 0x0F in (0x03, 0x0B) else 8
    ranges = []
    for _ in range(count[0]):
        if pos + 2 * size > len(hdr):
            break
        start = read_encoded(hdr, pos, hdr[3], addr + pos, addr)
        fde = read_encoded(hdr, pos + size, hdr[3], addr + pos + size, addr)
        if start is None or fde is None:
            return []
        end, off = None, file_offset(phdrs, fde[0])
        # GCC and Clang write pc_begin as pcrel|sdata4 and pc_range in the same format.
        if off is not None and off + 16 <= len(data) and struct.unpack_from("<I", data, off)[0] != 0xFFFFFFFF:
            begin = (fde[0] + 8 + struct.unpack_from("<i", data, off + 8)[0]) & (2 ** 64 - 1)
            if begin == start[0]:
                end = start[0] + struct.unpack_from("<I", data, off + 12)[0]
        ranges.append((start[0], end))
        pos += 2 * size
    return sorted(ranges)


def range_for(ranges, linked):
    best = None
    for start, end in ranges:
        if start > linked:
            break
        best = (start, end)
    if best is None or (best[1] is not None and linked >= best[1]):
        return None
    return best


def file_offset(phdrs, vaddr):
    seg = next((p for p in phdrs if p["type"] == PT_LOAD and p["vaddr"] <= vaddr < p["vaddr"] + p["filesz"]), None)
    return None if seg is None else seg["offset"] + vaddr - seg["vaddr"]


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


def find_image(mappings, binary, core, own_id):
    """The binary's mapping at file offset 0: by file name, else (a renamed copy) by build ID."""
    name = os.path.basename(binary)
    first = next((m for m in mappings if m["file_offset"] == 0 and os.path.basename(m["path"]) == name), None)
    if first is None and own_id:
        first = next((m for m in mappings if m["file_offset"] == 0
                      and core_image_build_id(core, m["start"]) == own_id), None)
    return first


def load_bias(first, phdrs):
    """Runtime base of the binary minus its link-time base."""
    # The mapping at file offset 0 corresponds to the PT_LOAD that covers offset 0.
    seg = next((p for p in phdrs if p["type"] == PT_LOAD and p["offset"] == 0), None)
    if first is None or seg is None:
        return None
    return first["start"] - (seg["vaddr"] & ~0xFFF)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("core")
    ap.add_argument("--binary", help="the exact binary that crashed")
    ap.add_argument("--addr", action="append", default=[], help="runtime address to place (repeatable)")
    args = ap.parse_args()

    mappings = nt_file_mappings(args.core)
    out = {"mappings": [dict(m, start=hex(m["start"]), end=hex(m["end"]), file_offset=hex(m["file_offset"]))
                        for m in mappings]}
    bias, image, ranges, bin_phdrs = None, None, [], []
    if args.binary:
        core = read_elf64(args.core)
        bin_data, _type, bin_phdrs = read_elf64(args.binary)
        own_id = binary_build_id(bin_data, bin_phdrs)
        first = find_image(mappings, args.binary, core, own_id)
        image = first["path"] if first else None
        core_id = core_image_build_id(core, first["start"]) if first else None
        out["build_id"] = {"binary": own_id, "core": core_id,
                           "match": None if not (own_id and core_id) else own_id == core_id}
        if out["build_id"]["match"] is False:
            out["build_id"]["warning"] = "not the binary that crashed: function names would be wrong"
        bias = load_bias(first, bin_phdrs)
        out["load_bias"] = hex(bias) if bias is not None else None
        if bias is None:
            out["load_bias_unknown"] = "no mapping of this binary in the core, by file name or build ID"
        elif image and os.path.basename(image) != os.path.basename(args.binary):
            out["mapped_as"] = image
        ranges = function_ranges(bin_data, bin_phdrs)
    placed = []
    for text in args.addr:
        addr = int(text, 16)
        m = next((m for m in mappings if m["start"] <= addr < m["end"]), None)
        entry = {"addr": hex(addr), "module": os.path.basename(m["path"]) if m else None}
        if m is None:
            entry["note"] = "not in any mapped file (heap, stack, anonymous or unmapped memory)"
        elif bias is not None and m["path"] == image:
            linked = addr - bias
            entry["linked"] = hex(linked)
            off = file_offset(bin_phdrs, linked)
            if off is not None:
                entry["file_offset"] = hex(off)
            found = range_for(ranges, linked)
            if found:
                start, end = found
                fn = {"name": f"sub_{start:x}", "start": hex(start), "source": ".eh_frame",
                      "offset": hex(linked - start)}
                if end is not None:
                    fn["end"] = hex(end)
                start_off = file_offset(bin_phdrs, start)
                if start_off is not None:
                    fn["file_offset"] = hex(start_off)
                entry["function"] = fn
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
