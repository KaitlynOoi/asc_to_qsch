# Contributing

Thanks for helping improve asc_to_qsch! This is an early-stage project, so every bug report and test circuit makes a real difference.

## Reporting a conversion problem

[Open an issue](../../issues/new/choose) and include:

- The `.asc` file (or a minimal one that reproduces the problem). Please remove anything confidential first.
- Any custom `.asy` / `.lib` / `.model` files it depends on, if you're able to share them.
- The `.log` file the tool writes next to the output `.qsch`.
- What you expected vs. what happened (a screenshot of the QSPICE error or schematic helps a lot).
- LTspice and QSPICE versions, and whether you used the `.exe` or the Python source.

Circuits that convert *correctly* are useful too — they become regression tests.

## Submitting a pull request

1. Fork the repo and create a branch: `git checkout -b fix/short-description`
2. Set up: `pip install -r requirements.txt`
3. Make your change in `conversion_tool.py`.
4. Test it on at least one real circuit, and open the result in QSPICE to confirm it simulates.
5. Open a PR describing what you changed, why, and which circuits you tested on.

Smaller, focused PRs are easier to review than big ones. If you're planning something large, open an issue first so we can talk it through.

### Ground rules for code changes

- **Don't edit `spicelib_vendor/` directly.** Bugs in spicelib are worked around with runtime patches in `conversion_tool.py` (search for `_patch_`), so the vendored copy stays diffable against upstream. If you find a genuine spicelib bug, consider reporting it [upstream](https://github.com/nunobrum/spicelib/issues) too.
- **Keep the CLI and GUI in sync.** Both drive the same `convert_asc_to_qsch()` / `process_models()` functions through callback hooks — conversion logic shouldn't depend on which interface is used.
- **Don't hardcode paths** to a specific machine's LTspice/QSPICE install.
- If your change affects behavior described in `docs/DEVELOPER_NOTES.md`, update it there.

## Good first contributions

- Test circuits (especially ones using op-amps, transformers, digital logic, or vendor models)
- An automated test suite — QSPICE can run headless (`QSPICE64.exe -o out.log netlist.net`), see section 6.5 of the developer notes
- Capacitor `Lser` → separate series inductor (section 6.1 of the developer notes)
- macOS/Linux support for model auto-discovery (e.g. LTspice under Wine)

## Building the Windows .exe

```bash
python -m PyInstaller conversion_tool.spec --noconfirm
```

The output is `dist/conversion_tool.exe`. Close any running copy first.

## License

By contributing, you agree that your contributions are licensed under the project's [GPL-3.0 license](LICENSE).
