#!/usr/bin/env python3
"""
kicad_merge.py  —  Part 2 of the KiCad merge tool
===================================================
Merges two KiCad 8 projects whose references have already been
normalised (non-overlapping ranges) by kicad_ref_normalise.py.

Usage
-----
  # Merge the two ForMerging sub-folders into ForMerging/merge/
  python3 kicad_merge.py  ForMerging/same-TRA-1004E/  ForMerging/offset-UTR11-1104E/  ForMerging/merge/

  # Dry-run — print diagnostics without writing
  python3 kicad_merge.py  same/  offset/  merge/  --dry-run

How it works
------------
KiCad links PCB footprints to schematic symbols via UUID, NOT via
reference strings.  Each footprint carries:

    (path   "/SYMBOL_UUID")        ← points directly to the SCH symbol
    (sheetfile "project.kicad_sch") ← which SCH file owns that symbol

Each SCH placed symbol has:

    (uuid "SYMBOL_UUID")           ← must match every PCB footprint that
                                     references it
    (instances (project "name"
      (path "/SHEET_UUID"           ← sheet-level path (= the SCH file's
        (reference "R11")            own top-level uuid)

When you copy-paste symbols in the editor KiCad REASSIGNS new UUIDs,
breaking the footprint→symbol link and causing KiCad to fall back to
reference-string matching — which is the source of the chaos observed.

This script merges by TEXT CONCATENATION, preserving every UUID, and
only updates the two fields that must change:

    SCH: (project "old")        → (project "merge")
    SCH: (path "/old-sheet-uuid") → (path "/merge-sheet-uuid")
    PCB: (sheetfile "old.kicad_sch") → (sheetfile "merge.kicad_sch")

After merging, the uuid chain is intact:
    PCB (path "/SYM_UUID") → SCH symbol (uuid "SYM_UUID") ✓

The script also merges the (lib_symbols ...) blocks from both SCH files,
deduplicating by symbol name so shared library symbols appear only once.

Output
------
  merge/merge.kicad_sch  — merged schematic
  merge/merge.kicad_pcb  — merged PCB (footprints only; no board outline)
  (merge.kicad_pro and merge.kicad_prl are copied from the template
   by kicad_ref_normalise.py and are not modified here)
"""

import re
import sys
import uuid
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# S-expression block extractors
# ---------------------------------------------------------------------------

