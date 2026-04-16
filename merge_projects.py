#!/usr/bin/env python3
"""
merge_projects.py  —  Unified KiCad 8 project merge tool
=========================================================
Merges two KiCad projects into a single ready-to-open project.

Usage
-----
  python3 merge_projects.py  path/to/TRA-1004E  path/to/UTR11-1104E

Output
------
  <parent-of-project1>/Merged/
      ForMerging/
          same-TRA-1004E/          ← project1 refs compacted 1..N
          offset-UTR11-1104E/      ← project2 refs shifted above project1
          merge/                   ← OPEN THIS in KiCad
              merge.kicad_sch
              merge.kicad_pcb
              merge.kicad_pro
              merge.kicad_prl

Options
-------
  --gap-sch MM    Vertical gap (mm) between the two designs on the
                  schematic sheet  (default 50.8 = 20 × 2.54 grid units)
  --gap-pcb MM    Horizontal gap (mm) between the two boards on the PCB
                  (default 25.4 = 10 × 2.54 grid units)
  --dry-run       Run all stages, print all diagnostics, write nothing.

What happens internally
-----------------------
  Stage 1 — kicad_ref_normalise :
      Compact project1 refs to a dense 1..N range.
      Shift project2 refs to start above project1's maxima (per prefix).
      Verify no overlap.  Write same-* and offset-* folders.

  Stage 2 — kicad_sch_merge :
      Find Y_max of same-project schematic.
      Shift all offset-project coordinates down by Y_max + gap.
      Merge lib_symbols (union, deduplicated by name).
      Concatenate all placed elements.
      Update (instances project/path) to point at the new merged sheet UUID.
      Write merge/merge.kicad_sch.

  Stage 3 — kicad_pcb_merge :
      Find X_max of same-project PCB.
      Shift all offset-project footprint placements and copper right by
      X_max + gap.  Footprint-internal (pad) coordinates are relative
      and are NOT shifted.
      Merge net tables (union by name, remap IDs).
      Update (sheetfile) to merge.kicad_sch.
      Write merge/merge.kicad_pcb.
"""

import sys
import argparse
import importlib.util
from pathlib import Path


# ---------------------------------------------------------------------------
# Import the three sub-scripts as modules without executing their main()
# ---------------------------------------------------------------------------

