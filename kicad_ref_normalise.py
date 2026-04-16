#!/usr/bin/env python3
"""
kicad_ref_normalise.py  —  Part 1 of the KiCad merge tool
==========================================================
Reads a KiCad project folder (one .kicad_sch + one .kicad_pcb),
checks that every reference present in the schematic also appears in
the PCB and vice-versa, then compacts the reference numbers into a
dense 1..N range using a safe two-pass rename so that a reference that
already exists in the target numbering (e.g. R1 → R1) is never
accidentally clobbered.

Offsets are per-prefix, so R, C, U etc. are each renumbered
independently.  This lets project 2's R-series start right after
project 1's highest R, and similarly for every other prefix.

Workflow
--------
  # Step 1 — dry-run project 1 (output_dir omitted → dry-run implied):
  python3 kicad_ref_normalise.py  project1/

  # Output prints:
  #   Per-prefix maxima (pass to project2 as --prefix-offset):
  #     --prefix-offset C:7,R:13,U:2

  # Step 2 — write project 1 with clean 1..N numbering:
  python3 kicad_ref_normalise.py  project1/  project1_out/

  # Step 3 — write project 2 starting above project 1's maxima:
  python3 kicad_ref_normalise.py  project2/  project2_out/  --prefix-offset C:7,R:13,U:2

  # Explicit dry-run on project 2 to preview before writing:
  python3 kicad_ref_normalise.py  project2/  project2_out/  --prefix-offset C:7,R:13,U:2  --dry-run

The script never modifies the source folder.

Output
------
  <out_dir>/  —  copies of .kicad_sch and .kicad_pcb with renumbered refs
  <out_dir>/ref_map.json  —  {"R11":"R1", "R13":"R2", ...}
  <out_dir>/consistency_report.txt  —  SCH vs PCB diff (should be empty)
"""

import re
import sys
import json
import shutil
import argparse
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# KiCad 6/7/8 schematic — reference is always double-quoted:
#   (property "Reference" "R11" ...)
# group(1): everything up to and including the opening "
# group(2): the reference value
# group(3): the closing "
SCH_REF_RE = re.compile(
    r'(\(property\s+"Reference"\s+")([A-Za-z_#\$][A-Za-z0-9_]*)(")',
)

# KiCad 6/7/8 schematic — instance binding (second, separate ref location):
#   (instances
#     (project "name"
#       (path "/uuid..."
#         (reference "R11")    ← THIS is the live ref KiCad uses for PCB sync
#         (unit 1)
#       )
#     )
#   )
# This token MUST be updated alongside SCH_REF_RE, or KiCad sees the property
# label and the instance binding disagreeing — the inconsistency the user observed.
# group(1): (reference "
# group(2): the reference value
# group(3): the closing "
SCH_INST_RE = re.compile(
    r'(\(reference\s+")([A-Za-z_#\$][A-Za-z0-9_]*)(")',
)

# KiCad 6/7/8 PCB — two forms:
#   (fp_text reference "R11" ...)     ← KiCad 6/7, always quoted
#   (fp_text reference R11 ...)       ← older pcbnew / Eagle imports, no quotes
#   (property "Reference" "R11" ...)  ← KiCad 8 footprint property, always quoted
#
# The opening group uses "? so it matches with or without the opening quote.
# group(3) is then the matching closing delimiter: " for quoted, \s for unquoted.
# Reconstruction: g1 + new_ref + g3 preserves the original quoting style exactly.
PCB_REF_RE = re.compile(
    r'(\((?:fp_text\s+reference|property\s+"Reference")\s+"?)([A-Za-z_#\$][A-Za-z0-9_]*)("|\s)',
)

