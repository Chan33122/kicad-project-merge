#!/usr/bin/env python3
"""
kicad_pcb_merge.py
==================
Merges two normalised KiCad 8 PCB files into one by placing the
offset-project board to the RIGHT of the same-project board.

Strategy
--------
  1. Find X_max of all content in the same-project PCB.
  2. Shift every ABSOLUTE coordinate in the offset-project PCB right by
       X_max_same + GAP   (default GAP = 25.4 mm = 10 × 2.54)
  3. Merge the net tables from both PCBs into a single numbered table,
     deduplicating by net name (shared power nets like GND, +3V3 etc.
     get one ID; project-specific nets each get a unique ID).
  4. Remap every net-ID reference throughout the offset-project content.
  5. Update (sheetfile "old.kicad_sch") → (sheetfile "merge.kicad_sch").
  6. Concatenate header + merged nets + all footprints + all copper/silk
     into merge.kicad_pcb.

Coordinate fields shifted in offset project (ABSOLUTE only)
------------------------------------------------------------
Top-level elements (segment, via, gr_line, gr_text, gr_rect, gr_circle,
gr_arc, gr_poly, zone, dimension):
    (start X Y)  (end X Y)  (at X Y [angle])
    (center X Y) (mid X Y)  (xy X Y)

Footprint top-level (at X Y [angle]) — the placement position.
Coordinates INSIDE a footprint block (pad (at …), property (at …)) are
RELATIVE to the footprint origin and are NOT shifted.

Net ID references remapped
--------------------------
    (net ID)           — segment, via, gr_line, gr_arc, zone …
    (net ID "name")    — pad inside footprint

Usage
-----
  python3 kicad_pcb_merge.py  same-TRA-1004E/  offset-UTR11-1104E/  merge/

  # Custom gap (mm):
  python3 kicad_pcb_merge.py  same/  offset/  merge/  --gap 30

  # Dry-run (print extents and shift, write nothing):
  python3 kicad_pcb_merge.py  same/  offset/  merge/  --dry-run
"""

import re
import sys
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_GAP_MM = 2.54 * 10   # 25.4 mm — 10 grid units


# ---------------------------------------------------------------------------
# Helpers: extents
# ---------------------------------------------------------------------------

def pcb_extent(text: str) -> tuple[float, float, float, float]:
    """
    Return (xmin, ymin, xmax, ymax) across all absolute coordinate tokens.
    Tokens: (start X Y) (end X Y) (at X Y) (center X Y) (mid X Y) (xy X Y)
    Falls back to (0,0,0,0) if none found.
    """
    coords = re.findall(
        r'\((?:start|end|at|center|mid)\s+([-\d.]+)\s+([-\d.]+)', text)
    xy = re.findall(r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)', text)
    all_c = coords + xy
    if not all_c:
        return 0.0, 0.0, 0.0, 0.0
    xs = [float(x) for x, _ in all_c]
    ys = [float(y) for _, y in all_c]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Helpers: net table
# ---------------------------------------------------------------------------

def extract_nets(text: str) -> dict[int, str]:
    """Return {net_id: net_name} from all (net ID "name") table entries."""
    return {int(i): n for i, n in re.findall(r'\(net\s+(\d+)\s+"([^"]*)"\)', text)}


def build_merged_nets(nets1: dict[int, str],
                      nets2: dict[int, str]) -> tuple[dict[str, int],
                                                       dict[int, int],
                                                       dict[int, int]]:
    """
    Build a merged net-name→new_id table and two remap dicts.

    Returns:
        merged   — {name: new_id}  (includes net 0 = unconnected)
        remap1   — {old_id: new_id} for PCB1
        remap2   — {old_id: new_id} for PCB2
    """
    merged: dict[str, int] = {'': 0}
    nid = 1
    for d in (nets1, nets2):
        for _, name in sorted(d.items()):
            if name == '':
                continue
            if name not in merged:
                merged[name] = nid
                nid += 1

    remap1 = {old: merged[name] for old, name in nets1.items()}
    remap2 = {old: merged[name] for old, name in nets2.items()}
    return merged, remap1, remap2


def net_table_text(merged: dict[str, int]) -> str:
    """Render the merged net table as PCB s-expression lines."""
    lines = []
    for name, nid in sorted(merged.items(), key=lambda kv: kv[1]):
        lines.append(f'\t(net {nid} "{name}")')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers: net ID remapping in text
# ---------------------------------------------------------------------------

