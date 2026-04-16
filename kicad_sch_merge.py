#!/usr/bin/env python3
"""
kicad_sch_merge.py
==================
Merges two normalised KiCad 8 schematic files into one.

Strategy
--------
  1. Find the Y-maximum of all placed content in the same-project SCH.
  2. Shift every coordinate in the offset-project SCH down by
       Y_max_same + GAP   (default GAP = 2.54 * 20 = 50.8 mm)
  3. Merge the two lib_symbols blocks (union, deduplicating by name).
  4. Concatenate all placed elements (symbols, wires, labels, junctions…)
     from both files into a single kicad_sch.
  5. Update the offset-project's (instances (project …) / (path …)) to
     point at the merged sheet's UUID — the only structural change needed.

Coordinate fields shifted in the offset project
------------------------------------------------
  (at  X  Y  [angle])   — symbols, properties, labels, text, junctions,
                           no_connect, sheet pins …
  (xy  X  Y)            — wire / bus endpoints inside (pts …)

Everything else (UUIDs, reference strings, lib_ids, net names, pin
connections) is preserved byte-for-byte, so the PCB footprint→symbol
UUID chain remains intact.

Usage
-----
  python3 kicad_sch_merge.py  same-TRA-1004E/  offset-UTR11-1104E/  merge/

  # Custom gap (mm):
  python3 kicad_sch_merge.py  same/  offset/  merge/  --gap 60

  # Dry-run (print extents and offset, write nothing):
  python3 kicad_sch_merge.py  same/  offset/  merge/  --dry-run
"""

import re
import sys
import uuid
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_GAP_MM = 2.54 * 20   # 50.8 mm  — 20 grid units at 2.54 mm/grid


# ---------------------------------------------------------------------------
# Helpers: lib_symbols
# ---------------------------------------------------------------------------

def _find_block(text: str, token: str) -> tuple[int, int]:
    """Return (start, end) of the first S-expr starting with *token*."""
    pos = text.find(token)
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


def extract_lib_symbols(sch: str) -> list[str]:
    """Return individual (symbol …) strings from inside (lib_symbols …)."""
    start, end = _find_block(sch, '(lib_symbols')
    if start == -1:
        return []
    inner = sch[start + len('(lib_symbols'):end - 1]
    syms, i = [], 0
    while i < len(inner):
        if inner[i] in ' \t\n\r':
            i += 1
            continue
        if inner[i] == '(':
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


def merge_lib_symbols(lib1: list[str], lib2: list[str]) -> list[str]:
    """Union, deduplicating by symbol name. lib1 wins on collision."""
    seen: dict[str, str] = {}
    for s in lib1 + lib2:
        m = re.match(r'\(symbol\s+"([^"]+)"', s.strip())
        name = m.group(1) if m else None
        if name and name not in seen:
            seen[name] = s
    return list(seen.values())


def strip_lib_symbols(sch: str) -> str:
    """Remove the (lib_symbols …) block entirely from *sch*."""
    start, end = _find_block(sch, '(lib_symbols')
    if start == -1:
        return sch
    return sch[:start] + sch[end:]


# ---------------------------------------------------------------------------
# Helpers: placed elements
# ---------------------------------------------------------------------------

# Top-level element types that carry coordinates and must be included.
# 'version', 'generator', 'generator_version', 'uuid', 'paper',
# 'lib_symbols', 'sheet_instances' are header/footer — excluded.
_PLACED_TOKENS = {
    'symbol', 'wire', 'bus', 'bus_entry', 'junction', 'no_connect',
    'label', 'global_label', 'hierarchical_label', 'text', 'text_box',
    'polyline', 'rectangle', 'arc', 'circle', 'image',
    'sheet', 'netclass_flag',
}


