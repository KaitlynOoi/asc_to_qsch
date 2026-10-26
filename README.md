# asc_to_qsch — LTspice to QSPICE schematic converter

Convert LTspice `.asc` schematics into QSPICE `.qsch` files that open **and simulate** — not just a file-format translation, but a fix-up pass for the things QSPICE handles differently from LTspice.

> **Status: early prototype.** Tested on 5 real circuits so far. It works well on those, but expect rough edges on circuits it hasn't seen yet. Bug reports with example `.asc` files are the most helpful thing you can contribute — see [Contributing](#contributing).

Built on top of [spicelib](https://github.com/nunobrum/spicelib) by Nuno Brum.

---

## What it does

1. **Converts the schematic** — components, wires, net labels, orientations (including mirrored parts), values, and simulation directives.
2. **Finds missing models and libraries** — searches your existing LTspice installation (plus Documents / Desktop / Downloads) for the `.model`, `.subckt`, and `.asy` files your circuit uses. You can also point it at extra folders or pick files by hand.
3. **Fixes known LTspice → QSPICE incompatibilities**, for example:
   - LTspice ideal digital gates (`A` devices: AND/OR/XOR/NAND/NOR/XNOR/BUF/INV) are not supported by QSPICE → replaced with equivalent behavioral (`B`) sources
   - Nodes with several different net-label names are merged (LTspice does this automatically; QSPICE doesn't)
   - Op-amp pin order (`SpiceOrder`) is respected, so `+IN`/`-IN` don't get swapped
   - Comments vs. directives are kept distinct, so design notes aren't parsed as devices
   - 0 Ω resistors become wires; undamped standalone inductors get `Rser=1m`
4. **Shows you everything before writing** — every choice is listed for review, and nothing is saved until you confirm. A `.log` of the whole run is saved next to the output.

## Quick start

### Option A — Windows executable (no Python needed)

1. Download `conversion_tool.exe` from the [Releases](../../releases) page.
2. Double-click it to open the GUI, pick your `.asc` file, and click Run.

### Option B — run from source (Python 3.10+)

```bash
git clone https://github.com/KaitlynOoi/asc_to_qsch.git
cd asc_to_qsch
pip install -r requirements.txt
python conversion_tool.py                  # opens the GUI
python conversion_tool.py my_circuit.asc   # command-line mode
```

## Command-line usage

```
python conversion_tool.py INPUT.asc [OUTPUT.qsch] [options]
```

| Option | What it does |
|---|---|
| `-a PATH`, `--add PATH` | Extra folder to search for `.asy` symbol files (repeatable) |
| `--auto-search PATH` | Extra folder to search for `.model` / `.subckt` files (repeatable) |
| `--no-annotation-fix` | Don't clean up annotation text |
| `--no-zero-ohm-replacement` | Keep 0 Ω resistors as resistors |
| `--no-device-model-fix` | Skip the interactive device-model step |
| `--apply-uic-workaround` | Replace `.tran ... startup` with `uic` (off by default — see limitations) |

If `OUTPUT.qsch` is omitted, it's written next to the input with the same name.

## Known limitations

- **Stateful digital parts** (flip-flops, counters, Schmitt triggers) are not converted — swap in a QSPICE part by hand.
- **Capacitor `Lser` (ESL)** is dropped; QSPICE doesn't support it. Add a separate series inductor if you need it.
- **`.tran ... startup`** is silently ignored by QSPICE, so startup transients may look different from LTspice.
- **`.step` sweeps** may behave differently in QSPICE (open reports on the Qorvo forum).
- The `.exe` is Windows-only; the Python source should run anywhere, but model auto-discovery assumes standard Windows LTspice/QSPICE install paths.
- There is no automated test suite yet.

Full technical details — every workaround, each spicelib bug it patches, and open research items — are in [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md).

## Contributing

Contributions are very welcome — bug reports, test circuits, docs, and pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

**Easiest way to help:** run it on one of your circuits and [open an issue](../../issues/new/choose) with the `.asc` file and what went wrong (or right!).

## Project layout

```
conversion_tool.py         the whole tool (GUI + CLI), single file
conversion_tool.spec       PyInstaller build spec for the .exe
reference/                 QSPICE native gate shapes used during gate conversion
spicelib_vendor/           vendored copy of spicelib 1.5.1 (GPL-3.0)
docs/DEVELOPER_NOTES.md    deep-dive: architecture, function reference, bugs found
```

## License

[GPL-3.0](LICENSE). This project includes a vendored copy of [spicelib](https://github.com/nunobrum/spicelib) (also GPL-3.0), so the project as a whole is distributed under the same license.

*LTspice is a trademark of Analog Devices. QSPICE is a trademark of Qorvo. This project is not affiliated with either.*