def remap_net_ids(text: str, remap: dict[int, int]) -> str:
    """
    Replace every (net OLD_ID) and (net OLD_ID "name") occurrence
    in *text* using *remap*.  Net 0 (unconnected) passes through unchanged
    since it always maps to itself.
    """
    if not remap:
        return text

    def _repl_bare(m):
        old = int(m.group(1))
        new = remap.get(old, old)
        return f'(net {new})'

    def _repl_named(m):
        old  = int(m.group(1))
        name = m.group(2)
        new  = remap.get(old, old)
        return f'(net {new} "{name}")'

    # Named form first (more specific) to avoid double-replacing
    text = re.sub(r'\(net\s+(\d+)\s+"([^"]*)"\)', _repl_named, text)
    text = re.sub(r'\(net\s+(\d+)\)',              _repl_bare,  text)
    return text


# ---------------------------------------------------------------------------
# Helpers: shared-net renaming in PCB
# ---------------------------------------------------------------------------

_NET_PREFIX = "__"


def _rename_pcb_nets(pcb: str, shared: set[str]) -> str:
    """
    Add _NET_PREFIX to every shared net name in *pcb*.
    Covers:
      (net ID "name")      — top-level net table and pad references
      (net_name "name")    — zone fill references
    Only renames names present in *shared* that are not already prefixed.
    """
    if not shared:
        return pcb
    prefix = _NET_PREFIX

    def _repl_net(m):
        name = m.group(2)
        if name in shared and not name.startswith(prefix):
            return f'(net {m.group(1)} "{prefix}{name}")'
        return m.group(0)

    def _repl_netname(m):
        name = m.group(1)
        if name in shared and not name.startswith(prefix):
            return f'(net_name "{prefix}{name}")'
        return m.group(0)

    pcb = re.sub(r'\(net\s+(\d+)\s+"([^"]+)"\)', _repl_net,     pcb)
    pcb = re.sub(r'\(net_name\s+"([^"]+)"\)',     _repl_netname, pcb)
    return pcb


# ---------------------------------------------------------------------------
# Helpers: coordinate shifting
# ---------------------------------------------------------------------------

# Patterns — X is first numeric, Y is second.
_START_END_RE = re.compile(
    r'(\((?:start|end|center|mid)\s+)([-\d.]+)(\s+)([-\d.]+)')
_XY_RE = re.compile(
    r'(\(xy\s+)([-\d.]+)(\s+)([-\d.]+)(\))')


def _shift_start_end(text: str, dx: float) -> str:
    def _r(m):
        x_new = round(float(m.group(2)) + dx, 6)
        return m.group(1) + str(x_new) + m.group(3) + m.group(4)
    return _START_END_RE.sub(_r, text)


def _shift_xy(text: str, dx: float) -> str:
    def _r(m):
        x_new = round(float(m.group(2)) + dx, 6)
        return m.group(1) + str(x_new) + m.group(3) + m.group(4) + m.group(5)
    return _XY_RE.sub(_r, text)


# (at X Y [angle]) — X shift only, Y and angle preserved
_AT_RE = re.compile(r'(\(at\s+)([-\d.]+)((?:\s+[-\d.]+)+)')


def _shift_at(text: str, dx: float) -> str:
    def _r(m):
        x_new = round(float(m.group(2)) + dx, 6)
        return m.group(1) + str(x_new) + m.group(3)
    return _AT_RE.sub(_r, text)


def shift_element(elem: str, dx: float) -> str:
    """Shift ALL coordinate tokens in a top-level PCB element."""
    elem = _shift_start_end(elem, dx)
    elem = _shift_at(elem, dx)
    elem = _shift_xy(elem, dx)
    return elem


def shift_footprint(fp: str, dx: float) -> str:
    """
    Shift only the footprint's OWN (at X Y [angle]) placement token.
    Everything inside the footprint block (pad positions, property positions)
    is RELATIVE to the footprint origin and must not be touched.
    """
    # The footprint's placement (at) is the first (at ...) in the block,
    # before any nested sub-block. Find it and shift only that one.
    m = re.search(r'\(at\s+([-\d.]+)((?:\s+[-\d.]+)+)\)', fp)
    if not m:
        return fp
    x_new = round(float(m.group(1)) + dx, 6)
    replacement = f'(at {x_new}{m.group(2)})'
    return fp[:m.start()] + replacement + fp[m.end():]


# ---------------------------------------------------------------------------
# Helpers: element extraction
# ---------------------------------------------------------------------------

