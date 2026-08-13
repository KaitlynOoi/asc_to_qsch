# LTspice → QSpice Conversion Tool — Project Handoff

**Project:** `newtool.py` — converts LTspice `.asc` schematics into QSpice-compatible `.qsch` files, then fixes up every known LTspice/QSpice incompatibility so the result actually simulates.
**Deliverable:** `dist/newtool.exe` (CLI + GUI, built with PyInstaller from `newtool.spec`).
**Status:** Working, verified against two real production circuits (a CLLC resonant converter and a CV+CC control loop, both containing logic gates, switches, coupled inductors, and multiple missing device models).

This document is written for whoever inherits this project next. It covers what the tool does, how every part of it works, every real bug found along the way (in our own code, in the vendored library, and in QSpice/QSpice-tooling itself), and what's still open or worth improving.

---

## 1. What this tool actually does

LTspice and QSpice both use a `.asc`/`.qsch`-style schematic format, but they are **not** compatible file formats, and QSpice's simulation engine does not support everything LTspice's does. Converting a real schematic by hand is slow and error-prone. This tool automates it in two steps:

1. **Structural conversion** (`convert_asc_to_qsch`): reads the `.asc`, resolves every component's real QSpice-drawable symbol, and writes a `.qsch` with the same components, wiring, and values.
2. **Fix-up pass** (`process_models`): finds anything the raw conversion couldn't resolve on its own (missing SPICE models/libraries, LTspice-only digital logic gates, zero-ohm resistors, annotation text QSpice would choke on, etc.), asks the user how to resolve each one (CLI prompts or GUI dialogs), and applies the fixes.

The tool has both a CLI (`newtool.exe --cli ...` or run from Python) and a GUI (`newtool.exe`, the default). Both drive the exact same underlying conversion/fix-up functions — nothing in the actual conversion logic depends on which interface is used.

---

## 2. Architecture / pipeline

```
.asc file
   │
   ▼
convert_asc_to_qsch()          <- always runs first, unconditionally
   - parses the .asc via spicelib_vendor's AscEditor
   - resolves every component's real QSpice symbol (.asy lookup, or a
     built-in QSCH primitive template, or a placeholder if nothing found)
   - copies everything into a QschEditor and saves a first-pass .qsch
   - runs 3 post-copy fixups that undo real bugs in the vendored
     spicelib's copy_from() (see section 4)
   │
   ▼
.qsch file exists on disk (structurally converted, not yet "fixed up")
   │
   ▼
process_models()               <- the interactive fix-up pass
   PHASE 1: ask about every missing library/device model
      (one prompt per model; CLI prompts or GUI file-picker dialogs)
   [optional] REVIEW screen: see every Phase-1 answer, redo any of them
   [optional] CONFIRM screen: "generate with these choices?" Yes/No
   PHASE 2: apply everything -- inject libraries/models, synthesize
      LTspice-only digital gates, clean up annotations, replace 0-ohm
      resistors with wires, damp undamped inductors, normalize .lib tags
   │
   ▼
final .qsch file, ready to open/simulate in real QSpice
```

The review/confirm screens are optional hooks (`review_choices`, `confirm_before_generating` parameters on `process_models`) — a caller that doesn't pass them gets the old, immediate-apply behavior. Both the CLI and GUI pass them; this is what gives the "ask everything first, review, then confirm before writing" two-phase UX.

---

## 3. Function reference

### 3.1 QSpice environment discovery
| Function | Purpose |
|---|---|
| `_find_qspice_install_dir()` | Finds the real QSpice install directory on this machine (env vars + Windows "App Paths" registry fallback). Never a hardcoded path. |
| `_get_qspice_native_symbol_names()` | Scans QSpice's actual installed `.qsym` library files to build the real set of parts QSpice ships natively. Replaced an earlier hand-maintained guess-list that was proven wrong (it silently skipped resolution for parts like 2N3904/1N4148/LT1001 that QSpice doesn't actually ship). Cached per process run; returns empty set if QSpice isn't installed (never assumes). |
| `is_qspice_local_library_component()` | True if a model name is either one of LTspice's generic primitive types (res/cap/npn/...) or something QSpice genuinely ships, per the scan above. |

