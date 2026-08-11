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

1. ~~**Digital gate connectivity resolution can fail with no clear cause.**~~ **Likely root-caused and fixed** (see section 4.4a): the wire-tracing fallback's rotation math only handled QSCH orientation codes 0-7, silently miscalculating pin positions for any mirrored component (codes 8-15). If the circuit that originally hit this had any mirrored gates, this is almost certainly why. Worth a quick re-test against that original circuit to confirm, but not re-verified against that specific file before this handoff.
2. **Capacitor ESL (`Lser`) is dropped, not converted.** Since QSpice doesn't support it as a parameter, the electrically-correct fix (per QSpice's own author) is to insert a real, separate series inductor component in the netlist next to the capacitor. That's a topology change (not just a text change), was scoped out of this session for time/risk reasons, and is not yet implemented.
3. **Resistor parasitic parameters were not investigated.** Only capacitor/inductor `Rser` handling was researched and fixed; whether resistors support any equivalent parasitic syntax in QSpice is unknown.
4. **The `.ic` "asserted via a soft tie" behavior reported on the Qorvo forum was not independently verified.** One forum thread claims QSpice implements `.ic` by tying a node through a small resistor rather than as a hard constraint, which could matter on high-impedance nodes — this project could not confirm the exact mechanism, so it's flagged as unverified rather than acted on.
5. **`.step` parameter sweeps** have multiple open/unresolved Qorvo forum reports of being silently ignored in some netlists, or not working when the stepped parameter feeds a `.model` parameter. Not addressed by this tool at all — worth a standing checklist item for any circuit that uses `.step`.
6. **No fully automated real-QSpice-engine testing.** All simulation-level verification currently depends on either the (partially unreliable) `QSpice_MCP` tooling or a human manually running QSpice's real GUI. A more robust CI-style verification path (if QSpice ever gets a reliable headless/batch mode) would remove a lot of manual back-and-forth.
7. **`spicelib` is vendored under its GPL-3.0 license.** This project copied the whole package into `spicelib_vendor/` for future-proofing (see `spicelib_vendor/VENDORED.md`), but the copyleft licensing implications of shipping/distributing this tool have not been resolved — that's a legal/compliance decision for whoever owns distribution of this project, not something decided in code.
8. **The `build/` directory** (PyInstaller's intermediate artifacts) was persistently locked/undeleteable in this environment ("Device or resource busy") across multiple attempts. It's excluded from git via `.gitignore` and is safe to delete manually via File Explorer if it's ever in the way — it regenerates automatically on the next `pyinstaller newtool.spec` build.
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