def extract_placed(sch: str) -> list[str]:
    """
    Return all placed-element s-expression strings — everything that is
    a top-level element in the SCH other than header tokens and
    lib_symbols / sheet_instances.
    """
    # Remove lib_symbols first so its (symbol …) entries are invisible
    sch = strip_lib_symbols(sch)
    # Remove sheet_instances block
    sch_start, sch_end = _find_block(sch, '(sheet_instances')
    if sch_start != -1:
        sch = sch[:sch_start] + sch[sch_end:]

    elements = []
    for m in re.finditer(r'\n\t\((\w+)\s', sch):
        token = m.group(1)
        if token not in _PLACED_TOKENS:
            continue
        start = m.start() + 1   # skip leading \n
        depth = 0
        for i in range(start, len(sch)):
            if sch[i] == '(':
                depth += 1
            elif sch[i] == ')':
                depth -= 1
                if depth == 0:
                    elements.append(sch[start:i + 1])
                    break
    return elements


# ---------------------------------------------------------------------------
# Helpers: coordinate extent
# ---------------------------------------------------------------------------

def _sch_extent(sch: str) -> tuple[float, float, float, float]:
    """
    Return (x_min, y_min, x_max, y_max) across all coordinate tokens
    in the placed section (lib_symbols excluded).
    Covers: (at X Y), (xy X Y), (start X Y), (end X Y)
    """
    placed = strip_lib_symbols(sch)
    xs: list[float] = []
    ys: list[float] = []
    for pat in (r'\(at\s+([-\d.]+)\s+([-\d.]+)',
                r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)',
                r'\(start\s+([-\d.]+)\s+([-\d.]+)\)',
                r'\(end\s+([-\d.]+)\s+([-\d.]+)\)'):
        for m in re.finditer(pat, placed):
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def y_extent(sch: str) -> tuple[float, float]:
    """Return (y_min, y_max) of placed content."""
    _, y_min, _, y_max = _sch_extent(sch)
    return y_min, y_max


# ---------------------------------------------------------------------------
# Helpers: coordinate shifting
# ---------------------------------------------------------------------------

# (at  X  Y  [angle])
_AT_RE  = re.compile(r'(\(at\s+)([-\d.]+)(\s+)([-\d.]+)')
# (xy  X  Y)
_XY_RE  = re.compile(r'(\(xy\s+)([-\d.]+)(\s+)([-\d.]+)(\))')
# (start X Y) / (end X Y) — used by rectangle, arc, polyline in SCH
_SE_RE  = re.compile(r'(\((?:start|end)\s+)([-\d.]+)(\s+)([-\d.]+)(\))')


def _shift_at(text: str, dy: float) -> str:
    def _r(m):
        return m.group(1) + m.group(2) + m.group(3) + str(round(float(m.group(4)) + dy, 6))
    return _AT_RE.sub(_r, text)


def _shift_xy(text: str, dy: float) -> str:
    def _r(m):
        return m.group(1) + m.group(2) + m.group(3) + str(round(float(m.group(4)) + dy, 6)) + m.group(5)
    return _XY_RE.sub(_r, text)


def _shift_se(text: str, dy: float) -> str:
    """Shift Y in (start X Y) and (end X Y) tokens."""
    def _r(m):
        return m.group(1) + m.group(2) + m.group(3) + str(round(float(m.group(4)) + dy, 6)) + m.group(5)
    return _SE_RE.sub(_r, text)


def shift_element(elem: str, dy: float) -> str:
    """Shift ALL coordinate Y-values in a SCH element."""
    elem = _shift_at(elem, dy)
    elem = _shift_xy(elem, dy)
    elem = _shift_se(elem, dy)
    return elem


def shift_coordinates(sch: str, dy: float) -> str:
    """
    Apply *dy* Y-shift to every coordinate in the PLACED section of *sch*.
    The lib_symbols block is excluded (template coords are irrelevant).
    """
    if dy == 0.0:
        return sch
    lib_start, lib_end = _find_block(sch, '(lib_symbols')
    if lib_start == -1:
        return shift_element(sch, dy)
    before  = shift_element(sch[:lib_start], dy)
    lib_blk = sch[lib_start:lib_end]
    after   = shift_element(sch[lib_end:], dy)
    return before + lib_blk + after