### 3.2 Model/library resolution
| Function | Purpose |
|---|---|
| `classify_spice_model()` | Decides whether a found model definition should be injected inline into the schematic or referenced via a `.lib` include, based on real syntax (`.subckt` vs `.model`, primitive device types, size/complexity) — not just line counts. |
| `find_coupled_inductors()` | Finds every inductor referenced in a `K` mutual-inductance statement, so those inductors are excluded from auto-damping (their coupling relationship matters more than adding series resistance). |
| `get_default_search_roots()` | Builds the ordered list of directories to search for missing model files (the `.asc`'s own folder first, then standard LTspice lib paths, then Documents/Desktop/Downloads). |
| `_build_model_candidate_index()` / `find_model_candidates()` / `_clear_model_candidate_cache()` | The model-file search engine. **Performance-critical** — see section 5. Builds one index of every `.model`/`.subckt` name found across the search roots (one filesystem walk, cached per `process_models()` run), then does O(1) lookups per missing model instead of re-walking the filesystem once per model. |
| `_extract_model_definition()` | Pulls the exact `.model`/`.subckt` text block for one part out of a candidate file. |
| `_existing_model_definitions()` / `_parse_model_lines()` | Reads what's already defined inline in the `.qsch`, to avoid double-injecting a model that's already there. |
| `validate_and_inject_device_model()` | Injects a hand-typed or found model definition into the schematic, after checking it doesn't collide with an existing one. |
| `_inject_raw_instruction()` / `_inject_lib_instruction()` | Low-level helpers that add a new SPICE directive text box (a `.lib` line, or any raw text) to the schematic. |
| `_normalize_lib_tags()` | Cleans up/deduplicates `.lib`/`.include` directives after injection. |
| `parse_asc_ref_to_symbol()` / `parse_qsch_x_type_refs()` / `parse_qsch_primitive_device_refs()` | Various "what does this component actually resolve to" lookups used to detect what's still missing. |

### 3.3 QSCH file writing — undoing real bugs in the vendored library
| Function | Purpose |
|---|---|
| `force_save_qsch()` | Forces a save even when spicelib's own dirty-flag logic thinks nothing changed (see section 4.1). |
| `_patch_spicelib_qsch_colon_parsing()` | Runtime monkey-patch fixing a real parser bug in spicelib's `QschTag.parse()` around colon-separated text. |
| `_corrected_asy_to_qsch()` (module-level patch on `AsyReader.to_qsch`) | Fixes a real bug in spicelib's ARC-to-QSCH-`arc3p` conversion that produced collapsed/degenerate curve geometry — this hit every LTspice logic gate, since their bodies are drawn with the ARC primitive. |
| `_restore_wire_net_names()` | Fixes a real bug in spicelib's `copy_from()`: every converted wire's stored net name comes out as literal `"0"` (ground) regardless of what it's actually connected to. Re-derives the real name via a full wire-connectivity graph trace. |
| `_merge_aliased_nets()` | LTspice allows one electrical node to carry multiple different FLAG names; its own netlister silently merges these into one canonical name. QSpice does not do this automatically, so this function replicates it (ground wins if present, else alphabetically-first name), rewriting every `net`/`wire`/`V(...)` reference consistently. |
| `_fix_text_comment_flags()` | Fixes a real bug in spicelib's `copy_from()`: every text box (real directives AND plain comment/design-note text) gets written with the "not a comment" flag, discarding the real distinction LTspice's own file format makes. Confirmed causing a real `"Fatal error: Unknown device type: 'A'"` crash on a real circuit, from a design note (`"a) Resonant Frequency:"`) being parsed downstream as if it were an actual device line. |
| `_build_qsch_wire_graph()` | Union-find graph over every wire endpoint + net label — the shared foundation both of the above two functions use to trace real connectivity. |

### 3.4 Component value / annotation fix-ups
| Function | Purpose |
|---|---|
| `_is_zero_ohm_value()` / `replace_zero_ohm_resistors_with_wires()` | Detects literal 0-ohm resistors (a common LTspice idiom for "just connect these two nodes") and replaces them with a real wire, since a 0-ohm resistor can cause convergence problems in some simulators. |
| `ensure_inductor_damping()` | Adds a default `Rser=1m` to any standalone inductor with no series resistance specified at all — matches QSpice's own inductor model, which defaults to the same 1mΩ internally (confirmed via Qorvo forum research, not guessed). Skips inductors that already have a real `Rser`, and skips magnetically-coupled inductors entirely (their `K` coupling matters more). |
| `fix_misclassified_comments()` | A second-pass safety net for the comment/directive classification: reclassifies any text box that doesn't look like a real directive (no `.` prefix, no bare `K`-coupling syntax) as a comment. Checks **every line** inside a multi-line text box, not just the first — an earlier version only checked the first line and was demoting boxes that mixed a commented-out line with a real active directive (e.g. `;tran 20m startup\n.tran 40m startup`) entirely to "comment", silently disabling a genuine, active `.tran` line. |
| `_extract_rser_token()` / `_resolve_component_display_value()` | Carries a component's real series resistance (`Rser=...`) from LTspice's separate `SpiceLine` attribute into the value QSpice actually sees — see section 4.3 for why only `Rser` is carried over and not the rest of `SpiceLine`. |
| `convert_startup_to_uic()` | Opt-in, off-by-default: swaps `.tran ... startup` for `.tran ... uic` — a plain text change, nothing else. A more ambitious version that also ramped every source was built, verified structurally clean, and then removed at the user's direction (see section 6.6) after real-GUI testing showed no visible simulation benefit for the schematic clutter it added. |

### 3.5 Digital gate synthesis (the biggest, most involved piece)
LTspice ships ideal, zero-power digital logic gates (device type `A`: AND/OR/XOR/NAND/NOR/XNOR/BUF/INV). **QSpice's simulation engine has no support for type-`A` devices at all** — confirmed empirically (`Fatal error: Unknown device type: 'A'`). QSpice's own native gate library is a different, physically-powered part needing real Vdd/Vss wiring, so it's not a drop-in replacement. `synthesize_ltspice_digital_primitives()` instead replaces each gate with a QSpice `B`-type behavioral voltage source computing the exact same boolean function.

| Function | Purpose |
|---|---|
| `synthesize_ltspice_digital_primitives()` | The main entry point. Detects every stateless gate (`DIGITAL_PRIMITIVE_STATELESS_MODELS`), resolves its pin connectivity, builds the equivalent boolean expression, and draws one combined replacement component (covering both `Q` and `¬Q` outputs where applicable) at the gate's original position, nudged apart from neighbors if it would otherwise overlap. |
| `_digital_bool_expr()` | Builds the actual 0–1 algebraic expression per gate type (AND = product, OR = `1-(1-a)*(1-b)...`, XOR = parity formula, NAND/NOR/XNOR = inverted versions, BUF/INV = pass-through/invert). |
| `_get_reliable_asc_netlist()` / `_parse_netlist_device_tokens()` | Gets real, LTspice-resolved pin connectivity from a sibling `.net` file **if one already exists** (e.g. the user already simulated the circuit in LTspice). Deliberately does **not** invoke LTspice.exe to generate one on demand — that was tried during development and observed hanging indefinitely on a real circuit despite an explicit timeout. |
| `_build_qsch_wire_graph()` (reused) | The fallback when no `.net` file exists: traces gate pin connectivity directly from the converted schematic's own wire geometry via a full multi-hop graph trace. Verified against a real 8-gate circuit with no `.net` available to reproduce the exact same connectivity as that same circuit's real LTspice-resolved `.net`. |
| `_load_raw_for_logic_levels()` / `_lookup_raw_logic_levels()` | If a sibling `.raw` simulation result exists, looks up the real simulated high/low voltage levels for a given net, so the 0/1 normalization matches the circuit's actual logic levels instead of assuming generic values. |
| `_lookup_native_gate_shape()` | Looks up a matching visual shape from QSpice's own native gate library (`reference/qspice_gate_library.qsch`) so the synthesized replacement still *looks* like the original gate, even though it's electrically a behavioral source underneath. |
| `_get_or_create_logic_supply_net()` | Handles invented supply nets for gates that need one. |
| `_fix_stale_wire_names_at()` / `_connect_synth_pin_by_label()` | Draws a real wire from a synthesized pin back to its net's exact original position (not just a same-named label) when the net has no separate tag elsewhere in the file — labeling alone would create a genuinely floating node. |
| `_rotate_local_offset()` / `_compute_schematic_bounds()` | Geometry helpers for correct pin placement under rotation/mirroring, and for computing collision-avoidance bounds. |
| `_resolve_collision_free_origin()` (nested in `synthesize_ltspice_digital_primitives`) | Nudges a gate's placement the minimum distance needed to avoid overlapping an already-placed neighbor — general collision avoidance, not tuned to any one circuit's layout. |

**Deliberately NOT synthesized:** stateful primitives (flip-flops, counters, Schmitt triggers). Their behavior depends on past state or hysteresis, not just current input voltages — no fixed algebraic expression can reproduce that correctly. These need a real QSpice-native part swapped in by hand.

### 3.6 Symbol building
| Function | Purpose |
|---|---|
| `_build_placeholder_symbol()` | Builds a visible "unresolved" placeholder symbol when nothing better is found, so the user can see at a glance what still needs a real part. |
| `_parse_builtin_symbol_templates_from_xml()` / `_build_native_symbol()` | Builds a component from spicelib's own built-in QSCH primitive templates (used for resistors, capacitors, etc. that don't need a `.asy` file at all). |
| `_ind_absolute_pin_positions()` / `_build_ind_symbol_absolute()` | Inductors specifically get drawn with absolute (not relative/rotated) coordinates — this was found to be the only reliable way to keep inductor pin placement correct across all four rotation states. |
| `_patch_asc_editor_symbol_lookup()` / `_MissingSymbolStub` | Runtime patch so a missing `.asy` symbol file doesn't crash the whole conversion — falls back to a stub instead. |
| `_default_search_paths()` | Combines the user-supplied extra search folders with the standard defaults for `.asy` symbol file lookup (step 1 only — separate from the model-file search roots used in step 2). |