# A "real" component reference has a letter prefix + digits, e.g. R11, C3, U1
# Power symbols / NetTie / unplaced parts use # prefix — we keep them unchanged.
REAL_REF_RE = re.compile(r'^([A-Za-z]+)(\d+)$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_project_files(folder: Path):
    """Return (sch_path, pcb_path) or raise if not found / ambiguous."""
    schs = list(folder.glob("*.kicad_sch"))
    pcbs = list(folder.glob("*.kicad_pcb"))
    if len(schs) != 1:
        raise FileNotFoundError(f"Expected exactly one .kicad_sch in {folder}, found {len(schs)}")
    if len(pcbs) != 1:
        raise FileNotFoundError(f"Expected exactly one .kicad_pcb in {folder}, found {len(pcbs)}")
    return schs[0], pcbs[0]


def strip_lib_symbols(sch_text: str) -> str:
    """
    Remove the (lib_symbols ...) block from a KiCad schematic text.

    The lib_symbols block contains symbol *template* definitions, each of
    which carries a (property "Reference" "R") with a bare prefix letter
    (or occasionally an example ref like "TP8" from a custom symbol).
    These are not placed instances and must not be parsed for refs or
    remapped.

    Boundary detection uses bracket counting — the block starts at the
    first (lib_symbols token and ends when its opening paren is closed.
    The text before and after the block is returned joined, preserving
    all placed-instance content unchanged.
    """
    marker = '(lib_symbols'
    start = sch_text.find(marker)
    if start == -1:
        return sch_text          # no lib_symbols block (shouldn't happen, but safe)

    depth = 0
    end = start
    for i in range(start, len(sch_text)):
        if sch_text[i] == '(':
            depth += 1
        elif sch_text[i] == ')':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    return sch_text[:start] + sch_text[end:]


def extract_refs(text: str, pattern: re.Pattern) -> set[str]:
    """Return the set of all reference strings matched by *pattern*."""
    return {m.group(2) for m in pattern.finditer(text)}


def split_ref(ref: str):
    """Split 'R11' → ('R', 11).  Returns None if not a real component ref."""
    m = REAL_REF_RE.match(ref)
    if m:
        return m.group(1), int(m.group(2))
    return None


def build_compact_map(refs: set[str],
                      offsets: dict[str, int] | None = None) -> tuple[dict, dict]:
    """
    Build old→new mapping that compacts references into a dense
    <prefix>(offset+1) .. <prefix>(offset+N) range.

    *offsets* is a per-prefix dict, e.g. {"R": 13, "C": 7}.
    A prefix absent from the dict gets offset 0 (starts at 1).

    The two-pass trick
    ------------------
    Suppose the old set is {R1, R11, R13} and offset for R = 0.
    Target: R1→R1, R11→R2, R13→R3  (sorted by old number).

    The collision happens when an *old* name equals a *new* name that
    belongs to a *different* component, e.g. old={R1,R3}, target R3→R1,
    R1→R2:  a single-pass rename of R3→R1 would clobber the existing R1.

    Fix: rename everything to a unique temporary name first
    (R1→R_tmp_1, R3→R_tmp_3), then rename all R_tmp_X to their finals.
    The tmp namespace never appears in real KiCad files.
    """
    if offsets is None:
        offsets = {}

    by_prefix: dict[str, list] = defaultdict(list)
    for ref in refs:
        parts = split_ref(ref)
        if parts:
            prefix, num = parts
            by_prefix[prefix].append((num, ref))

    old_to_tmp: dict[str, str] = {}    # R11      → R_tmp_11
    tmp_to_new: dict[str, str] = {}    # R_tmp_11 → R1

    for prefix, items in by_prefix.items():
        items.sort()   # sort by original number → deterministic compact order
        start = 1 + offsets.get(prefix, 0)
        for rank, (_, old_ref) in enumerate(items, start=start):
            tmp = f"{prefix}_tmp_{old_ref[len(prefix):]}"   # R_tmp_11
            new_ref = f"{prefix}{rank}"
            old_to_tmp[old_ref] = tmp
            tmp_to_new[tmp]     = new_ref

    return old_to_tmp, tmp_to_new


def apply_map(text: str, mapping: dict[str, str], pattern: re.Pattern) -> str:
    """Replace every reference in *text* that appears in *mapping*."""
    def replacer(m):
        ref = m.group(2)
        return m.group(1) + mapping.get(ref, ref) + m.group(3)
    return pattern.sub(replacer, text)


def two_pass_rename(text: str,
                    old_to_tmp: dict[str, str],
                    tmp_to_new: dict[str, str],
                    pattern: re.Pattern) -> str:
    """Apply old→tmp then tmp→new to avoid mid-flight collisions."""
    text = apply_map(text, old_to_tmp, pattern)
    text = apply_map(text, tmp_to_new, pattern)
    return text


def consistency_check(sch_refs: set[str], pcb_refs: set[str]) -> tuple[set, set]:
    """
    Return (sch_only, pcb_only) — refs present in one file but not the other.
    Power symbols / unconnected stubs (#PWR, #FLG) are excluded.
    """
    def real_only(s):
        return {r for r in s if split_ref(r) is not None}
    sr = real_only(sch_refs)
    pr = real_only(pcb_refs)
    return sr - pr, pr - sr


def max_ref_number(refs: set[str]) -> dict[str, int]:
    """Return {prefix: max_number} across all real refs."""
    result = defaultdict(int)
    for ref in refs:
        parts = split_ref(ref)
        if parts:
            prefix, num = parts
            result[prefix] = max(result[prefix], num)
    return result


def min_ref_number(refs: set[str]) -> dict[str, int]:
    """Return {prefix: min_number} across all real refs."""
    result: dict[str, int] = {}
    for ref in refs:
        parts = split_ref(ref)
        if parts:
            prefix, num = parts
            if prefix not in result or num < result[prefix]:
                result[prefix] = num
    return result


# ---------------------------------------------------------------------------
# Annotation text helpers
# ---------------------------------------------------------------------------

def _bbox(text: str) -> tuple[float, float, float, float]:
    """
    Return (xmin, ymin, xmax, ymax) from all (at X Y) tokens in *text*.
    Falls back to (0, 0, 100, 100) if none found.
    """
    coords = re.findall(r'\(at\s+([-\d.]+)\s+([-\d.]+)', text)
    if not coords:
        return 0.0, 0.0, 100.0, 100.0
    xs = [float(x) for x, _ in coords]
    ys = [float(y) for _, y in coords]
    return min(xs), min(ys), max(xs), max(ys)


def _new_uuid() -> str:
    import uuid as _uuid
    return str(_uuid.uuid4())


def _format_ref_range(new_refs: set[str]) -> str:
    """
    Build a compact per-prefix range summary, e.g.
      C1..C34  FID1..FID8  R1..R7  TP1..TP196
    """
    mins = min_ref_number(new_refs)
    maxs = max_ref_number(new_refs)
    return _format_ref_range_from_min_max(mins, maxs)


def _format_ref_range_from_min_max(mins: dict[str, int],
                                   maxs: dict[str, int]) -> str:
    """Build range string from pre-computed min/max dicts."""
    parts = []
    for p in sorted(mins):
        lo, hi = mins[p], maxs.get(p, mins[p])
        if lo == hi:
            parts.append(f"{p}{lo}")
        else:
            parts.append(f"{p}{lo}..{p}{hi}")
    return "  ".join(parts)


def _format_offset_summary(offsets: dict[str, int]) -> str:
    """
    Human-readable offset summary, e.g.
      C+34  FID+8  R+7  TP+196
    Prefixes with offset 0 are omitted.
    """
    parts = [f"{p}+{v}" for p, v in sorted(offsets.items()) if v > 0]
    return "  ".join(parts) if parts else "none"


# SCH text element template (KiCad 6/7/8 s-expression)
_SCH_TEXT_TMPL = (
    '\t(text "{content}"\n'
    '\t\t(exclude_from_sim no)\n'
    '\t\t(at {x} {y} 0)\n'
    '\t\t(effects\n'
    '\t\t\t(font\n'
    '\t\t\t\t(size 2 2)\n'
    '\t\t\t\t(thickness 0.4)\n'
    '\t\t\t\t(bold yes)\n'
    '\t\t\t)\n'
    '\t\t\t(justify left)\n'
    '\t\t)\n'
    '\t\t(uuid "{uuid}")\n'
    '\t)'
)

# PCB graphical text template — placed on F.SilkS
_PCB_TEXT_TMPL = (
    '\t(gr_text "{content}"\n'
    '\t\t(at {x} {y})\n'
    '\t\t(layer "F.SilkS")\n'
    '\t\t(uuid "{uuid}")\n'
    '\t\t(effects\n'
    '\t\t\t(font\n'
    '\t\t\t\t(size 1.5 1.5)\n'
    '\t\t\t\t(thickness 0.3)\n'
    '\t\t\t\t(bold yes)\n'
    '\t\t\t)\n'
    '\t\t\t(justify left)\n'
    '\t\t)\n'
    '\t)'
)


def annotate_sch(sch_text: str,
                 lines: list[str]) -> str:
    """
    Insert one SCH text element per line into sch_text.
    Lines are stacked vertically below the schematic content.
    """
    _, _, _, ymax = _bbox(sch_text)
    y_start = ymax + 15.0   # 15 mm below the lowest element
    line_step = 6.0          # 6 mm between lines
    x = 10.0

    blocks = []
    for i, line in enumerate(lines):
        blocks.append(_SCH_TEXT_TMPL.format(
            content=line.replace('"', "'"),
            x=x,
            y=round(y_start + i * line_step, 3),
            uuid=_new_uuid(),
        ))

    insert_pos = sch_text.rfind('\n)')
    return sch_text[:insert_pos] + '\n' + '\n'.join(blocks) + sch_text[insert_pos:]


def annotate_pcb(pcb_text: str,
                 lines: list[str]) -> str:
    """
    Insert one PCB gr_text element per line into pcb_text on F.SilkS.
    Lines are stacked to the left of the board content.
    """
    xmin, _, _, ymax = _bbox(pcb_text)
    x = round(xmin - 5.0, 3)   # 5 mm to the left of the leftmost element
    y_start = round(ymax, 3)    # start at the bottom of the board
    line_step = 4.0              # 4 mm between lines (1.5 mm text + gap)

    blocks = []
    for i, line in enumerate(lines):
        blocks.append(_PCB_TEXT_TMPL.format(
            content=line.replace('"', "'"),
            x=x,
            y=round(y_start + i * line_step, 3),
            uuid=_new_uuid(),
        ))

    insert_pos = pcb_text.rfind('\n)')
    return pcb_text[:insert_pos] + '\n' + '\n'.join(blocks) + pcb_text[insert_pos:]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_project(folder: Path,
                    out_dir: Path | None,
                    offsets: dict[str, int] | None = None,
                    dry_run: bool = False,
                    p1_maxima: dict[str, int] | None = None) -> dict[str, int]:
    """
    Normalise one project.

    *offsets*    per-prefix start offsets, e.g. {"R": 13, "C": 7}.
                 Absent prefixes start at 1.
    *out_dir*    None → dry-run implied regardless of *dry_run* flag.
    *p1_maxima*  Maxima from the already-written project1 output, used to
                 include the project1 ref range in the offset project's
                 annotation text.

    Returns the per-prefix maxima of the NEW ref numbers, ready to be
    passed as *offsets* to the next project.
    """
    if out_dir is None:
        dry_run = True

    sch_path, pcb_path = find_project_files(folder)

    sch_text = sch_path.read_text(encoding="utf-8")
    pcb_text = pcb_path.read_text(encoding="utf-8")

    # Strip the lib_symbols block before extracting refs.
    # lib_symbols contains symbol *template* definitions whose
    # (property "Reference" ...) values are bare prefix letters ("R", "C")
    # or example refs from custom symbols ("TP8").  These are not placed
    # instances and must not influence the ref set or consistency check.
    # The remap is still applied to the FULL sch_text (including lib_symbols)
    # so that template refs stay in sync — KiCad reloads symbols from the
    # embedded lib and would be confused if template refs were left stale.
    sch_text_for_refs = strip_lib_symbols(sch_text)

    # SCH stores refs in TWO independent locations:
    #   SCH_REF_RE  — (property "Reference" "R11")  visual label on symbol body
    #   SCH_INST_RE — (reference "R11") inside each symbol's (instances ...) block
    #
    # These two sets are structurally identical (same prefixes, same counts) but
    # may carry DIFFERENT numbers if a prior partial run updated one and not the
    # other — exactly the inconsistency the user observed.
    #
    # Strategy: treat the two sets as INDEPENDENT sources.
    # (property "Reference") matches PCB and is authoritative for consistency check.
    # Build a separate compact map for each, both using the same offsets, so both
    # token forms end up with the same final values after renaming.
    sch_prop_refs = extract_refs(sch_text_for_refs, SCH_REF_RE)
    sch_inst_refs = extract_refs(sch_text_for_refs, SCH_INST_RE)
    pcb_refs      = extract_refs(pcb_text, PCB_REF_RE)

    # --- Consistency check: prop refs vs PCB (authoritative pair) ------------
    sch_only, pcb_only = consistency_check(sch_prop_refs, pcb_refs)

    print(f"\n{'='*60}")
    print(f"Project : {folder}")
    print(f"  SCH   : {sch_path.name}  ({len(sch_prop_refs)} prop refs, {len(sch_inst_refs)} inst refs)")
    print(f"  PCB   : {pcb_path.name}  ({len(pcb_refs)} refs)")
    if offsets:
        print(f"  Offsets: " + ", ".join(f"{p}:{v}" for p, v in sorted(offsets.items())))
    else:
        print(f"  Offsets: none (numbering starts at 1 for each prefix)")

    # Warn if prop and inst already disagree in the INPUT (prior partial run)
    inst_only_in_input = sch_inst_refs - sch_prop_refs
    prop_only_in_input = sch_prop_refs - sch_inst_refs
    if inst_only_in_input or prop_only_in_input:
        print(f"\n  NOTE: SCH property/instances refs are already split — repairing.")
        if prop_only_in_input:
            print(f"  prop-only (stale inst): {sorted(prop_only_in_input)[:10]}"
                  + (" …" if len(prop_only_in_input) > 10 else ""))
        if inst_only_in_input:
            print(f"  inst-only (stale prop): {sorted(inst_only_in_input)[:10]}"
                  + (" …" if len(inst_only_in_input) > 10 else ""))

    if sch_only or pcb_only:
        print("\n  *** CONSISTENCY PROBLEMS ***")
        if sch_only:
            print(f"  In SCH but NOT PCB : {sorted(sch_only)}")
        if pcb_only:
            print(f"  In PCB but NOT SCH : {sorted(pcb_only)}")
        print("  (Resolve these before merging.  Processing continues.)")
    else:
        print("  Consistency: OK — SCH and PCB refs match exactly.")

    # --- Build independent remaps for each token form -----------------------
    # prop and PCB share the same ref set → one shared map covers both
    prop_pcb_refs = sch_prop_refs | pcb_refs
    old_to_tmp_prop, tmp_to_new_prop = build_compact_map(prop_pcb_refs, offsets=offsets)

    # inst may have different numbers → its own map, same offsets → same finals
    old_to_tmp_inst, tmp_to_new_inst = build_compact_map(sch_inst_refs, offsets=offsets)

    # Human-readable old→new map (from prop/PCB map — the authoritative one)
    combined_map = {old: tmp_to_new_prop[tmp] for old, tmp in old_to_tmp_prop.items()}

    print(f"\n  Reference remap ({len(combined_map)} components):")
    by_prefix_disp: dict[str, list] = defaultdict(list)
    for old, new in sorted(combined_map.items()):
        parts = split_ref(old)
        if parts:
            by_prefix_disp[parts[0]].append((old, new))
    for prefix in sorted(by_prefix_disp):
        pairs = by_prefix_disp[prefix]
        print(f"    {prefix}: " + ", ".join(f"{o}→{n}" for o, n in pairs[:8])
              + (" …" if len(pairs) > 8 else ""))

    # Compute per-prefix maxima of the NEW numbering
    new_maxima = max_ref_number(set(combined_map.values()))

    if dry_run:
        print("\n  Dry-run: no files written.")
        return new_maxima

    # --- Write output -------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)

    # Apply prop/PCB map to SCH_REF_RE and PCB_REF_RE (same map — same ref set)
    # Apply inst map  to SCH_INST_RE (independent map — repairs any prior split)
    new_sch_text = two_pass_rename(sch_text,     old_to_tmp_prop, tmp_to_new_prop, SCH_REF_RE)
    new_sch_text = two_pass_rename(new_sch_text, old_to_tmp_inst, tmp_to_new_inst, SCH_INST_RE)
    new_pcb_text = two_pass_rename(pcb_text,     old_to_tmp_prop, tmp_to_new_prop, PCB_REF_RE)

    # --- Build annotation lines -------------------------------------------
    new_refs = set(combined_map.values())
    range_str   = _format_ref_range(new_refs)
    offset_str  = _format_offset_summary(offsets)
    project_name = folder.name

    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if offsets:
        # Build project1 range string for cross-reference
        p1_range_str = ""
        if p1_maxima:
            p1_mins = {p: 1 for p in p1_maxima}
            p1_range_str = _format_ref_range_from_min_max(p1_mins, p1_maxima)

        sch_labels = [
            f"[offset] {project_name}  [{ts}]",
            f"Offsets applied: {offset_str}",
            f"This project refs: {range_str}",
        ]
        if p1_range_str:
            sch_labels.append(f"Project-1 refs:   {p1_range_str}")

        pcb_labels = [
            f"[offset] {project_name}  [{ts}]",
            f"Offsets: {offset_str}",
            f"This refs: {range_str}",
        ]
        if p1_range_str:
            pcb_labels.append(f"P1 refs: {p1_range_str}")
    else:
        sch_labels = [
            f"[same] {project_name}  [{ts}]",
            f"Refs (compacted 1..N): {range_str}",
        ]
        pcb_labels = [
            f"[same] {project_name}  [{ts}]",
            f"Refs: {range_str}",
        ]

    new_sch_text = annotate_sch(new_sch_text, sch_labels)
    new_pcb_text = annotate_pcb(new_pcb_text, pcb_labels)

    out_sch = out_dir / sch_path.name
    out_pcb = out_dir / pcb_path.name
    out_sch.write_text(new_sch_text, encoding="utf-8")
    out_pcb.write_text(new_pcb_text, encoding="utf-8")

    # Copy any other project files (symbols, footprints, project file, etc.)
    for f in folder.iterdir():
        if f.suffix not in (".kicad_sch", ".kicad_pcb") and f.is_file():
            shutil.copy2(f, out_dir / f.name)

    # Write ref map JSON
    (out_dir / "ref_map.json").write_text(
        json.dumps(combined_map, indent=2, sort_keys=True),
        encoding="utf-8"
    )

    # Write consistency report
    report_lines = [f"Project: {folder}"]
    if sch_only:
        report_lines.append("SCH-only refs: " + ", ".join(sorted(sch_only)))
    if pcb_only:
        report_lines.append("PCB-only refs: " + ", ".join(sorted(pcb_only)))
    if not sch_only and not pcb_only:
        report_lines.append("Consistency: PASS")
    (out_dir / "consistency_report.txt").write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8"
    )

    print(f"\n  Written to: {out_dir}")
    print(f"    {out_sch.name}")
    print(f"    {out_pcb.name}")
    print(f"    ref_map.json")
    print(f"    consistency_report.txt")

    return new_maxima


