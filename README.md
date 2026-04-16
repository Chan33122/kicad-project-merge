
# kicad-project-merge

Merge two KiCad 8 projects into a single ready-to-open project — without
breaking the schematic↔PCB link.

## Motivation

PCB fabricators typically require a minimum panel size (e.g. 50×50 mm). When two small prototype boards are panelised together, standard KiCad merge methods re-annotate reference designators, breaking the match between the physical silkscreen, schematic, BOM, and test procedures. 

## The problem

KiCad links PCB footprints to schematic symbols via a UUID stored in the
footprint's `(path "/SYMBOL_UUID")` field. Copy-pasting symbols in the
editor reassigns new UUIDs, breaking every footprint↔symbol link silently.
KiCad then falls back to reference-string matching and re-associates
footprints incorrectly, corrupting the merged design.

## What this tool is for

The primary use case is **proto panelisation**: two small boards merged
onto one panel to meet a fabricator's minimum panel size requirement
(typically 50×50 mm).

The guarantee the tool provides is:

> Every reference designator on the physical silkscreen matches exactly
> what is in the merged schematic and BOM — with zero re-annotation.

This keeps test procedures, probe notes, and firmware pin maps consistent
from the moment the panel arrives on the bench.


## The solution

This tool merges entirely by **text concatenation**, preserving every UUID
byte-for-byte. Only three fields change:

| Field | Change |
|---|---|
| SCH `(project "old")` | → `(project "merge")` |
| SCH `(path "/old-sheet-uuid")` | → `(path "/new-uuid")` |
| PCB `(sheetfile "old.kicad_sch")` | → `(sheetfile "merge.kicad_sch")` |

## Usage

```bash
python3 merge_projects.py  Project1/  Project2/
```

Produces a single KiCad project folder beside the source projects:

- Merge-Project1-Project2/
- Merge-Project1-Project2.kicad_pro   ← open this in KiCad
- Merge-Project1-Project2.kicad_sch
- Merge-Project1-Project2.kicad_pcb
- Merge-Project1-Project2.kicad_prl


Dry-run first to verify everything before writing:

```bash
python3 merge_projects.py  Project1/  Project2/  --dry-run
```

Optional gap overrides:

```bash
python3 merge_projects.py  Project1/  Project2/  --gap-sch 80  --gap-pcb 40
```

## What the three stages do

**Stage 1 — Reference normalisation** (`kicad_ref_normalise.py`)
- Compacts project1 refs to a dense 1..N range per prefix (C, R, U …)
- Shifts project2 refs to start above project1's maxima
- Repairs any split-state files where `(property "Reference")` and
  `(instances/reference)` disagree — a common artifact of prior partial operations
- Verifies no overlap before writing

**Stage 2 — Schematic merge** (`kicad_sch_merge.py`)
- Finds Y_max of project1 schematic content
- Shifts all project2 coordinates down by Y_max + gap (default 50.8 mm)
- Merges `lib_symbols` blocks (union, deduplicated by name)
- Adds a labelled dashed box around each project zone
- Sets a custom `User W H` paper size fitted to the merged content

**Stage 3 — PCB merge** (`kicad_pcb_merge.py`)
- Places project2 board to the right of project1 (X shift)
- Footprint-internal coordinates (pad positions) are relative and are NOT shifted
- Merges net tables by name — shared nets (GND, +3V3) get one ID
- Adds labelled boxes on F.SilkS
- Sets a custom paper size fitted to both boards

## Requirements

- Python 3.10+
- KiCad 8 (S-expression format — likely compatible with KiCad 6/7)
- No external Python packages required

## Files

- merge_projects.py           ← single entry point
- kicad_ref_normalise.py      ← stage 1
- kicad_sch_merge.py          ← stage 2
- kicad_pcb_merge.py          ← stage 3

All four scripts must be in the same directory.

## ⚠ Read before opening the merged project in KiCad

### Net isolation — shared net names

Power nets and signal nets with identical names in both projects (e.g.
`GND`, `+3V3`, `+1V8`, `+5V`) are automatically renamed in the offset
project by adding a `__` prefix:

| Original (same-project) | Renamed (offset-project) |
|---|---|
| `GND` | `__GND` |
| `+3V3` | `__+3V3` |
| `+1V8` | `__+1V8` |
| `+5V` | `__+5V` |

This renaming is applied consistently in three places:

- **Schematic power symbols** — the `(property "Value")` of every
  `#PWR<n>` symbol whose net name is shared. This is the field KiCad
  uses to assign the net name to all pins connected to that power rail.
- **Schematic wire labels** — any `(label)`, `(global_label)`, or
  `(hierarchical_label)` carrying a shared net name.
- **PCB net table and pad references** — the net name string in the
  top-level net table and inside every pad's `(net ID "name")` field,
  as well as zone fill `(net_name "name")` references.

The result is that the two boards are **fully electrically independent**
in the merged KiCad project. `GND` and `__GND` are separate nets with
no connection between them. KiCad will not show any inter-board ratsnest
or unrouted connections.

### What this means on the bench

The physical boards share no copper — each board's ground plane is its
own island on the panel. If you need the two boards to share a common
ground during test, add a wire between their GND test points manually.
This is the correct approach for an RF front-end panel where ground
coupling between two sensitive circuits would be undesirable anyway.

**The merged project is not plug-and-play. The following are expected and require manual attention:**

### PCB
- **Boards need manual repositioning** — the tool places them side by side with
  a fixed gap. You still need to arrange them into your final panel
  layout, add board-edge cuts, V-score lines, and tooling strips manually.
  


### Schematic
- **The two designs are not electrically connected** — the merge is
  purely additive. No cross-wiring is created.




---

## License

MIT — free to use, modify, and distribute.
Developed by Chandresh Sharma with Claude (Anthropic).

