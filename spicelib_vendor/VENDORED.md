# Vendored copy of spicelib

Source: https://github.com/nunobrum/spicelib
Version vendored: 1.5.1 (PyPI, GPL-3.0 license -- see LICENSE in this directory)
Vendored on: 2026-08-10

This is a verbatim, unmodified copy of the `spicelib` package, kept locally
inside this project so `newtool.py` does not depend on the upstream GitHub
repository or PyPI package remaining available.

newtool.py imports this as `spicelib_vendor` (not `spicelib`) so it can never
be silently shadowed by, or confused with, a `pip install spicelib` package
that might also be present in the environment.

Any behavioral quirks/bugs in this vendored copy that newtool.py works around
are handled by newtool.py's own runtime monkey-patches (search newtool.py for
`_patch_`) -- this vendored copy itself is left untouched from upstream, so
diffing against a fresh copy of spicelib 1.5.1 stays meaningful.