def _load(script_path: Path):
    """Load a .py file as a module from its path."""
    spec = importlib.util.spec_from_file_location(
        script_path.stem, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_scripts(this_script: Path) -> tuple[Path, Path, Path]:
    """
    Locate the three sub-scripts relative to merge_projects.py.
    They must all live in the same directory.
    """
    base = this_script.parent
    names = {
        'normalise': 'kicad_ref_normalise.py',
        'sch_merge': 'kicad_sch_merge.py',
        'pcb_merge': 'kicad_pcb_merge.py',
    }
    paths = {}
    missing = []
    for key, fname in names.items():
        p = base / fname
        if not p.exists():
            missing.append(fname)
        paths[key] = p
    if missing:
        print("ERROR: the following scripts must be in the same folder as "
              "merge_projects.py:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)
    return paths['normalise'], paths['sch_merge'], paths['pcb_merge']


# ---------------------------------------------------------------------------
# Section headers for readable console output
# ---------------------------------------------------------------------------

def _banner(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="merge_projects.py",
        description="Merge two KiCad 8 projects into one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 merge_projects.py  TRA-1004E/  UTR11-1104E/
  python3 merge_projects.py  TRA-1004E/  UTR11-1104E/  --dry-run
  python3 merge_projects.py  TRA-1004E/  UTR11-1104E/  --gap-pcb 30
""",
    )
    parser.add_argument("project1", type=Path,
                        help="First project folder (becomes same-* / ref 1..N)")
    parser.add_argument("project2", type=Path,
                        help="Second project folder (becomes offset-* / refs shifted up)")
    parser.add_argument(
        "--gap-sch", type=float, default=None, metavar="MM",
        help="Vertical gap between designs on the schematic sheet in mm "
             "(default: 50.8 = 20 × 2.54).",
    )
    parser.add_argument(
        "--gap-pcb", type=float, default=None, metavar="MM",
        help="Horizontal gap between boards on the PCB in mm "
             "(default: 25.4 = 10 × 2.54).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run all stages and print diagnostics without writing any files.",
    )
    args = parser.parse_args()

    # ── Validate inputs ───────────────────────────────────────────────────
    for d in (args.project1, args.project2):
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory.", file=sys.stderr)
            sys.exit(1)

    p1 = args.project1.resolve()
    p2 = args.project2.resolve()

    # ── Output layout ─────────────────────────────────────────────────────
    # Everything beside project1's parent (the CWD equivalent).
    # Staging:  <parent>/ForMerging/same-P1/  and  offset-P2/
    # Project:  <parent>/Merge-P1-P2/         ← open this in KiCad
    base         = p1.parent
    project_name = f"Merge-{p1.name}-{p2.name}"
    merge_out    = base / project_name          # the KiCad project folder
    formerging   = base / "ForMerging"
    same_out     = formerging / f"same-{p1.name}"
    offset_out   = formerging / f"offset-{p2.name}"

    print(f"\n  project1 : {p1}")
    print(f"  project2 : {p2}")
    print(f"  staging  : {formerging}/")
    print(f"               same-{p1.name}/")
    print(f"               offset-{p2.name}/")
    print(f"  output   : {merge_out}/  ← open this in KiCad")

    # ── Locate sub-scripts ────────────────────────────────────────────────
    this_script = Path(__file__).resolve()
    normalise_path, sch_merge_path, pcb_merge_path = _find_scripts(this_script)

    norm = _load(normalise_path)
    schm = _load(sch_merge_path)
    pcbm = _load(pcb_merge_path)

    # ── Stage 1: ref normalisation ────────────────────────────────────────
    _banner("Stage 1 / 3 — Reference normalisation")

    # Project 1: compact to 1..N
    print("\n── Project 1 ──────────────────────────────────────────────────")
    p1_maxima = norm.process_project(
        folder  = p1,
        out_dir = None if args.dry_run else same_out,
        offsets = {},
        dry_run = args.dry_run,
    )

    # Derive per-prefix offsets from project1's compacted maxima
    offsets2   = dict(p1_maxima)
    offset_str = ",".join(f"{p}:{v}" for p, v in sorted(offsets2.items()))
    print(f"\n  → Auto-computed offsets for project2: {offset_str}")

    # Project 2: shift above project1
    print("\n── Project 2 ──────────────────────────────────────────────────")
    p2_maxima = norm.process_project(
        folder    = p2,
        out_dir   = None if args.dry_run else offset_out,
        offsets   = offsets2,
        dry_run   = args.dry_run,
        p1_maxima = p1_maxima,
    )

    # Overlap check
    problems = norm.overlap_check(p1_maxima, p2_maxima, offsets2)
    print()
    if problems:
        print("  *** OVERLAP DETECTED — aborting ***")
        for p in problems:
            print(p)
        sys.exit(1)
    print("  Overlap check: PASS — ref ranges are non-overlapping.")

    # ── Stage 2: schematic merge ──────────────────────────────────────────
    _banner("Stage 2 / 3 — Schematic merge")

    sch_gap = args.gap_sch if args.gap_sch is not None else schm.DEFAULT_GAP_MM
    schm.merge_schematics(
        same_dir   = same_out   if not args.dry_run else p1,
        offset_dir = offset_out if not args.dry_run else p2,
        out_dir    = merge_out,
        gap        = sch_gap,
        dry_run    = args.dry_run,
    )

    # ── Stage 3: PCB merge ────────────────────────────────────────────────
    _banner("Stage 3 / 3 — PCB merge")

    pcb_gap = args.gap_pcb if args.gap_pcb is not None else pcbm.DEFAULT_GAP_MM
    pcbm.merge_pcbs(
        same_dir   = same_out   if not args.dry_run else p1,
        offset_dir = offset_out if not args.dry_run else p2,
        out_dir    = merge_out,
        gap        = pcb_gap,
        dry_run    = args.dry_run,
    )

    # ── Write .kicad_pro and .kicad_prl inline ───────────────────────────
    if not args.dry_run:
        pro_path = merge_out / f"{project_name}.kicad_pro"
        prl_path = merge_out / f"{project_name}.kicad_prl"

        if not pro_path.exists():
            pro_path.write_text(f"""\
{{
  "board": {{
    "3dviewports": [],
    "design_settings": {{
      "defaults": {{}},
      "diff_pair_dimensions": [],
      "drc_exclusions": [],
      "rules": {{}},
      "track_widths": [],
      "via_dimensions": []
    }},
    "ipc2581": {{
      "dist": "",
      "distpn": "",
      "internal_id": "",
      "mfg": "",
      "mpn": ""
    }},
    "layer_presets": [],
    "viewports": []
  }},
  "boards": [],
  "cvpcb": {{
    "equivalence_files": []
  }},
  "libraries": {{
    "pinned_footprint_libs": [],
    "pinned_symbol_libs": []
  }},
  "meta": {{
    "filename": "{project_name}.kicad_pro",
    "version": 1
  }},
  "net_settings": {{
    "classes": [
      {{
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.2,
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "wire_width": 6
      }}
    ],
    "meta": {{
      "version": 3
    }},
    "net_colors": null,
    "netclass_assignments": null,
    "netclass_patterns": []
  }},
  "pcbnew": {{
    "last_paths": {{
      "gencad": "",
      "idf": "",
      "netlist": "",
      "plot": "",
      "pos_files": "",
      "specctra_dsn": "",
      "step": "",
      "svg": "",
      "vrml": ""
    }},
    "page_layout_descr_file": ""
  }},
  "schematic": {{
    "legacy_lib_dir": "",
    "legacy_lib_list": []
  }},
  "sheets": [],
  "text_variables": {{}}
}}
""", encoding="utf-8")

        if not prl_path.exists():
            prl_path.write_text(f"""\
{{
  "board": {{
    "active_layer": 0,
    "active_layer_preset": "",
    "auto_track_width": true,
    "hidden_netclasses": [],
    "hidden_nets": [],
    "high_contrast_mode": 0,
    "net_color_mode": 1,
    "opacity": {{
      "images": 0.6,
      "pads": 1.0,
      "tracks": 1.0,
      "vias": 1.0,
      "zones": 0.6
    }},
    "selection_filter": {{
      "dimensions": true,
      "footprints": true,
      "graphics": true,
      "keepouts": true,
      "lockedItems": false,
      "otherItems": true,
      "pads": true,
      "text": true,
      "tracks": true,
      "vias": true,
      "zones": true
    }},
    "visible_items": [
      0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19,
      20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33, 34, 35,
      36, 39, 40
    ],
    "visible_layers": "fffffff_ffffffff",
    "zone_display_mode": 0
  }},
  "git": {{
    "repo_password": "",
    "repo_type": "",
    "repo_username": "",
    "ssh_key": ""
  }},
  "meta": {{
    "filename": "{project_name}.kicad_prl",
    "version": 3
  }},
  "project": {{
    "files": []
  }}
}}
""", encoding="utf-8")

    # ── Rename SCH and PCB to match the project folder name ──────────────
    if not args.dry_run:
        import shutil as _shutil
        for ext in ("kicad_sch", "kicad_pcb"):
            src = merge_out / f"merge.{ext}"
            dst = merge_out / f"{project_name}.{ext}"
            if src.exists():
                src.rename(dst)

        # ── Clean up staging folder ───────────────────────────────────────
        _shutil.rmtree(formerging, ignore_errors=True)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if args.dry_run:
        print("  DRY-RUN COMPLETE — no files were written.")
    else:
        print("  MERGE COMPLETE")
        print(f"\n  KiCad project : {merge_out}/")
        print(f"    {project_name}.kicad_sch")
        print(f"    {project_name}.kicad_pcb")
        print(f"    {project_name}.kicad_pro   ← open this in KiCad")
        print(f"    {project_name}.kicad_prl")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