# ---------------------------------------------------------------------------
# Helpers: annotation boxes
# ---------------------------------------------------------------------------

_SCH_PAD = 5.0   # mm padding around content for SCH box
_PCB_PAD_SCH = 3.0  # not used here but kept for reference


def _sch_box(x1: float, y1: float, x2: float, y2: float,
             label: str) -> list[str]:
    """
    Return [rectangle_str, text_str] for a labelled dashed box
    on the KiCad 8 schematic.
    """
    import uuid as _uuid
    ru, tu = str(_uuid.uuid4()), str(_uuid.uuid4())
    rect = (
        f'(rectangle\n'
        f'\t\t(start {x1:.3f} {y1:.3f})\n'
        f'\t\t(end {x2:.3f} {y2:.3f})\n'
        f'\t\t(stroke\n'
        f'\t\t\t(width 0.25)\n'
        f'\t\t\t(type dash)\n'
        f'\t\t)\n'
        f'\t\t(fill\n'
        f'\t\t\t(type none)\n'
        f'\t\t)\n'
        f'\t\t(uuid "{ru}")\n'
        f'\t)'
    )
    text = (
        f'(text "{label}"\n'
        f'\t\t(exclude_from_sim no)\n'
        f'\t\t(at {x1 + 2:.3f} {y1 + 4:.3f} 0)\n'
        f'\t\t(effects\n'
        f'\t\t\t(font\n'
        f'\t\t\t\t(size 3 3)\n'
        f'\t\t\t\t(thickness 0.3)\n'
        f'\t\t\t\t(bold yes)\n'
        f'\t\t\t)\n'
        f'\t\t\t(justify left top)\n'
        f'\t\t)\n'
        f'\t\t(uuid "{tu}")\n'
        f'\t)'
    )
    return [rect, text]


def _compute_paper(x_max: float, y_max: float, margin: float = 20.0) -> str:
    """
    Return a KiCad 'User W H' paper token string sized to fit the merged
    content plus *margin* mm on each trailing edge.
    Origin is always (0,0) so width = x_max + margin, height = y_max + margin.
    Floor to nearest mm, minimum A4 (297 x 210).
    """
    import math
    w = math.ceil(max(x_max + margin, 297))
    h = math.ceil(max(y_max + margin, 210))
    return f"User {w} {h}"


# ---------------------------------------------------------------------------
# Helpers: UUID and project repointing
# ---------------------------------------------------------------------------

def get_top_uuid(sch: str) -> str:
    m = re.search(r'\(uuid\s+"([^"]+)"\)', sch[:800])
    return m.group(1) if m else ""


def get_project_name(sch: str) -> str:
    m = re.search(r'\(instances\s*\(project\s*"([^"]+)"', sch)
    return m.group(1) if m else ""


def repoint_instances(sch: str,
                      old_sheet_uuid: str, old_project: str,
                      new_sheet_uuid: str, new_project: str = "merge") -> str:
    """
    Update all (instances (project "old") (path "/old-uuid" …)) blocks
    to point at the merged sheet.  Symbol UUIDs are NOT touched.
    """
    if old_project:
        sch = sch.replace(f'(project "{old_project}"',
                          f'(project "{new_project}"')
    if old_sheet_uuid:
        sch = sch.replace(f'(path "/{old_sheet_uuid}"',
                          f'(path "/{new_sheet_uuid}"')
    return sch


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