### 3.7 Main pipeline
| Function | Purpose |
|---|---|
| `convert_asc_to_qsch()` | Step 1 — see section 2. |
| `process_models()` | Step 2 — see section 2. Accepts optional `review_choices`/`confirm_before_generating` callbacks; omitting them reproduces the original immediate-apply behavior exactly. |
| `ProcessingResult` | The dataclass `process_models()` returns — counts of everything it did, plus a `cancelled: bool` flag distinguishing "user declined at the confirm step" from "actually completed" (added this session — see section 6.2). |

### 3.8 CLI and GUI (interactive UX)
Both interfaces implement the same four hook functions `process_models()` expects: a per-model chooser, a device-model chooser, a review-screen, and a confirm-screen. The CLI versions (`choose_model_path_cli`, `choose_device_model_cli`, `review_choices_cli`, `confirm_and_generate_cli`) use terminal prompts; the GUI versions (`choose_model_path_gui`, `choose_device_model_gui`, `review_choices_gui`, `confirm_and_generate_gui`) use Tkinter dialogs. `run_gui()` and `run_cli_combined()` wire everything together into the two-phase flow described in section 2.

---

## 4. Real bugs found (with sources, where applicable)

### 4.1 Bugs in the vendored `spicelib` library (not our code, but we work around them)
All of spicelib's own bugs are worked around via **runtime patches in `newtool.py`** (search for `_patch_`), not by editing the vendored copy directly — this keeps `spicelib_vendor/` diffable against a fresh upstream copy if that's ever useful.

1. **`AsyReader.to_qsch()` ARC conversion is broken.** It normalizes arc start/end points into unit-vector-ish fractions instead of computing real absolute coordinates, producing collapsed/degenerate geometry. This hits every LTspice logic gate, since their curved bodies use the ARC primitive. *Proof it's a spicelib bug, not ours:* spicelib itself has a second, different ARC conversion elsewhere (in `qsch_editor.py`'s `copy_from`) that does the trigonometry correctly — we just ported that same correct formula into the broken path.
2. **`QschEditor.copy_from()` writes every wire's net name as literal `"0"`** regardless of actual connectivity — the line that would set the real name is commented out in spicelib's own source.
3. **`QschEditor.copy_from()` writes every text box with the "is this a comment" flag hardcoded to "not a comment"**, discarding the real LTspice `!`-directive vs plain-comment distinction. Confirmed causing a real crash (see section 3.3/3.5).
4. **`QschTag.parse()` mishandles colon-separated text** in certain cases — patched.
5. **`QschEditor.save_as()` only writes when `self.updated` is `True`**, but that's a plain per-instance attribute set on load, not a property — so a module-level class patch can't override it; must be set directly on each instance before saving (`force_save_qsch`).
6. **`AsyReader.to_qsch()` silently discards `PINATTR SpiceOrder`.** LTspice's `.asy` format lets a symbol declare its pins in whatever order is visually convenient for the artwork, then override the actual SPICE node order per-pin via `PINATTR SpiceOrder n` (per LTwiki's `.asy` docs — drawing order is only the fallback when a pin omits it). spicelib parses `SpiceOrder` into each pin's attribute dict but never reads it back out in `to_qsch()`; every converted symbol emits its `«pin»` tags in raw drawing order instead. Confirmed empirically: a minimal test circuit using `AD743` (whose `-IN` pin is drawn before `+IN` but has `SpiceOrder 2` vs `+IN`'s `SpiceOrder 1`) produced a QSpice netlist reading `U1 MINUSIN PLUSIN VPLUS VMINUS VOUT AD743` instead of the correct `U1 PLUSIN MINUSIN ...` — silently swapping the op-amp's inverting/non-inverting inputs. Scanning every `.asy` file findable in this machine's local LTspice symbol libraries, **275 of 338 files with `SpiceOrder` attributes have at least one pin out of drawing-order** — this is the *majority* convention for real op-amp/comparator symbols (`AD743`, `LM393`, `OPA170`, `AD8014`, ... all affected), not an edge case. Fixed in our own `_corrected_asy_to_qsch` patch: pins are now re-sorted by `SpiceOrder` before emission whenever every pin on the symbol declares one (falling back to drawing order — matching LTspice's own documented behavior — if any pin omits it, so a partially-annotated symbol isn't scrambled). Does not affect either of this project's two real test circuits directly (their actual op-amp/comparator parts — `AD826A`, `AD823A`, `LT1714` — all happen to already have `SpiceOrder` matching drawing order), but is a real, general, previously-silent correctness bug for any future circuit using a standard op-amp.