def _find_block(text: str, start_token: str, start_pos: int = 0) -> tuple[int, int]:
    """
    Find the start and end of the first S-expression block beginning with
    *start_token* at or after *start_pos*.  Returns (start, end) byte
    offsets (end is exclusive, i.e. text[start:end] is the full block).
    Returns (-1, -1) if not found.
    """
    pos = text.find(start_token, start_pos)
    if pos == -1:
        return -1, -1
    depth = 0
    for i in range(pos, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return pos, i + 1
    return -1, -1


def get_top_uuid(text: str) -> str:
    """
    Return the top-level (uuid "...") of a kicad_sch file — this is the
    sheet's own UUID, used as the path prefix in all instances blocks.
    """
    m = re.search(r'\(uuid\s+"([^"]+)"\)', text[:800])
    return m.group(1) if m else ""


def get_project_name(sch_text: str) -> str:
    """Return the project name used in (instances (project "name" ...))."""
    m = re.search(r'\(instances\s*\(project\s*"([^"]+)"', sch_text)
    return m.group(1) if m else ""


def extract_lib_symbols(sch_text: str) -> list[str]:
    """
    Return the individual (symbol ...) definition strings from inside the
    (lib_symbols ...) block.  Each entry is a complete s-expression.
    """
    start, end = _find_block(sch_text, '(lib_symbols')
    if start == -1:
        return []
    inner = sch_text[start + len('(lib_symbols'):end - 1]
    syms = []
    i = 0
    while i < len(inner):
        # Skip whitespace
        if inner[i] in (' ', '\t', '\n', '\r'):
            i += 1
            continue
        if inner[i] == '(':
            # Find matching close
            depth = 0
            for j in range(i, len(inner)):
                if inner[j] == '(':
                    depth += 1
                elif inner[j] == ')':
                    depth -= 1
                    if depth == 0:
                        syms.append(inner[i:j + 1])
                        i = j + 1
                        break
            else:
                break
        else:
            i += 1
    return syms


def _sym_lib_name(sym_text: str) -> str:
    """Extract the lib symbol name, e.g. 'Device:R' from (symbol "Device:R" ...)."""
    m = re.match(r'\(symbol\s+"([^"]+)"', sym_text.strip())
    return m.group(1) if m else ""


def merge_lib_symbols(lib1: list[str], lib2: list[str]) -> list[str]:
    """
    Union of two lib_symbols lists, deduplicated by symbol name.
    lib1 entries take precedence on collision (project1 = authoritative).
    """
    seen: dict[str, str] = {}
    for sym in lib1 + lib2:
        name = _sym_lib_name(sym)
        if name and name not in seen:
            seen[name] = sym
    return list(seen.values())


def extract_placed_symbols(sch_text: str) -> list[str]:
    """
    Return all placed (symbol ...) s-expression strings from the schematic,
    excluding the lib_symbols template block.
    """
    # Remove lib_symbols block so its (symbol ...) entries are invisible
    lib_start, lib_end = _find_block(sch_text, '(lib_symbols')
    if lib_start != -1:
        sch_text = sch_text[:lib_start] + sch_text[lib_end:]

    syms = []
    # Placed symbols are \n\t(symbol\n at top level
    for m in re.finditer(r'\n\t\(symbol\s*\n', sch_text):
        start = m.start() + 1   # exclude the leading \n
        depth = 0
        for i in range(start, len(sch_text)):
            if sch_text[i] == '(':
                depth += 1
            elif sch_text[i] == ')':
                depth -= 1
                if depth == 0:
                    syms.append(sch_text[start:i + 1])
                    break
    return syms


def extract_footprints(pcb_text: str) -> list[str]:
    """
    Return all (footprint ...) s-expression strings from the PCB file.
    """
    fps = []
    for m in re.finditer(r'\n\t\(footprint\s+"', pcb_text):
        start = m.start() + 1
        depth = 0
        for i in range(start, len(pcb_text)):
            if pcb_text[i] == '(':
                depth += 1
            elif pcb_text[i] == ')':
                depth -= 1
                if depth == 0:
                    fps.append(pcb_text[start:i + 1])
                    break
    return fps


def extract_graphical(pcb_text: str) -> list[str]:
    """
    Return all gr_text / gr_line / gr_arc / gr_rect / gr_poly / zone /
    segment / via / dimension top-level elements from the PCB.
    These carry board outline, copper, text annotations etc.
    """
    GR_TOKENS = ('(gr_text ', '(gr_line ', '(gr_arc ', '(gr_rect ',
                 '(gr_poly ', '(zone ', '(segment ', '(via ',
                 '(dimension ')
    items = []
    for m in re.finditer(r'\n\t\((?:gr_text|gr_line|gr_arc|gr_rect|gr_poly'
                         r'|zone|segment|via|dimension)\s', pcb_text):
        start = m.start() + 1
        depth = 0
        for i in range(start, len(pcb_text)):
            if pcb_text[i] == '(':
                depth += 1
            elif pcb_text[i] == ')':
                depth -= 1
                if depth == 0:
                    items.append(pcb_text[start:i + 1])
                    break
    return items


# ---------------------------------------------------------------------------
# Repointing helpers
# ---------------------------------------------------------------------------

def repoint_symbol(sym_text: str,
                   old_sheet_uuid: str, old_project: str,
                   new_sheet_uuid: str, new_project: str) -> str:
    """
    Update the instances block inside a placed symbol to point to the
    merged sheet.  Only these two tokens change; all symbol UUIDs are
    preserved exactly.
    """
    if old_project:
        sym_text = sym_text.replace(
            f'(project "{old_project}"',
            f'(project "{new_project}"'
        )
    if old_sheet_uuid:
        sym_text = sym_text.replace(
            f'(path "/{old_sheet_uuid}"',
            f'(path "/{new_sheet_uuid}"'
        )
    return sym_text


def repoint_footprint(fp_text: str,
                      old_sch_file: str,
                      new_sch_file: str) -> str:
    """
    Update (sheetfile "old.kicad_sch") → (sheetfile "new.kicad_sch").
    The (path "/SYMBOL_UUID") is left completely unchanged.
    """
    if old_sch_file:
        fp_text = fp_text.replace(
            f'(sheetfile "{old_sch_file}")',
            f'(sheetfile "{new_sch_file}")'
        )
    return fp_text


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_uuid_chain(merged_sch: str, merged_pcb: str,
                      merge_sheet_uuid: str) -> list[str]:
    """
    Check that:
    1. Every SCH instance (path ...) == /merge_sheet_uuid
    2. Every PCB (sheetfile) == "merge.kicad_sch"
    3. Every PCB (path "/SYM_UUID") resolves to a symbol in the merged SCH

    Returns a list of problem strings (empty = all OK).
    """
    problems = []

    # 1. SCH instance paths
    inst_paths = set(re.findall(
        r'\(path\s+"([^"]+)"\s*\n\s*\(reference', merged_sch))
    expected = f'/{merge_sheet_uuid}'
    wrong = inst_paths - {expected}
    if wrong:
        problems.append(
            f"SCH instance path(s) not updated: {sorted(wrong)[:5]}")

    # 2. PCB sheetfile
    sheetfiles = set(re.findall(r'\(sheetfile\s+"([^"]+)"\)', merged_pcb))
    wrong_sf = sheetfiles - {'merge.kicad_sch'}
    if wrong_sf:
        problems.append(f"PCB sheetfile(s) not updated: {wrong_sf}")

    # 3. PCB path → SCH symbol uuid resolution
    pcb_path_uuids = {
        m.lstrip('/') for m in
        re.findall(r'\(path\s+"(/[^"]+)"\)', merged_pcb)
    }
    sch_sym_uuids = set(re.findall(r'\(uuid\s+"([^"]+)"\)', merged_sch))
    unresolved = pcb_path_uuids - sch_sym_uuids
    if unresolved:
        problems.append(
            f"{len(unresolved)} PCB footprint(s) whose path uuid is not "
            f"found in merged SCH (first 5: {sorted(unresolved)[:5]})")

    return problems


# ---------------------------------------------------------------------------
# Assemblers
# ---------------------------------------------------------------------------

def assemble_sch(merge_sheet_uuid: str,
                 merged_lib: list[str],
                 all_symbols: list[str],
                 paper: str = "A4") -> str:
    lib_lines = "\n".join(f"\t\t{s}" for s in merged_lib)
    sym_lines = "\n".join(f"\t{s}" for s in all_symbols)
    return (
        f'(kicad_sch (version 20231120) (generator "eeschema")'
        f' (generator_version "8.0")\n'
        f'\t(uuid "{merge_sheet_uuid}")\n'
        f'\t(paper "{paper}")\n'
        f'\t(lib_symbols\n{lib_lines}\n\t)\n'
        f'{sym_lines}\n'
        f')\n'
    )


def assemble_pcb(all_footprints: list[str],
                 all_graphical: list[str]) -> str:
    fp_lines   = "\n".join(f"\t{f}" for f in all_footprints)
    gr_lines   = "\n".join(f"\t{g}" for g in all_graphical)
    body = fp_lines
    if gr_lines:
        body += "\n" + gr_lines
    return (
        f'(kicad_pcb (version 20240108) (generator "pcbnew")'
        f' (generator_version "8.0")\n'
        f'{body}\n'
        f')\n'
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def merge_projects(p1_dir: Path, p2_dir: Path, out_dir: Path,
                   dry_run: bool = False) -> None:

    def find_single(folder, ext):
        files = list(folder.glob(f"*{ext}"))
        if len(files) != 1:
            raise FileNotFoundError(
                f"Expected exactly one {ext} in {folder}, found {len(files)}")
        return files[0]

    p1_sch_path = find_single(p1_dir, ".kicad_sch")
    p1_pcb_path = find_single(p1_dir, ".kicad_pcb")
    p2_sch_path = find_single(p2_dir, ".kicad_sch")
    p2_pcb_path = find_single(p2_dir, ".kicad_pcb")

    p1_sch = p1_sch_path.read_text(encoding="utf-8")
    p1_pcb = p1_pcb_path.read_text(encoding="utf-8")
    p2_sch = p2_sch_path.read_text(encoding="utf-8")
    p2_pcb = p2_pcb_path.read_text(encoding="utf-8")

    # Extract source metadata
    p1_sheet_uuid = get_top_uuid(p1_sch)
    p2_sheet_uuid = get_top_uuid(p2_sch)
    p1_project    = get_project_name(p1_sch)
    p2_project    = get_project_name(p2_sch)
    p1_sch_file   = next(iter(set(re.findall(
        r'\(sheetfile\s+"([^"]+)"\)', p1_pcb))), "")
    p2_sch_file   = next(iter(set(re.findall(
        r'\(sheetfile\s+"([^"]+)"\)', p2_pcb))), "")

    merge_sheet_uuid = str(uuid.uuid4())

    print(f"\n{'='*60}")
    print(f"Merging:")
    print(f"  P1 : {p1_dir}  (sheet {p1_sheet_uuid[:8]}…  proj={p1_project!r})")
    print(f"  P2 : {p2_dir}  (sheet {p2_sheet_uuid[:8]}…  proj={p2_project!r})")
    print(f"  Out: {out_dir}  (new sheet uuid {merge_sheet_uuid[:8]}…)")

    # ── lib_symbols ──────────────────────────────────────────────────────
    lib1 = extract_lib_symbols(p1_sch)
    lib2 = extract_lib_symbols(p2_sch)
    merged_lib = merge_lib_symbols(lib1, lib2)
    print(f"\n  lib_symbols: p1={len(lib1)}  p2={len(lib2)}"
          f"  merged={len(merged_lib)} (deduplicated)")

    # ── placed symbols ───────────────────────────────────────────────────
    syms1_raw = extract_placed_symbols(p1_sch)
    syms2_raw = extract_placed_symbols(p2_sch)
    syms1 = [repoint_symbol(s, p1_sheet_uuid, p1_project,
                             merge_sheet_uuid, "merge") for s in syms1_raw]
    syms2 = [repoint_symbol(s, p2_sheet_uuid, p2_project,
                             merge_sheet_uuid, "merge") for s in syms2_raw]
    print(f"  Placed symbols: p1={len(syms1)}  p2={len(syms2)}"
          f"  total={len(syms1)+len(syms2)}")

    # ── footprints ───────────────────────────────────────────────────────
    fps1_raw = extract_footprints(p1_pcb)
    fps2_raw = extract_footprints(p2_pcb)
    fps1 = [repoint_footprint(f, p1_sch_file, "merge.kicad_sch")
            for f in fps1_raw]
    fps2 = [repoint_footprint(f, p2_sch_file, "merge.kicad_sch")
            for f in fps2_raw]
    print(f"  Footprints: p1={len(fps1)}  p2={len(fps2)}"
          f"  total={len(fps1)+len(fps2)}")

    # ── graphical elements (gr_text annotations from ref_normalise) ──────
    gr1 = extract_graphical(p1_pcb)
    gr2 = extract_graphical(p2_pcb)
    print(f"  Graphical items: p1={len(gr1)}  p2={len(gr2)}"
          f"  total={len(gr1)+len(gr2)}")

    # ── assemble ─────────────────────────────────────────────────────────
    merged_sch = assemble_sch(merge_sheet_uuid, merged_lib, syms1 + syms2)
    merged_pcb = assemble_pcb(fps1 + fps2, gr1 + gr2)

    # ── verify uuid chain ────────────────────────────────────────────────
    problems = verify_uuid_chain(merged_sch, merged_pcb, merge_sheet_uuid)
    print()
    if problems:
        print("  *** UUID CHAIN PROBLEMS ***")
        for p in problems:
            print(f"    {p}")
    else:
        pcb_fp_count = len(fps1) + len(fps2)
        pcb_path_uuids = set(re.findall(
            r'\(path\s+"(/[^"]+)"\)', merged_pcb))
        print(f"  UUID chain: PASS")
        print(f"    {pcb_fp_count} footprints → all paths resolve to"
              f" SCH symbol uuids ✓")
        print(f"    All SCH instance paths → /{merge_sheet_uuid[:8]}… ✓")
        print(f"    All PCB sheetfiles → merge.kicad_sch ✓")

    if dry_run:
        print("\n  Dry-run: no files written.")
        return

    if problems:
        print("\n  Aborting write due to UUID chain problems.")
        sys.exit(1)

    # ── write ────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    out_sch = out_dir / "merge.kicad_sch"
    out_pcb = out_dir / "merge.kicad_pcb"
    out_sch.write_text(merged_sch, encoding="utf-8")
    out_pcb.write_text(merged_pcb, encoding="utf-8")

    print(f"\n  Written:")
    print(f"    {out_sch}")
    print(f"    {out_pcb}")
    print(f"\n  Open {out_dir}/merge.kicad_pro in KiCad to verify.")


def main():
    parser = argparse.ArgumentParser(
        description="Merge two normalised KiCad 8 projects (Part 2 of merge tool).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Typical workflow:
  # Step 1 — normalise and create ForMerging/ folder:
  python3 kicad_ref_normalise.py  TRA-1004E/  UTR11-1104E/

  # Step 2 — merge:
  python3 kicad_merge.py  ForMerging/same-TRA-1004E/  ForMerging/offset-UTR11-1104E/  ForMerging/merge/
""",
    )
    parser.add_argument("p1_dir",  type=Path, help="Project 1 folder (same-*)")
    parser.add_argument("p2_dir",  type=Path, help="Project 2 folder (offset-*)")
    parser.add_argument("out_dir", type=Path, help="Merge output folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="Verify and print without writing files.")
    args = parser.parse_args()

    for d in (args.p1_dir, args.p2_dir):
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory.", file=sys.stderr)
            sys.exit(1)

    merge_projects(args.p1_dir, args.p2_dir, args.out_dir,
                   dry_run=args.dry_run)


if __name__ == "__main__":
    main()