def assemble_sch(merge_uuid: str,
                 merged_lib: list[str],
                 all_elements: list[str],
                 paper: str = "A4") -> str:
    lib_inner = "\n".join(f"\t\t{s}" for s in merged_lib)
    elem_body = "\n".join(f"\t{e}" for e in all_elements)
    # Named size: (paper "A4")
    # Custom size: (paper "User" W H)  — W and H are separate numeric tokens
    parts = paper.split()
    if len(parts) == 3:                          # "User 435 665"
        paper_tok = f'\t(paper "{parts[0]}" {parts[1]} {parts[2]})'
    else:
        paper_tok = f'\t(paper "{paper}")'
    return (
        f'(kicad_sch (version 20231120) (generator "eeschema")'
        f' (generator_version "8.0")\n'
        f'\t(uuid "{merge_uuid}")\n'
        f'{paper_tok}\n'
        f'\t(lib_symbols\n{lib_inner}\n\t)\n'
        f'{elem_body}\n'
        f')\n'
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(merged_sch: str, merge_uuid: str) -> list[str]:
    problems = []
    inst_paths = set(re.findall(
        r'\(path\s+"([^"]+)"\s*\n\s*\(reference', merged_sch))
    wrong = inst_paths - {f'/{merge_uuid}'}
    if wrong:
        problems.append(
            f"Instance path(s) not updated: {sorted(wrong)[:5]}")
    return problems


# ---------------------------------------------------------------------------
# Main merge function
# ---------------------------------------------------------------------------

def merge_schematics(same_dir: Path, offset_dir: Path, out_dir: Path,
                     gap: float = DEFAULT_GAP_MM,
                     dry_run: bool = False) -> None:

    def find_sch(d: Path) -> Path:
        files = list(d.glob("*.kicad_sch"))
        if len(files) != 1:
            raise FileNotFoundError(
                f"Expected exactly one .kicad_sch in {d}, found {len(files)}")
        return files[0]

    same_path   = find_sch(same_dir)
    offset_path = find_sch(offset_dir)

    same_sch   = same_path.read_text(encoding="utf-8")
    offset_sch = offset_path.read_text(encoding="utf-8")

    # ── Extents ──────────────────────────────────────────────────────────
    sx_min, sy_min, sx_max, sy_max = _sch_extent(same_sch)
    ox_min, oy_min, ox_max, oy_max = _sch_extent(offset_sch)
    dy = round(sy_max + gap - oy_min, 6)

    print(f"\n{'='*60}")
    print(f"  same   : {same_path}")
    print(f"  offset : {offset_path}")
    print(f"\n  same   Y extent : {sy_min:.3f} .. {sy_max:.3f} mm")
    print(f"  offset Y extent : {oy_min:.3f} .. {oy_max:.3f} mm")
    print(f"  gap             : {gap:.3f} mm")
    print(f"  Y shift applied : {dy:.3f} mm"
          f"  (offset content starts at Y ≈ {round(sy_max + gap, 3):.3f})")

    # ── lib_symbols ───────────────────────────────────────────────────────
    lib_same   = extract_lib_symbols(same_sch)
    lib_offset = extract_lib_symbols(offset_sch)
    merged_lib = merge_lib_symbols(lib_same, lib_offset)
    print(f"\n  lib_symbols : same={len(lib_same)}"
          f"  offset={len(lib_offset)}"
          f"  merged={len(merged_lib)} (deduplicated)")

    # ── Placed elements ───────────────────────────────────────────────────
    elems_same   = extract_placed(same_sch)
    elems_offset = extract_placed(offset_sch)
    print(f"  Placed elements : same={len(elems_same)}"
          f"  offset={len(elems_offset)}"
          f"  total={len(elems_same)+len(elems_offset)}")

    # ── Shift offset elements (Y shift covers at/xy/start/end) ────────────
    shifted_offset = [shift_element(e, dy) for e in elems_offset]

    # ── Repoint instances ─────────────────────────────────────────────────
    merge_uuid      = str(uuid.uuid4())
    same_sheet_uuid = get_top_uuid(same_sch)
    off_sheet_uuid  = get_top_uuid(offset_sch)
    same_project    = get_project_name(same_sch)
    off_project     = get_project_name(offset_sch)

    repointed_same = [
        repoint_instances(e, same_sheet_uuid, same_project, merge_uuid)
        for e in elems_same
    ]
    repointed_offset = [
        repoint_instances(e, off_sheet_uuid, off_project, merge_uuid)
        for e in shifted_offset
    ]

    print(f"\n  Merge sheet UUID : {merge_uuid}")
    print(f"  same   sheet UUID : {same_sheet_uuid[:8]}…  proj={same_project!r}")
    print(f"  offset sheet UUID : {off_sheet_uuid[:8]}…  proj={off_project!r}")

    # ── Annotation boxes ──────────────────────────────────────────────────
    # Padded dashed rectangle + bold label for each project zone
    same_label   = same_path.stem    # e.g. "TRA-1004E"
    offset_label = offset_path.stem  # e.g. "UTR11-1104E"

    box_same = _sch_box(
        sx_min - _SCH_PAD, sy_min - _SCH_PAD,
        sx_max + _SCH_PAD, sy_max + _SCH_PAD,
        same_label,
    )
    # Offset box: apply the same dy shift to its coordinates
    ox1 = ox_min - _SCH_PAD
    oy1 = oy_min - _SCH_PAD
    ox2 = ox_max + _SCH_PAD
    oy2 = oy_max + _SCH_PAD
    box_offset = _sch_box(
        ox1, oy1 + dy,
        ox2, oy2 + dy,
        offset_label,
    )

    print(f"\n  Boxes added:")
    print(f"    [{same_label}]   Y {sy_min - _SCH_PAD:.1f} .. {sy_max + _SCH_PAD:.1f}")
    print(f"    [{offset_label}] Y {oy_min - _SCH_PAD + dy:.1f} .. {oy_max + _SCH_PAD + dy:.1f}")

    # ── Assemble ──────────────────────────────────────────────────────────
    # Paper sized to content so the drawing frame hugs the merged design
    paper = _compute_paper(
        x_max = max(sx_max, ox_max) + _SCH_PAD,
        y_max = max(sy_max, oy_max + dy) + _SCH_PAD,
    )
    print(f"  Paper size      : {paper}")

    merged_sch = assemble_sch(
        merge_uuid, merged_lib,
        box_same + repointed_same + box_offset + repointed_offset,
        paper=paper,
    )

    # ── Verify ────────────────────────────────────────────────────────────
    problems = verify(merged_sch, merge_uuid)
    print()
    if problems:
        print("  *** PROBLEMS ***")
        for p in problems:
            print(f"    {p}")
    else:
        print("  Instance path check : PASS — all → /{merge_uuid[:8]}…")

    if dry_run:
        print("\n  Dry-run: no files written.")
        return

    if problems:
        print("\n  Aborting write.")
        sys.exit(1)

    # ── Write ─────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "merge.kicad_sch"
    out_path.write_text(merged_sch, encoding="utf-8")
    print(f"\n  Written: {out_path}")
    print(f"  Open merge.kicad_pro in KiCad to verify.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge two normalised KiCad 8 schematics by Y-offsetting "
                    "the second project below the first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Typical workflow:
  python3 kicad_ref_normalise.py  TRA-1004E/  UTR11-1104E/
  python3 kicad_sch_merge.py  ForMerging/same-TRA-1004E/  \\
                              ForMerging/offset-UTR11-1104E/  \\
                              ForMerging/merge/
""",
    )
    parser.add_argument("same_dir",   type=Path, help="same-*   project folder")
    parser.add_argument("offset_dir", type=Path, help="offset-* project folder")
    parser.add_argument("out_dir",    type=Path, help="Output merge folder")
    parser.add_argument(
        "--gap", type=float, default=DEFAULT_GAP_MM, metavar="MM",
        help=f"Vertical gap between the two designs in mm "
             f"(default {DEFAULT_GAP_MM:.1f} = 20 × 2.54 grid units).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print extents and offset without writing anything.",
    )
    args = parser.parse_args()

    for d in (args.same_dir, args.offset_dir):
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory.", file=sys.stderr)
            sys.exit(1)

    merge_schematics(args.same_dir, args.offset_dir, args.out_dir,
                     gap=args.gap, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