_MOVABLE_TOKENS = frozenset({
    'segment', 'arc', 'gr_line', 'gr_arc', 'gr_text', 'gr_rect',
    'gr_circle', 'gr_poly', 'via', 'zone', 'dimension', 'gr_curve',
})

_HEADER_TOKENS = frozenset({
    'version', 'generator', 'generator_version',
    'general', 'paper', 'layers', 'setup', 'net',
})


def extract_elements(pcb: str) -> dict[str, list[str]]:
    """
    Return all top-level PCB elements grouped by token type.
    Keys: 'header' (general/paper/layers/setup),
          'nets', 'footprints', 'movable' (everything else)
    """
    result: dict[str, list[str]] = {
        'header': [], 'nets': [], 'footprints': [], 'movable': []
    }

    for m in re.finditer(r'\n\t\((\w+)\s', pcb):
        token = m.group(1)
        if token in ('version', 'generator', 'generator_version'):
            continue   # part of the file header line — not separate blocks

        start = m.start() + 1
        depth = 0
        for i in range(start, len(pcb)):
            if pcb[i] == '(':
                depth += 1
            elif pcb[i] == ')':
                depth -= 1
                if depth == 0:
                    block = pcb[start:i + 1]
                    if token in ('general', 'paper', 'layers', 'setup'):
                        result['header'].append(block)
                    elif token == 'net':
                        result['nets'].append(block)
                    elif token == 'footprint':
                        result['footprints'].append(block)
                    elif token in _MOVABLE_TOKENS:
                        result['movable'].append(block)
                    break

    return result


# ---------------------------------------------------------------------------
# Helpers: annotation boxes
# ---------------------------------------------------------------------------

_PCB_PAD = 5.0   # mm padding around content


def _pcb_box(x1: float, y1: float, x2: float, y2: float,
             label: str, layer: str = "F.SilkS") -> list[str]:
    """
    Return [gr_rect_str, gr_text_str] for a labelled dashed box
    on the KiCad 8 PCB comment layer.
    """
    import uuid as _uuid
    ru, tu = str(_uuid.uuid4()), str(_uuid.uuid4())
    rect = (
        f'(gr_rect\n'
        f'\t\t(start {x1:.3f} {y1:.3f})\n'
        f'\t\t(end {x2:.3f} {y2:.3f})\n'
        f'\t\t(stroke\n'
        f'\t\t\t(width 0.15)\n'
        f'\t\t\t(type dash)\n'
        f'\t\t)\n'
        f'\t\t(fill none)\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{ru}")\n'
        f'\t)'
    )
    text = (
        f'(gr_text "{label}"\n'
        f'\t\t(at {x1 + 1:.3f} {y1 - 2.5:.3f})\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{tu}")\n'
        f'\t\t(effects\n'
        f'\t\t\t(font\n'
        f'\t\t\t\t(size 2 2)\n'
        f'\t\t\t\t(thickness 0.25)\n'
        f'\t\t\t\t(bold yes)\n'
        f'\t\t\t)\n'
        f'\t\t\t(justify left)\n'
        f'\t\t)\n'
        f'\t)'
    )
    return [rect, text]


# ---------------------------------------------------------------------------
# Helpers: sheetfile update
# ---------------------------------------------------------------------------

def update_sheetfile(text: str, old_name: str, new_name: str) -> str:
    if old_name:
        text = text.replace(f'(sheetfile "{old_name}")',
                            f'(sheetfile "{new_name}")')
    return text


def get_sheetfile(pcb: str) -> str:
    """Return the (sheetfile "...") value used in this PCB, or ''."""
    m = re.search(r'\(sheetfile\s+"([^"]+)"\)', pcb)
    return m.group(1) if m else ''


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