def parse_prefix_offsets(raw: str) -> dict[str, int]:
    """
    Parse  "R:13,C:7,U:2"  →  {"R": 13, "C": 7, "U": 2}.
    Raises ValueError on bad input.
    """
    result = {}
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"Expected PREFIX:NUMBER, got {token!r}")
        prefix, num = token.split(":", 1)
        result[prefix.strip()] = int(num.strip())
    return result


def overlap_check(maxima1: dict[str, int], maxima2: dict[str, int],
                  offsets2: dict[str, int]) -> list[str]:
    """
    Verify that project2's output refs do not overlap project1's.

    For each prefix present in both, project1 occupies 1..maxima1[prefix]
    and project2 occupies (offsets2[prefix]+1)..(offsets2[prefix]+count2).
    The safe condition is offsets2[prefix] >= maxima1[prefix] for every
    shared prefix.  Returns a list of violation strings (empty = clean).
    """
    problems = []
    for prefix in sorted(set(maxima1) & set(maxima2)):
        floor2 = offsets2.get(prefix, 0)   # project2 starts at floor2+1
        top1   = maxima1[prefix]            # project1 ends at top1
        if floor2 < top1:
            problems.append(
                f"  {prefix}: project1 ends at {prefix}{top1} but "
                f"project2 starts at {prefix}{floor2+1} — overlap!"
            )
    return problems