### 4.2 Confirmed QSpice engine differences from LTspice (independently verified via Qorvo's own forum, not guessed)
| LTspice behavior | QSpice behavior | Source |
|---|---|---|
| `.tran ... startup` ramps sources from off over ~20µs before a normal transient | **Silently ignored** — not an error, just has no effect; falls back to a normal bias-point-solved transient | [Qorvo forum thread](https://forum.qorvo.com/t/startup-seems-to-be-ignored-by-qspice-in-the-trans-command/16629) |
| Multiple `.tran` directives | Only the **last** one is honored; earlier ones are silently ignored — can produce a completely different simulation if a duplicate is left in by accident | Directly observed on a real circuit this project |
| Capacitor `Rser` (ESR) | **Supported** | [Qorvo forum](https://forum.qorvo.com/t/why-doesnt-qspice-support-series-inductance-in-the-cpacitor-model/17470) |
| Capacitor `Lser` (ESL) | **Not implemented at all**, by deliberate design — "a single lumped series inductance usually caused more harm than good" (convergence problems) | Direct quote from Mike Engelhardt (QSpice's author), same thread. Recommended workaround: add a real, separate series inductor component instead of a parameter. |
| Inductor `Rser` | Supported, and QSpice's own inductor model **defaults to `Rser=1mΩ`** on its own | [Qorvo forum](https://forum.qorvo.com/t/parasitic-inductance-of-passive-components-lser/15608) |
| Ideal digital gates (device type `A`) | **Not supported at all** — `Fatal error: Unknown device type: 'A'` | Directly observed on real circuits this project |
| One electrical node with multiple different FLAG names | LTspice's own netlister silently collapses these to one canonical name | QSpice does **not** do this automatically — must be replicated manually (see `_merge_aliased_nets`) |

### 4.3 Why the fix only carries over `Rser`, not all of `SpiceLine`
LTspice stores a passive part's extra parasitic parameters (ESR, ESL, parallel R/C, tolerance) in a separate `SpiceLine` attribute, not in `Value`. An earlier version of `_resolve_component_display_value()` appended the *entire* `SpiceLine` string onto the component's value. Given the confirmed `Lser` gap above, that would silently produce an unrecognized parameter on any capacitor with a real ESL specified. The fix now extracts and carries over **only** the `Rser=` token via regex (`_extract_rser_token`), which is confirmed safe for both capacitors and inductors, and drops everything else rather than pass through something that would be silently ignored anyway.

### 4.4a Bug in our own code: `_rotate_local_offset` only handled half of QSpice's real rotation range
`_rotate_local_offset()` (used by the gate-synthesis wire-tracing fallback to compute a pin's absolute position from its component's placement + rotation) only implemented QSCH orientation codes 0-7. QSpice's real range is **0-15**: 0-7 are the four 0/90/180/270° rotations plus their mirrored counterparts, but **8-15 are a separate mirrored variant** with the X-offset's sign flipped relative to 0-7 — confirmed directly from spicelib's own `QschEditor._find_pin_position`, the actual ground truth QSpice itself was built against.

Found while investigating why a resistor (R34) appeared "unwired" during a manual connectivity check on a real circuit: for orientation 8, the old code silently computed a position 200 units off from either of R34's real wire endpoints (it treated orientation 8 as equivalent to orientation 0, due to `8 * 45° = 360°` wrapping around, and never applied the mirror's sign flip at all). R34 itself turned out to be correctly wired in the actual schematic (its real connectivity comes from spicelib's own geometry-preserving `copy_from`, not from this function) — but this function is exactly what the digital-gate wire-tracing fallback depends on, so **any mirrored gate (orientation 8-15) would have had its pin connectivity silently miscalculated**, which is very likely the explanation for the earlier, previously-unexplained case (see section 6, item 1) where gate connectivity resolution returned nothing for a whole circuit despite the fallback supposedly working standalone. Fixed by implementing the full 0-15 range exactly as spicelib's own method does it.

**A second instance of the exact same bug pattern was found and fixed in `replace_zero_ohm_resistors_with_wires`** immediately after, by deliberately auditing every other place in the codebase that reads a component's rotation (since finding one instance of "assumes only half the real range" meant there could be others). Its own local orientation calculation was `orientation = (rot_val // 45) % 8` — the `% 8` unconditionally wrapped any mirrored value (8-15) straight back down into 0-7, discarding the mirror bit the exact same way. Confirmed on R34 directly: `rot_val` there is `360` (spicelib's own degree-based rotation attribute, where 0-359 = normal and 360-719 = mirrored, matching the convention `_ind_absolute_pin_positions` already used correctly elsewhere in this file), so `360 // 45 = 8` — the correct ground-truth QSCH orientation — but the old `% 8` turned that back into `0`. This path is used when replacing a literal 0-ohm resistor with a plain wire; a mirrored 0-ohm resistor would have had its replacement wire computed against the wrong net. Fixed by removing the incorrect `% 8` entirely. A full audit of every other rotation-reading call site in the file (`_ind_absolute_pin_positions`, the inductor-placement branch in `convert_asc_to_qsch`, the LTspice-90°/270°-swap logic) found no further instances — those were all already correct.

### 4.4 QSpice_MCP tooling limitations (the automation/checker tool used during development — separate from real QSpice)
These are limitations of the **verification tooling**, not of QSpice itself or of this converter — but worth knowing so nobody chases a phantom bug:
- Its own `generate_netlist`/`run_simulation` can fail even on a completely untouched, unmodified circuit.
- Its netlist generator embeds literal two-character `\n` sequences instead of real newlines when a multi-line `.param` text block gets flattened, producing malformed multi-directive lines that crash the real QSpice engine with misleading errors (e.g. "Missing expression in B-source X") that have nothing to do with the named component.
- Its schematic checker's `"missing_value"` warning is a **false positive** for any inductor/capacitor whose value has extra text appended (e.g. `"617n Rser=1m"`) — confirmed because the user's own real, working, already-simulating file has always shown this warning on its inductors. Real QSpice accepts this syntax fine; the checker's parser just doesn't recognize the compound form.
- Its "comment vs directive" flag on a `.qsch` text box does not reliably reflect what the *real* QSpice engine treats as active — a genuinely-executing `.tran` line was observed with that flag set to "comment" in a real, working file.

### 4.5 Performance
`find_model_candidates()` originally re-walked the **entire** filesystem search path and re-read every candidate file from scratch, once per missing model — with 6 missing models on a real 265-component circuit, this meant 6 redundant full-tree scans, accounting for 94.6% of a 94.5-second total conversion time. Fixed by building one shared index (one walk, one read per file) cached for the duration of a single `process_models()` run, dropping the same conversion to ~21 seconds (~4.5× faster). Verified: identical model-resolution results before and after, identical checker output.

### 4.6 GUI/UX bugs found and fixed this session
1. Picking a new `.asc` file only auto-filled the matching output `.qsch` name if the output field had never been manually edited — meaning after the *first* manual rename (for any reason), every subsequent circuit picked afterward silently kept the stale old filename. Fixed: picking a new `.asc` always re-syncs the output name now.
2. Answering "No" on the final "generate?" confirmation dialog still ran every automatic fixup (annotation cleanup, zero-ohm replacement, inductor damping) and still saved the file — only library/model *injection* was actually skipped. Fixed: "No" now aborts before any of that runs, verified by checking the file's mtime/size are completely unchanged afterward.
3. The GUI's own "Finished! Saved to: ..." success dialog was shown **unconditionally** after `process_models()` returned, even when the user had just clicked "No" — so it looked like it saved anyway. Fixed by adding an explicit `cancelled` flag to `ProcessingResult` and showing a distinct, honest "Cancelled" message when it's set.
4. In the review/redo screen, cancelling out of a re-pick (e.g. accidentally double-clicking the wrong entry) unconditionally overwrote the existing choice with "no choice" — silently downgrading an already-correct selection. Fixed: a cancelled redo now preserves the previous choice unchanged.
5. The "Quit" button only called `root.destroy()`, which doesn't reliably end the underlying process — confirmed directly this session (closing the GUI left the compiled `.exe` still running in the background; `tasklist` showed two live `newtool.exe` instances at once after what looked like a clean close). Fixed by having both "Quit" and the window's own close ("X") button call `os._exit(0)` after tearing down the Tk mainloop, which guarantees real process termination.
6. **Removed, then had to partially reconsider, the "Extra .asy symbol search folders" GUI section.** It was removed on the reasoning that `_default_search_paths` (step 1, `.asy` symbol resolution) already covered every location that mattered. That reasoning was wrong and caused a real regression, found later the same session: a real circuit's four `AD826A` instances (a manufacturer-supplied symbol the user had placed directly in `~/Documents`, `~/Documents/LTspice`, and `~/Desktop/convergence` — none of them LTspice-standard folders, and none covered by `_default_search_paths`'s fixed list) silently failed to resolve, each falling back to a blank, pinless placeholder box in the converted schematic — this is almost certainly what was reported as "not even a correct shape or label, just a random rectangle." Checked LTspice's own config (`%APPDATA%\LTspice.ini`) hoping for a fully general auto-detected fix via its `UserCmpDir` setting, but that machine's `UserCmpDir` is set to `Downloads`, which only contains a differently-named `AD826.asy` (no trailing "A") — so no purely-automatic default would have found the real file either.

   The actual fix did **not** restore the removed UI. Step 2 (model/`.lib` fix-up) already used a much broader root list via `get_default_search_roots()` — recursively covering `Documents`, `Desktop`, and `Downloads`, not just LTspice's fixed install paths — while step 1 used its own separate, narrower list. That inconsistency, not the missing UI, was the real gap. `_default_search_paths` now folds `get_default_search_roots(asc_file)` in directly, so both steps search the same real-world locations, recursively (`os.walk` under each root — see `find_file_in_directory`), automatically. Verified: all four `AD826A` instances (plus every other component) now resolve with no code/UI addition needed — `269/269 components resolved with a real symbol, 0 placeholder(s)` on the real circuit, vs. 4 unresolved before the fix. The equivalent `--search-path` / `--auto-search` CLI flags are unaffected and still available for anything genuinely outside even this broader default set.

   **A second, unrelated bug was found and fixed while chasing this**: once `AD826A.asy` actually started resolving, conversion crashed with `KeyError: 'Description'` inside our own `_corrected_asy_to_qsch` patch (the ARC-geometry fix from item 1 in section 4.1). `AD826A.asy` has no `SYMATTR Description` line at all (confirmed — none of its 3 on-disk copies do), and our patch reads `self.attributes["Description"]` with bare bracket indexing, unlike upstream spicelib's own `to_qsch()`, which uses `.get("Description", "")`. This was a regression introduced when the ARC-fix patch was originally written (a full copy of `to_qsch()` with only the ARC branch intentionally changed) — the safe fallback was accidentally dropped along the way. This means **any `.asy` file without a `Description` attribute would have crashed the whole conversion outright**, not just `AD826A` — restoring only the search-path fix without this one would not have been enough. Fixed by restoring `.get("Description", "")`.
7. **Investigated but not reproduced**: a report that manually changing a `.lib` choice gets asked about a second time before the final "generate" confirmation. Audited the two most likely causes directly in the code — a model appearing in both the "library imports" and "device models" categories (guarded against at the `unresolved_device_models` filter, confirmed using the same `bare_model_name()` normalization in both categories, so no mismatch), and the review/redo flow dropping a manual choice (already fixed earlier this session — item 4 above). Neither reproduces the described behavior in the current code. If this is still seen on a fresh build, it needs a live repro (exact dialog sequence) to pin down, since it isn't one of the failure modes visible from static review.
8. **Merged the "library imports" and "device models" review screens into one.** `process_models` used to call the `review_choices` hook twice back-to-back — once to review every library-import file pick, once to review every device-model pick — producing two separate screens (CLI: two "--- Review: ... ---" prompts; GUI: two modal dialogs) the user had to step through in sequence for a single conversion. Reported as confusing/easy to lose track of. The underlying *injection* was already correctly unified (both get applied together in the single "Apply everything" block, and the final `confirm_before_generating` screen already showed both together) — only the intermediate review/redo step was needlessly split. Now `process_models` builds one combined item list (tagging each entry with which category it came from) and calls `review_choices` once; the injection format for each entry is untouched (library imports still inject a plain `.lib "path"` line, device models still inject an extracted/edited model card) — only the review *screen* is unified, not what gets written to the schematic. `review_choices_cli`/`review_choices_gui` needed no changes at all, since both already took `category_label`/`items_by_model` generically. Verified via a scripted stub `review_choices` on the real CV+CC circuit: all 7 unresolved models (3 library imports, 4 device models) now appear in one combined review pass, and the resulting `resolved_paths`/`device_choices`/injection counts are identical to the pre-merge two-screen flow (17 values fixed, 3 libs injected, 4 device models skipped — same as before). Also fixed a latent edge case surfaced while doing this: `resolved_paths` must never hold a `None` value (the injection loop right after it has no `if path:` guard), which the original two-call flow could already have violated if a user redid a library choice down to "none" — the new merge logic explicitly filters that out when writing `resolved_paths` back.
9. **Removed a misleading unused import.** `import spicelib_vendor as spicelib` sat at the top of `newtool.py`, directly underneath a comment saying the vendored library is "imported as `spicelib_vendor`, never `spicelib`" — the import line itself contradicted its own comment. Confirmed the `spicelib` alias was never actually referenced anywhere in the file (every class used is imported explicitly by name from its real submodule, e.g. `RawRead` at its one point of use) — it was dead code. Removed.
10. **Deleted `spicelib_vendor/scripts/rawplot.py`.** A separate, unrelated instance of the same real-vs-vendored `spicelib` confusion: this bundled utility script (not part of our own conversion pipeline — our own `RawRead` usage goes through `spicelib_vendor.raw.raw_read` directly) contained `from spicelib import RawRead`, i.e. an import of the *real*, unvendored pip package, not the vendored fork. Since the whole point of vendoring is to not depend on the real package being installed, this was a real, if latent, bug — the script would `ModuleNotFoundError` on any machine without the real `spicelib` pip package. Confirmed nothing else in the repo imports or otherwise references `rawplot.py`, so it was deleted outright rather than patched.
11. **A malformed/nonstandard `.asy` file (one that can't even open in real LTspice) crashed the whole conversion instead of falling back to a placeholder.** This is the behavior this tool is *supposed* to have for any symbol it can't fully resolve: still convert everything else, drop in a placeholder box for that one component (keeping its reference designator and value text), and let the user supply a matching library by hand during the model-fixup step. That fallback only worked for the "no file with this name exists anywhere" case (`FileNotFoundError`) — two other call sites had no exception handling at all: `AscEditor`'s own symbol lookup (patched in `_patch_asc_editor_symbol_lookup`, which only caught `FileNotFoundError`) and the main symbol-resolution loop's `asy_reader.to_qsch(...)` call in `convert_asc_to_qsch` (no `try`/`except` at all). A genuinely malformed file — confirmed directly with a minimal `.asy` using an unsupported drawing primitive — raises `NotImplementedError` from spicelib's own parser, not `FileNotFoundError`, so it went uncaught and crashed the run. This became much more likely to actually trigger after item 6's search-path fix: a stray/corrupt `.asy` sitting in `Documents`/`Desktop` that was previously never found (and so safely fell through to the placeholder) is now found by the broader search, and previously would have crashed instead of falling back. Fixed by broadening both catch sites to catch any exception during parse/convert, not just "not found," logging why, and falling back to the same placeholder path either way. Verified with a real malformed `.asy` (unsupported primitive): conversion now completes with `0/1 resolved, 1 placeholder` and a clear log line, instead of crashing.
12. **Added an "open the file now?" prompt after a successful conversion, and hardened both it and the pre-existing "Generate?" confirm prompt against a missing console.** GUI uses a normal `messagebox`; CLI's `input()` calls are now wrapped in broad exception handling, since a CLI run launched without an attached console (e.g. via a file association) has no real stdin and `input()` fails immediately rather than waiting for a keypress. The "open file?" prompt defaults to *not* opening on failure (harmless either way); the "Generate?" confirm prompt defaults to *not* generating (declining is the safe default when the whole point of asking is to let the user back out of something that looked wrong in review). Verified via real subprocess runs of the built exe with stdin genuinely closed: both now exit cleanly instead of crashing or hanging, while a real yes/no answer sequence still works exactly as before.
13. **Added a `.log` transcript export, same location and name as the converted `.qsch`.** GUI: the `log()` closure that already feeds the on-screen log widget now also collects every line into a list, written to `<qsch base name>.log` at every meaningful exit point (conversion error, model-fixup error, or normal completion/cancellation) so a failed run's transcript is captured too, not just a successful one. CLI: rather than threading a custom logger through every existing `print()`/`input()` call site, `sys.stdout` itself is wrapped in a small tee (`_TeeStdout`) for the duration of the run, capturing the complete transcript — phase headers, per-component resolution log, prompts, and answers — with zero changes to any existing call site. Verified end-to-end against the real built exe (not just the Python module): the `.log` file lands next to the `.qsch` with a matching base name and contains the full transcript from the very first line.
14. **Added explicit upfront totals before any missing library/model is individually asked about, at two levels.** Both categories (library imports, device models) already printed every model+component before their own asking loop started -- a "Total: N component(s) across M model(s)..." line was added on top of each existing listing, so the full scope of *that* category is obvious before diving into its per-model prompts, not just implied by counting list entries.

    On top of that, a single **combined, itemized** list across *both* categories is now shown before either one is asked about at all -- not just a total number, but every specific model and component reference, e.g.:
    ```
    FULL LIST OF COMPONENTS THAT WILL NEED SOMETHING IMPORTED OR DEFINED
    ~54 component(s) total, before any of them are individually asked about below: 17 confirmed library import(s) + ~37 device model(s) (approximate...).

    Library imports (confirmed):
      AD823A  (11 component(s): U1, U10, ...)
      ...
    Device models (approximate):
      1N4148  (4 component(s): D2 (D), D3 (D), ...)
      and  (6 component(s): A1 (A), A2 (A), ...)
      ...
    ```
    The device-model half is explicitly labeled approximate rather than exact: `parse_qsch_primitive_device_refs` isn't scoped to only diodes/BJTs/MOSFETs, it scans every non-passive component, so computing it this early (before digital-gate synthesis has run, further down) counts unconverted LTspice gates as "needing a model" even though synthesis will resolve them without ever asking about them -- confirmed directly on the real CV+CC circuit, where the preview lists `and`/`or` gates by name (8 components) that then never appear in the real, exact list further below once synthesis has resolved them (37 estimated vs. 29 actual, the gap being exactly those gates). The real, exact per-category listings immediately below are unaffected; only this one combined preview carries the "approximate" caveat, stated in its own text so it's never mistaken for the confirmed list. Purely additive -- no existing detection order, timing, or asking behavior changed; the preview uses its own throwaway `QschEditor` instance read-only for counting, never touching the one the rest of the pipeline uses.

---

## 5. Testing / verification methodology used

No automated way to run the *real* QSpice engine exists in the environment this was developed in, so verification relied on:
1. **Structural/content diffing** — comparing every SPICE directive between the source `.asc` and the converted `.qsch` (exact multiset comparison, normalized for whitespace/unit-symbol encoding), to catch anything silently dropped or duplicated.
2. **`QSpice_MCP`'s schematic checker** (`check_schematic`) — useful for catching floating pins, conflicting net labels, missing values, etc., but with the caveats in section 4.4 above; never trusted blindly when it disagreed with real observed behavior.
3. **The user's own real QSpice GUI runs** — the ultimate source of truth throughout, especially for anything involving actual simulation behavior (transient startup, gate logic, etc.), since the automation tooling above has confirmed gaps.
4. **Regression testing against two independent real production circuits** on every change — a CLLC resonant converter (with coupled inductors, GaN switches, behavioral parameter sources) and a separate CV+CC control loop circuit (with 8 real digital logic gates) — checking that resolved-component counts, injected libraries, and checker output stayed identical before/after each fix.
5. **Automated Tkinter-driven GUI tests** (in the scratch test directory, not shipped) for the interactive dialogs, simulating button clicks/selections programmatically to verify dialog logic without manual clicking.

---

## 6. Known limitations / open items / space for improvement

Ranked roughly by how much they'd matter to a real user:

1. ~~**Digital gate connectivity resolution can fail with no clear cause.**~~ **Root-caused and fixed** (see section 4.4a): two instances of the same rotation-math bug (only handling QSCH orientation codes 0-7 instead of the real 0-15) meant any mirrored component's pin position could be silently miscalculated. If the circuit that originally hit this had a mirrored gate, this is almost certainly why. *(Not re-tested against that original circuit specifically before this handoff — worth a quick confirmation pass.)*
2. **Capacitor ESL (`Lser`) is dropped, not converted.** *(No code change made — see 6.1 below for the researched alternative.)*
3. **Resistor parasitic parameters were not investigated in depth.** *(Partially researched — see 6.2 below.)*
4. **The `.ic` "asserted via a soft tie" behavior reported on the Qorvo forum remains unverified.** *(Re-searched again for this handoff — still unconfirmed; see 6.3 below.)*
5. **`.step` parameter sweeps** have multiple open/unresolved Qorvo forum reports of misbehaving. *(Researched — see 6.4 below; no clean workaround found.)*
6. **No fully automated real-QSpice-engine testing.** *(A real, viable path exists and just needs implementing — see 6.5 below, this is the most actionable item on this list.)*
7. **`spicelib` is vendored under its GPL-3.0 license.** This project copied the whole package into `spicelib_vendor/` for future-proofing (see `spicelib_vendor/VENDORED.md`), but the copyleft licensing implications of shipping/distributing this tool have not been resolved — that's a legal/compliance decision for whoever owns distribution of this project, not something decided in code.
8. **The `build/` directory** (PyInstaller's intermediate artifacts) was persistently locked/undeleteable in this environment ("Device or resource busy") across multiple attempts. It's excluded from git via `.gitignore` and is safe to delete manually via File Explorer if it's ever in the way — it regenerates automatically on the next `pyinstaller newtool.spec` build.
9. **`.tran ... startup` being silently ignored can shift when a transient feature (ringing, a startup spike) appears, relative to LTspice.** Still open. A general fix (ramping every source) was built, verified structurally correct, then removed after real testing showed no visible benefit for the added schematic clutter — see 6.6. A plain `.tran startup` → `uic` text-swap-only option remains available (opt-in) but is confirmed *not* an improvement on its own.

### 6.1 Alternative for capacitor `Lser` (ESL)
**Confirmed workaround, not yet implemented:** insert a real, separate series inductor component between the capacitor and one of its original net connections, carrying the `Lser` value — this is Mike Engelhardt's own recommendation (*"you can add your own L in series to the cap. This should be an exact equivalent to the sim"*, same forum thread cited in 4.2). Implementing this generally would mean: for any capacitor whose `SpiceLine` contains an `Lser=` token, insert a new inductor component in series (new component + net rename + two new wires), rather than just editing text. This is a genuine topology change, which is why it was scoped out of the current code changes — flagged here as a concrete, ready-to-implement next step rather than an open research question.

### 6.2 Alternative for resistor parasitics
Research for this handoff confirmed QSpice resistors support **tolerance-based standard-value snapping** (1%/5%/10%, E96/E24/E12 series) via a right-click "Standard Value" option in the GUI — but found **no confirmed syntax for resistor series inductance or other parasitic modeling**. Given `Lser` is confirmed unsupported on capacitors by deliberate design, it's reasonable to assume the same applies to resistors, though this wasn't independently confirmed. If a real circuit needs resistor ESL modeled, the same "insert a real series inductor" workaround from 6.1 would apply. [Source](https://www.powerelectronicsnews.com/qspice-behavioral-resistors-part-11/)

### 6.3 Alternative for `.ic` behavior — still unverified
Re-searched for this handoff specifically (Qorvo forum, LTspice/QSpice documentation). Still could not independently confirm the mechanism the earlier forum thread described (`.ic` asserted via a soft ~1mΩ tie rather than a hard constraint). No new alternative or workaround was found either. **Recommendation for whoever picks this up:** if a converted circuit has a `.ic` statement on a genuinely high-impedance node (large `R` to ground/supply, or a node fed only by a current source) and its behavior looks suspicious, that's the first place to look — but there's currently no general code-level mitigation to apply, since the exact mechanism isn't confirmed.

### 6.4 Alternative for `.step` sweep issues — no clean workaround found
Multiple open, unresolved Qorvo forum threads describe `.step` being silently ignored in some netlists, or not propagating into a stepped `.model` parameter. No single confirmed workaround was found for this handoff. LTspice has its own alternative technique (combining multiple swept parameters into one `.step` via a `table()` lookup function) that might reduce exposure to whichever specific `.step` variant is failing, but this was **not verified against QSpice specifically** — flagging it as a lead, not a confirmed fix. [Related threads](https://forum.qorvo.com/t/qspice-doesnt-follow-the-step-command/25710), [here too](https://forum.qorvo.com/t/how-to-step-parameters-of-a-diode-model/17940).

### 6.5 Alternative for automated real-QSpice testing — a real path exists
This is the most concrete, actionable item found during this handoff's research. **QSpice genuinely supports headless, GUI-free command-line execution** — confirmed directly from the vendored `spicelib_vendor/simulators/qspice_simulator.py` (not a guess: this is the exact command spicelib itself constructs and runs):
```
QSPICE64.exe -o <logfile.log> <netlist.net> [-binary|-ASCII] [-r <output.qraw>]
```
This runs directly against a `.net`/`.cir` netlist file — no schematic GUI involved at all — and produces a real `.qraw`/`.log` from QSpice's actual simulation engine. Combined with spicelib's own already-vendored `RawRead` (in `spicelib_vendor/raw/raw_read.py`), this is a fully viable path to **real**, automatable verification that doesn't depend on the `QSpice_MCP` tooling's confirmed limitations (section 4.4) or on a human manually running the GUI. **Not implemented in this project** — the verification approach used throughout this handoff's development (section 5) relied on `QSpice_MCP` and manual GUI checks instead — but this is a concrete, ready-to-build next step for a more reliable CI-style test suite.

### 6.6 Alternative for `.tran ... startup` being ignored — tried, partially rolled back
Confirmed via direct testing: switching to `.tran ... uic` **on its own** (without also changing the sources) is not an improvement — it made a real circuit's result *worse* (lost the entire decaying-ring startup transient, since `uic` skips the bias-point solve and starts every un-`.ic`'d node/state at zero instead of a computed operating point).

**A general fix was built and then removed at the user's explicit direction.** The full version (`convert_startup_to_uic_with_source_ramp()`, briefly in the codebase) replicated LTspice's actual ramped-startup behavior for *any* source type: it physically relocated every independent voltage/current source to a fresh, empty staging area outside the schematic's bounding box, then dropped a small behavioral (`B`-type) scaler at the source's old position computing `V=(new_net_plus - new_net_minus)*min(1,time/20u)` — since an ideal source's own terminal output *is* its waveform value at that instant regardless of function type, scaling it this way ramps any source shape (PULSE/SINE/EXP/SFFM/PWL/constant) without reimplementing any SPICE waveform formula. Two real bugs were found and fixed while building it (both via the real `QSpice_MCP` checker, not assumed): a naive relocation offset ran a new wire straight through the interior of an unrelated existing wire (fixed by staging in genuinely empty space), and a hardcoded pin template silently disconnected a current source with different real pin geometry (fixed by reading the original component's own actual pins). It was verified structurally clean (0 checker errors) on both real circuits, including one with 32 ramped sources of mixed types.

**Removed anyway**: real-world testing (GUI, by the user) showed it produced a visible "wall" of disconnected-looking staged components in the schematic without a corresponding visible improvement in the simulated result. Not worth the schematic clutter for a benefit that didn't show up in practice on the circuits tried.

**What remains, opt-in**: `convert_startup_to_uic()` — a plain text swap, `.tran ... startup` → `.tran ... uic`, nothing else moved or added. Still gated behind `apply_uic_workaround` (off by default; GUI checkbox / `--apply-uic-workaround` CLI flag), since `uic` alone is confirmed *not* an improvement over leaving `startup` in as a no-op for at least one real circuit (see the first paragraph above) — it's provided as a building block for further research, not a recommended default. The full ramp mechanism is preserved in git history (search for "ramp_scaler" or "RampRef" in the commit log) if this is revisited.
9. **QSpice's `.qraw` output for a converted circuit was observed ~7x larger than LTspice's own `.raw` for the same nominal transient simulation** (30GB vs 4.3GB on one real circuit) — strongly suggestive of QSpice taking a much finer/smaller timestep than LTspice for the same circuit, which would also explain visually different-looking waveform features at the same "% complete" mark on two simulations that haven't finished yet. Not root-caused before this handoff — worth checking whether a `.tran` step-size parameter is being interpreted differently between the two, or whether a convergence issue in the converted circuit is forcing QSpice into much smaller adaptive steps.

---

## 7. Repository layout

```
ver2/
├── newtool.py              # the entire tool -- single file, ~5400 lines
├── newtool.spec             # PyInstaller build spec
├── HANDOFF.md                # this document
├── reference/
│   └── qspice_gate_library.qsch   # QSpice's own native gate shapes, used for
│                                     visual matching during gate synthesis
├── spicelib_vendor/          # vendored copy of spicelib 1.5.1 (GPL-3.0)
│   ├── VENDORED.md           # provenance notes -- why/how this was vendored
│   ├── LICENSE
│   └── editor/, sim/, raw/, scripts/, simulators/, log/, client_server/, utils/
└── dist/
    └── newtool.exe          # the built deliverable (not committed to git --
                                see .gitignore; rebuild via
                                `pyinstaller newtool.spec`)
```

To rebuild the exe after any source change:
```bash
python -m PyInstaller newtool.spec --noconfirm
```
(Close any running `newtool.exe` first — PyInstaller can't overwrite a locked file.)