def assemble_pcb(header_blocks: list[str],
                 net_table: str,
                 footprints: list[str],
                 movable: list[str]) -> str:
    header = "\n".join(f"\t{b}" for b in header_blocks)
    fps    = "\n".join(f"\t{f}" for f in footprints)
    movs   = "\n".join(f"\t{m}" for m in movable)
    parts  = [
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (generator_version "8.0")',
        header,
        net_table,
        fps,
        movs,
        ')',
    ]
    return "\n".join(p for p in parts if p.strip()) + "\n"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(merged_pcb: str, merged_nets: dict[str, int]) -> list[str]:
    problems = []

    # All sheetfiles should be merge.kicad_sch
    sf = set(re.findall(r'\(sheetfile\s+"([^"]+)"\)', merged_pcb))
    wrong = sf - {'merge.kicad_sch'}
    if wrong:
        problems.append(f"Sheetfile(s) not updated: {wrong}")

    # No net ID higher than the merged table max
    max_id = max(merged_nets.values()) if merged_nets else 0
    bare_ids  = [int(m) for m in re.findall(r'\(net\s+(\d+)\)', merged_pcb)]
    named_ids = [int(m) for m in re.findall(r'\(net\s+(\d+)\s+"', merged_pcb)]
    over = [i for i in bare_ids + named_ids if i > max_id]
    if over:
        problems.append(
            f"{len(over)} net ID(s) exceed merged table max {max_id}: "
            f"{sorted(set(over))[:5]}")

    return problems


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def merge_pcbs(same_dir: Path, offset_dir: Path, out_dir: Path,
               gap: float = DEFAULT_GAP_MM,
               dry_run: bool = False) -> None:

    def find_pcb(d: Path) -> Path:
        files = list(d.glob("*.kicad_pcb"))
        if len(files) != 1:
            raise FileNotFoundError(
                f"Expected exactly one .kicad_pcb in {d}, found {len(files)}")
        return files[0]

    same_path   = find_pcb(same_dir)
    offset_path = find_pcb(offset_dir)

    same_pcb   = same_path.read_text(encoding="utf-8")
    offset_pcb = offset_path.read_text(encoding="utf-8")

    # ── Extents ──────────────────────────────────────────────────────────
    sx_min, sy_min, sx_max, sy_max = pcb_extent(same_pcb)
    ox_min, oy_min, ox_max, oy_max = pcb_extent(offset_pcb)
    dx = round(sx_max + gap - ox_min, 6)

    print(f"\n{'='*60}")
    print(f"  same   : {same_path}")
    print(f"  offset : {offset_path}")
    print(f"\n  same   extent : X {sx_min:.1f}..{sx_max:.1f}"
          f"  Y {sy_min:.1f}..{sy_max:.1f} mm")
    print(f"  offset extent : X {ox_min:.1f}..{ox_max:.1f}"
          f"  Y {oy_min:.1f}..{oy_max:.1f} mm")
    print(f"  gap           : {gap:.3f} mm")
    print(f"  X shift       : {dx:.3f} mm"
          f"  (offset board starts at X ≈ {round(sx_max + gap, 3):.1f})")

    # ── Net tables ───────────────────────────────────────────────────────
    nets1 = extract_nets(same_pcb)
    nets2 = extract_nets(offset_pcb)

    # Rename shared net names in offset PCB with __ prefix
    shared_nets = {n for n in nets1.values() if n} & {n for n in nets2.values() if n}
    if shared_nets:
        offset_pcb = _rename_pcb_nets(offset_pcb, shared_nets)
        # Rebuild nets2 after rename so merged table is consistent
        nets2 = extract_nets(offset_pcb)
        print(f"\n  Shared nets renamed with '{_NET_PREFIX}' prefix"
              f" in offset project: {len(shared_nets)}")
        print(f"    sample: {sorted(shared_nets)[:6]}")

    merged_nets, remap1, remap2 = build_merged_nets(nets1, nets2)
    net_text = net_table_text(merged_nets)

    needs_remap2 = sum(1 for old, new in remap2.items() if old != new)
    print(f"\n  Nets : same={len(nets1)}  offset={len(nets2)}"
          f"  merged={len(merged_nets)}  "
          f"offset IDs remapped={needs_remap2}")

    # ── Extract elements ─────────────────────────────────────────────────
    e1 = extract_elements(same_pcb)
    e2 = extract_elements(offset_pcb)
    print(f"  Footprints : same={len(e1['footprints'])}"
          f"  offset={len(e2['footprints'])}"
          f"  total={len(e1['footprints'])+len(e2['footprints'])}")
    print(f"  Movable    : same={len(e1['movable'])}"
          f"  offset={len(e2['movable'])}"
          f"  total={len(e1['movable'])+len(e2['movable'])}")

    # ── Sheetfile names ──────────────────────────────────────────────────
    sf1 = get_sheetfile(same_pcb)
    sf2 = get_sheetfile(offset_pcb)

    # ── Process same-project (net remap only, no coordinate shift) ───────
    fps1 = [
        update_sheetfile(remap_net_ids(f, remap1), sf1, "merge.kicad_sch")
        for f in e1['footprints']
    ]
    mov1 = [remap_net_ids(m, remap1) for m in e1['movable']]

    # ── Process offset-project (shift + net remap) ───────────────────────
    fps2 = [
        update_sheetfile(
            remap_net_ids(shift_footprint(f, dx), remap2),
            sf2, "merge.kicad_sch"
        )
        for f in e2['footprints']
    ]
    mov2 = [
        remap_net_ids(shift_element(m, dx), remap2)
        for m in e2['movable']
    ]

    # ── Annotation boxes ─────────────────────────────────────────────────
    same_label   = same_path.stem
    offset_label = offset_path.stem

    box_same = _pcb_box(
        sx_min - _PCB_PAD, sy_min - _PCB_PAD,
        sx_max + _PCB_PAD, sy_max + _PCB_PAD,
        same_label,
    )
    # Offset box coordinates are already in the shifted frame
    box_offset = _pcb_box(
        ox_min - _PCB_PAD + dx, oy_min - _PCB_PAD,
        ox_max + _PCB_PAD + dx, oy_max + _PCB_PAD,
        offset_label,
    )

    print(f"\n  Boxes added:")
    print(f"    [{same_label}]   X {sx_min - _PCB_PAD:.1f} .. {sx_max + _PCB_PAD:.1f}")
    print(f"    [{offset_label}] X {ox_min - _PCB_PAD + dx:.1f} .. {ox_max + _PCB_PAD + dx:.1f}")

    # ── Paper size — replace header (paper ...) with custom fit ──────────
    import math
    margin = 20.0
    pcb_x_max = max(sx_max + _PCB_PAD, ox_max + _PCB_PAD + dx)
    pcb_y_max = max(sy_max + _PCB_PAD, oy_max + _PCB_PAD)
    pw = math.ceil(max(pcb_x_max + margin, 297))
    ph = math.ceil(max(pcb_y_max + margin, 210))
    paper_tok = f'(paper "User" {pw} {ph})'
    # Replace the (paper ...) block in the header list — strip whitespace when matching
    header_blocks = [
        paper_tok if b.strip().startswith('(paper') else b
        for b in e1['header']
    ]
    print(f"  Paper size      : User {pw} x {ph} mm")

    # ── Assemble ─────────────────────────────────────────────────────────
    merged_pcb = assemble_pcb(
        header_blocks,
        net_text,
        fps1 + fps2,
        mov1 + mov2 + box_same + box_offset,
    )

    # ── Verify ───────────────────────────────────────────────────────────
    problems = verify(merged_pcb, merged_nets)
    print()
    if problems:
        print("  *** PROBLEMS ***")
        for p in problems:
            print(f"    {p}")
    else:
        total_fps = len(fps1) + len(fps2)
        print(f"  Sheetfile check : PASS — all → merge.kicad_sch ✓")
        print(f"  Net ID check    : PASS — all IDs within merged table ✓")
        print(f"  Total footprints: {total_fps}")

    if dry_run:
        print("\n  Dry-run: no files written.")
        return

    if problems:
        print("\n  Aborting write.")
        sys.exit(1)

    # ── Write ─────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "merge.kicad_pcb"
    out_path.write_text(merged_pcb, encoding="utf-8")
    print(f"\n  Written: {out_path}")
    print(f"  Open merge.kicad_pro in KiCad to verify.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge two normalised KiCad 8 PCB files by placing the "
                    "offset-project board to the right of the same-project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Typical workflow:
  python3 kicad_ref_normalise.py  TRA-1004E/  UTR11-1104E/
  python3 kicad_sch_merge.py  ForMerging/same-TRA-1004E/ \\
                              ForMerging/offset-UTR11-1104E/ \\
                              ForMerging/merge/
  python3 kicad_pcb_merge.py  ForMerging/same-TRA-1004E/ \\
                              ForMerging/offset-UTR11-1104E/ \\
                              ForMerging/merge/
""",
    )
    parser.add_argument("same_dir",   type=Path, help="same-*   project folder")
    parser.add_argument("offset_dir", type=Path, help="offset-* project folder")
    parser.add_argument("out_dir",    type=Path, help="Output merge folder")
    parser.add_argument(
        "--gap", type=float, default=DEFAULT_GAP_MM, metavar="MM",
        help=f"Horizontal gap between the boards in mm "
             f"(default {DEFAULT_GAP_MM:.1f} = 10 × 2.54).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print extents and shift without writing anything.",
    )
    args = parser.parse_args()

    for d in (args.same_dir, args.offset_dir):
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory.", file=sys.stderr)
            sys.exit(1)

    merge_pcbs(args.same_dir, args.offset_dir, args.out_dir,
               gap=args.gap, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