def main():
    parser = argparse.ArgumentParser(
        description="Normalise KiCad reference numbers for merging.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Two-project mode (recommended):
  %(prog)s  TRA-1004E/  UTR11-1104E/

  Writes to:
    ForMerging/same-TRA-1004E/      ← project1, compacted 1..N
    ForMerging/offset-UTR11-1104E/  ← project2, shifted above project1
  Offset is computed automatically. No copy-paste needed.

  Override the output parent folder:
  %(prog)s  TRA-1004E/  UTR11-1104E/  --out-dir MyMerge/

  Dry-run (no files written):
  %(prog)s  TRA-1004E/  UTR11-1104E/  --dry-run

Single-project mode (inspect or process one project):
  %(prog)s  project1/                          # inspect only
  %(prog)s  project1/  --out-dir out-p1/       # write normalised output
  %(prog)s  project2/  --prefix-offset C:23,R:28  --out-dir out-p2/
""",
    )
    parser.add_argument(
        "projects", type=Path, nargs="+",
        metavar="PROJECT_DIR",
        help="One project dir (single-project mode) or two project dirs (two-project mode).",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None, metavar="DIR",
        help=(
            "Two-project mode: parent folder for the two output dirs "
            "(default: ForMerging/ next to the first project). "
            "Single-project mode: output folder (default: inspect only)."
        ),
    )
    parser.add_argument(
        "--prefix-offset",
        metavar="P:N[,P:N…]",
        default="",
        help="Single-project mode only. Per-prefix start offsets, e.g. C:23,R:28.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print remaps without writing any files.",
    )
    args = parser.parse_args()

    if len(args.projects) > 2:
        parser.error("Expected 1 or 2 project directories.")

    for d in args.projects:
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory.", file=sys.stderr)
            sys.exit(1)

    # ── Two-project mode ──────────────────────────────────────────────────────
    if len(args.projects) == 2:
        p1_in, p2_in = args.projects

        if args.prefix_offset:
            print("WARNING: --prefix-offset is ignored in two-project mode.",
                  file=sys.stderr)

        # Output parent: --out-dir if given, else ForMerging/ beside project1
        out_parent = args.out_dir if args.out_dir else (p1_in.parent / "ForMerging")
        p1_out = out_parent / f"same-{p1_in.name}"
        p2_out = out_parent / f"offset-{p2_in.name}"

        if not args.dry_run:
            print(f"  Output folder : {out_parent}")
            print(f"    project1 → {p1_out.name}")
            print(f"    project2 → {p2_out.name}")

        # Step 1: normalise project1, compact to 1..N
        print("\n── Project 1 ──────────────────────────────────────────────────")
        p1_maxima = process_project(
            folder  = p1_in,
            out_dir = None if args.dry_run else p1_out,
            offsets = {},
            dry_run = args.dry_run,
        )

        # Step 2: derive per-prefix offsets from project1's compacted maxima
        offsets2   = dict(p1_maxima)
        offset_str = ",".join(f"{p}:{v}" for p, v in sorted(offsets2.items()))
        print(f"\n  → Auto-computed offsets for project2: {offset_str}")

        # Step 3: normalise project2 starting above project1
        print("\n── Project 2 ──────────────────────────────────────────────────")
        p2_maxima = process_project(
            folder    = p2_in,
            out_dir   = None if args.dry_run else p2_out,
            offsets   = offsets2,
            dry_run   = args.dry_run,
            p1_maxima = p1_maxima,
        )

        # Step 4: overlap check
        problems = overlap_check(p1_maxima, p2_maxima, offsets2)
        print()
        if problems:
            print("  *** OVERLAP DETECTED — outputs are NOT safe to merge ***")
            for p in problems:
                print(p)
            sys.exit(1)
        else:
            print("  Overlap check: PASS — ref ranges are non-overlapping.")
            if not args.dry_run:
                print(f"\n  Merge-ready outputs in: {out_parent}/")
                print(f"    {p1_out.name}/")
                print(f"    {p2_out.name}/")
        return

    # ── Single-project mode ───────────────────────────────────────────────────
    project_dir = args.projects[0]
    output_dir  = args.out_dir   # None → inspect/dry-run only

    offsets: dict[str, int] = {}
    if args.prefix_offset:
        try:
            offsets = parse_prefix_offsets(args.prefix_offset)
        except ValueError as exc:
            print(f"ERROR: --prefix-offset: {exc}", file=sys.stderr)
            sys.exit(1)

    new_maxima = process_project(
        folder  = project_dir,
        out_dir = output_dir,
        offsets = offsets,
        dry_run = args.dry_run,
    )

    offset_str = ",".join(f"{p}:{v}" for p, v in sorted(new_maxima.items()))
    print(f"\n  Per-prefix maxima (pass to project2 as --prefix-offset):")
    print(f"    --prefix-offset {offset_str}")


if __name__ == "__main__":
    main()

