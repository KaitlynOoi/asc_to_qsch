#!/usr/bin/env python3
"""
qspice_combined_tool.py

ONE file that does everything safely: pick a single original LTspice .asc file,
edit your custom output .qsch name, click Run, inspect & approve detected model candidates,
and get a fully converted + verified QSpice .qsch file ready to simulate.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

# Vendored copy of spicelib (see spicelib_vendor/VENDORED.md) -- imported
# as "spicelib_vendor", never "spicelib", so this can never be silently
# shadowed by (or confused with) a pip-installed spicelib package that
# might also be present. Keeps this tool working even if the upstream
# spicelib GitHub repo/PyPI package ever goes away. Every class actually
# used below is imported explicitly by name from its real submodule (e.g.
# RawRead is imported directly at its one point of use further down) --
# there is no top-level "import spicelib_vendor" of any kind, aliased or
# not, since nothing in this file ever needs the package object itself.
from spicelib_vendor.editor.spice_editor import SpiceCircuit

# --- Compatibility Patch for spicelib version mismatches ---
def _always_updated(self):
    return True

SpiceCircuit.updated = _always_updated

try:
    from spicelib_vendor.editor.asc_editor import AscEditor
    AscEditor.updated = _always_updated
except ImportError:
    AscEditor = None

try:
    from spicelib_vendor.editor.qsch_editor import (
        QschEditor,
        QschTag,
        QSCH_TEXT_COMMENT,
        QSCH_TEXT_INSTR_QUALIFIER,
        QSCH_TEXT_STR_ATTR,
    )
    QschEditor.updated = _always_updated
except ImportError:
    QschEditor = None
    QschTag = None
    QSCH_TEXT_COMMENT = 4
    QSCH_TEXT_INSTR_QUALIFIER = "﻿"
    QSCH_TEXT_STR_ATTR = 8

from spicelib_vendor.editor.asy_reader import AsyReader
from spicelib_vendor.editor.base_schematic import Point, TextTypeEnum
from spicelib_vendor.utils.file_search import find_file_in_directory

# --- Fix: AsyReader.to_qsch() produces broken ARC geometry ---
#
# spicelib's AsyReader.to_qsch() (used whenever a component resolves via a
# real .asy file -- this is the path logic gates, and any other symbol
# drawn with curved outlines, go through) has a confirmed bug in how it
# converts LTspice's 4-point ARC primitive into QSCH's "arc3p" tag.
#
# Proof this is a spicelib bug, not something in our own code: spicelib
# itself contains a SECOND, different ARC-to-arc3p conversion (in
# qsch_editor.py's copy_from, used for schematic-level ARC elements) that
# computes proper absolute points via bounding-box + angle trigonometry.
# AsyReader.to_qsch's version instead normalizes the arc's start/end points
# into unit-vector-ish fractions and then multiplies by the LTspice->QSCH
# scale factor alone -- producing tiny, nonsensical coordinates (e.g. "-4,4"
# next to a "100,-350" center) instead of real absolute positions in the
# same coordinate space as the rest of the symbol. This is exactly the kind
# of degenerate, near-the-origin geometry that shows up as a missing or
# collapsed arc/curve in the converted symbol -- LTspice's built-in logic
# gates (AND/OR/etc.) draw their curved body outlines with this same ARC
# primitive, so they hit this bug on every conversion.
#
# The fix below replaces AsyReader.to_qsch with a corrected version that's
# identical except the ARC branch, which now uses the same validated
# trigonometric formula as qsch_editor.py's own (working) ARC conversion.
import math as _math_for_asy_patch
from spicelib_vendor.editor.asy_reader import AsyReader as _AsyReaderToPatch
from spicelib_vendor.editor.qsch_editor import QschTag as _QschTagForAsyPatch

_ASY_SCALE_X = 6.25
_ASY_SCALE_Y = -6.25


def _corrected_asy_to_qsch(self, *args):
    spice_prefix = self.attributes['Prefix']
    symbol = _QschTagForAsyPatch("symbol", spice_prefix[0])
    symbol.items.append(_QschTagForAsyPatch("type:", spice_prefix))
    symbol.items.append(_QschTagForAsyPatch("description:", self.attributes.get("Description", "")))
    symbol.items.append(_QschTagForAsyPatch("shorted pins:", "false"))

    for line in self.lines:
        x1 = int(line.V1.X * _ASY_SCALE_X)
        y1 = int(line.V1.Y * _ASY_SCALE_Y)
        x2 = int(line.V2.X * _ASY_SCALE_X)
        y2 = int(line.V2.Y * _ASY_SCALE_Y)
        segment, _ = _QschTagForAsyPatch.parse(
            f"\u00abline ({x1},{y1}) ({x2},{y2}) 0 0 0x1000000 -1 -1\u00bb"
        )
        symbol.items.append(segment)

    for shape in self.shapes:
        if shape.name == "RECTANGLE":
            x1 = int(shape.points[0].X * _ASY_SCALE_X)
            y1 = int(shape.points[0].Y * _ASY_SCALE_Y)
            x2 = int(shape.points[1].X * _ASY_SCALE_X)
            y2 = int(shape.points[1].Y * _ASY_SCALE_Y)
            shape_tag, _ = _QschTagForAsyPatch.parse(
                f"\u00abrect ({x1},{y1}) ({x2},{y2}) 0 0 0 0x4000000 0x1000000 -1 0 -1\u00bb"
            )
        elif shape.name == "ARC":
            # LTspice ARC format: points[0..1] = bounding box corners,
            # points[2..3] = start/end points of the arc (roughly on the
            # curve). Convert to QSCH arc3p (start, end, center) using
            # real trigonometry -- matching qsch_editor.py's own working
            # ARC conversion -- instead of the original code's broken
            # normalized-unit-vector shortcut.
            pts = shape.points
            center_x = (pts[0].X + pts[1].X) / 2.0
            center_y = (pts[0].Y + pts[1].Y) / 2.0
            ellipse_width = abs(pts[1].X - pts[0].X)
            ellipse_height = abs(pts[1].Y - pts[0].Y)

            start_angle = _math_for_asy_patch.atan2(pts[2].Y - center_y, pts[2].X - center_x)
            end_angle = _math_for_asy_patch.atan2(pts[3].Y - center_y, pts[3].X - center_x)

            start_x = center_x + ellipse_width / 2.0 * _math_for_asy_patch.cos(start_angle)
            start_y = center_y + ellipse_height / 2.0 * _math_for_asy_patch.sin(start_angle)
            end_x = center_x + ellipse_width / 2.0 * _math_for_asy_patch.cos(end_angle)
            end_y = center_y + ellipse_height / 2.0 * _math_for_asy_patch.sin(end_angle)

            sx1 = int(start_x * _ASY_SCALE_X)
            sy1 = int(start_y * _ASY_SCALE_Y)
            sx2 = int(end_x * _ASY_SCALE_X)
            sy2 = int(end_y * _ASY_SCALE_Y)
            sx3 = int(center_x * _ASY_SCALE_X)
            sy3 = int(center_y * _ASY_SCALE_Y)
            shape_tag, _ = _QschTagForAsyPatch.parse(
                f"\u00abarc3p ({sx1},{sy1}) ({sx2},{sy2}) ({sx3},{sy3}) 0 0 0xff0000 -1 -1\u00bb"
            )
        elif shape.name == "CIRCLE" or shape.name == "ellipse":
            x1 = int(shape.points[0].X * _ASY_SCALE_X)
            y1 = int(shape.points[0].Y * _ASY_SCALE_Y)
            x2 = int(shape.points[1].X * _ASY_SCALE_X)
            y2 = int(shape.points[1].Y * _ASY_SCALE_Y)
            shape_tag, _ = _QschTagForAsyPatch.parse(
                f"\u00abellipse ({x1},{y1}) ({x2},{y2}) 0 0 0 0x1000000 0x1000000 -1 -1\u00bb"
            )
        else:
            raise ValueError(f"Shape {shape.name} not supported")
        symbol.items.append(shape_tag)

    for i, attr in enumerate(self.windows):
        coord = attr.coord
        x = coord.X * _ASY_SCALE_X
        y = coord.Y * _ASY_SCALE_Y
        text, _ = _QschTagForAsyPatch.parse(
            f'\u00abtext ({x:.0f},{y:.0f}) 1 7 0 0x1000000 -1 -1 "{args[i]}"\u00bb'
        )
        symbol.items.append(text)

    # LTspice's .asy PIN entries are declared in whatever order is visually
    # convenient for the symbol artwork -- not necessarily the order the
    # underlying .SUBCKT/.MODEL expects. LTspice itself corrects for this
    # using the optional "PINATTR SpiceOrder n" attribute on each pin (per
    # LTwiki: if present, it -- not drawing order -- determines the node
    # order used when generating the SPICE X-line; if omitted on a pin,
    # drawing order is used). spicelib's upstream to_qsch() parses
    # SpiceOrder into each pin's attr_dict but never reads it back out, so
    # every converted symbol emitted its \u00abpin\u00bb tags in raw drawing order.
    # Confirmed with a real AD743 test conversion: our own netlist generator
    # emitted "U1 MINUSIN PLUSIN ..." (drawing order: -IN then +IN) instead
    # of the "+IN -IN ..." order AD743's real .SUBCKT requires -- silently
    # swapping the op-amp's inverting/non-inverting inputs. A library scan
    # found this affects the majority of real op-amp/comparator .asy files
    # (e.g. AD743, LM393, OPA170 all declare -IN before +IN but give +IN the
    # lower SpiceOrder), so this is corrected generally here rather than for
    # any one part. Only reorders when every pin on the symbol declares a
    # SpiceOrder; if any pin omits it, drawing order is kept, matching
    # LTspice's own fallback so partially-annotated symbols aren't scrambled.
    def _pin_spice_order(pin):
        for pair in pin.text.split(";"):
            if pair.startswith("SpiceOrder="):
                try:
                    return int(pair.split("=", 1)[1])
                except ValueError:
                    return None
        return None

    _pin_orders = [_pin_spice_order(p) for p in self.pins]
    if self.pins and all(o is not None for o in _pin_orders):
        ordered_pins = [p for _, p in sorted(zip(_pin_orders, self.pins), key=lambda t: t[0])]
    else:
        ordered_pins = self.pins

    for pin in ordered_pins:
        coord = pin.coord
        attr_dict = {}
        for pair in pin.text.split(";"):
            if '=' in pair:
                k, v = pair.split('=')
                attr_dict[k] = v
        pin_tag, _ = _QschTagForAsyPatch.parse(
            f'\u00abpin ({coord.X * _ASY_SCALE_X:.0f},{coord.Y * _ASY_SCALE_Y:.0f}) (0,0)'
            f' 1 0 0 0x1000000 -1 "{attr_dict["PinName"]}"\u00bb'
        )
        symbol.items.append(pin_tag)

    return symbol


_AsyReaderToPatch.to_qsch = _corrected_asy_to_qsch
# ---------------------------------------------------------

BUILTIN_PRIMITIVES = {
    "res",
    "cap",
    "ind",
    "voltage",
    "current",
    "diode",
    "schottky",
    "npn",
    "pnp",
    "nmos",
    "pmos",
    "zener",
    "polcap",
}

LTSPICE_GENERIC_DIGITAL_PRIMITIVES = {
    "and", "or", "nand", "nor", "xor", "xnor", "buf", "inv", "not", "dff", "schmitt",
}
"""LTspice's own fixed vocabulary of generic digital-gate placeholder symbol
names (Digital\\and.asy, Digital\\or.asy, ...). This is NOT a guess about
what QSpice ships -- it exists only to stop find_model_candidates from doing
a blind exact-name text search for these short, generic words, which is
unsafe: e.g. an unrelated analog-switch macromodel library was found to
define its own internal helper card literally named ".model OR D(...)" with
no relation to a logic gate, and a plain name search would hand that back as
if it were a real match. QSpice's native gate library uses a different
naming convention (AND2, AND2_Q for NAND, input-count variants, etc.) with
no reliable 1:1 mapping from these LTspice names, so components in this set
are always routed to the user to manually pick the matching QSpice-native
gate instead of being auto-searched or silently assumed resolved.
"""

GENERIC_OPAMP_SYMBOLS = {
    "opamp",
    "opamp2",
    "opamp2_a",
    "eamp",
    "subckt",
    "singleopamp",
    "dualopamp",
    "quadopamp",
}

def _find_qspice_install_dir() -> Optional[str]:
    """Locates the QSpice installation directory, if any. Never hardcodes a
    single fixed path as ground truth -- checks the usual Program Files
    locations (via env vars, which respect 32/64-bit redirection) and falls
    back to the Windows "App Paths" registry entry QSpice's installer
    registers, so this keeps working across machines/installs without
    needing a code change."""
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, "QSPICE"))
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    try:
        import winreg
        for subkey in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QSPICE64.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QSPICE.exe",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                    exe_path, _ = winreg.QueryValueEx(key, "")
                    install_dir = os.path.dirname(exe_path)
                    if os.path.isdir(install_dir):
                        return install_dir
            except OSError:
                continue
    except ImportError:
        pass
    return None


_qspice_native_symbol_cache: Optional[Set[str]] = None


def _get_qspice_native_symbol_names() -> Set[str]:
    """Returns the set of component names QSpice genuinely ships built-in,
    discovered by scanning the real installed QSpice library on disk (its
    .qsym symbol files) rather than trusting a hand-maintained guess list --
    that guess-list approach was proven wrong (it silently skipped model
    resolution for parts like 2N3904/2N3906/1N4148/LT1001/BAT54 that QSpice
    does not actually ship). The "Examples" folder is excluded because it
    bundles demo-specific symbols (e.g. Examples/1N4148.qsym) that are not
    part of the general library and would reintroduce the same bug.
    Result is cached for the process lifetime; if QSpice isn't found on this
    machine, returns an empty set so nothing is ever wrongly assumed native.
    """
    global _qspice_native_symbol_cache
    if _qspice_native_symbol_cache is not None:
        return _qspice_native_symbol_cache

    names: Set[str] = set()
    install_dir = _find_qspice_install_dir()
    if install_dir:
        for dirpath, dirnames, filenames in os.walk(install_dir):
            dirnames[:] = [d for d in dirnames if d.lower() != "examples"]
            for fname in filenames:
                if fname.lower().endswith(".qsym"):
                    names.add(os.path.splitext(fname)[0].lower())
    _qspice_native_symbol_cache = names
    return names


def is_qspice_local_library_component(model_name: str) -> bool:
    """Checks if a model name is either (a) one of LTspice's generic
    placeholder symbol types (res/cap/npn/diode/...), which map directly to
    QSpice's built-in primitive device letters, or (b) a component QSpice
    genuinely ships natively, verified by scanning the real installed
    QSpice library on disk. Nothing here is a guessed/hardcoded list of real
    part numbers -- if QSpice isn't installed or a part isn't found in its
    library, this returns False and the part goes through the normal
    search/prompt flow instead of being silently skipped.
    """
    if not model_name:
        return False
    bare = bare_model_name(model_name).lower()
    if bare in BUILTIN_PRIMITIVES:
        return True
    return bare in _get_qspice_native_symbol_names()

ZERO_OHM_TOLERANCE = 1e-15

# Standard SPICE primitive device types
SPICE_PRIMITIVES = {
    'D',     # Diode
    'NPN',   # NPN BJT
    'PNP',   # PNP BJT
    'NMOS',  # N-Channel MOSFET
    'PMOS',  # P-Channel MOSFET
    'NJF',   # N-Channel JFET
    'PJF',   # P-Channel JFET
    'SW',    # Voltage-Controlled Switch
    'CSW',   # Current-Controlled Switch
    'RES',   # Semiconductor Resistor
    'CAP',   # Semiconductor Capacitor
    'IND'    # Non-linear Inductor
}


def classify_spice_model(model_text: str) -> dict:
    """
    Classifies SPICE text based on syntax tokens rather than just line counts.
    Distinguishes primitive .model statements (Diode, BJT, MOSFET) from .subckt blocks.
    """
    lines = [line.strip() for line in model_text.splitlines() if line.strip() and not line.startswith('*')]
    clean_text = "\n".join(lines)
    
    # 1. Check if it's a Subcircuit (.subckt) -> Always External File
    if re.search(r'^\s*\.subckt\b', clean_text, re.IGNORECASE | re.MULTILINE):
        return {
            "action": "INCLUDE_FILE",
            "reason": "Subcircuit macromodel (.subckt detected)"
        }
    
    # 2. Check if it's a Primitive Model (.model)
    # Match pattern: .model <model_name> <model_type>(...
    model_match = re.search(
        r'^\s*\.model\s+(?P<name>\S+)\s+(?P<type>[A-Za-z0-9]+)', 
        clean_text, 
        re.IGNORECASE | re.MULTILINE
    )
    
    if model_match:
        device_type = model_match.group("type").upper()
        
        # If it's a recognized primitive (Diode, BJT, MOSFET, etc.)
        if device_type in SPICE_PRIMITIVES:
            return {
                "action": "INLINE_DIRECTIVE",
                "reason": f"Primitive SPICE model ({device_type})"
            }
    
    # 3. Fallback: If it contains multiple .model statements or is excessively large
    model_count = len(re.findall(r'^\s*\.model\b', clean_text, re.IGNORECASE | re.MULTILINE))
    if model_count > 1 or len(lines) > 25:
        return {
            "action": "INCLUDE_FILE",
            "reason": "Multi-model library or non-primitive block"
        }
        
    # Default fallback for simple short statements
    return {
        "action": "INLINE_DIRECTIVE",
        "reason": "Single simple directive"
    }


def find_coupled_inductors(text_content: str) -> Set[str]:
    """Finds all inductor references (e.g. L1, L2, L_PRI) coupled in 'K' mutual inductance directives."""
    coupled = set()
    for line in text_content.splitlines():
        line_clean = line.strip()
        if re.search(r'^\s*\.?K\w*\b', line_clean, re.IGNORECASE):
            tokens = line_clean.split()
            for tok in tokens[1:]:
                tok_clean = re.sub(r'[^a-zA-Z0-9_]', '', tok)
                if tok_clean.upper().startswith('L'):
                    coupled.add(tok_clean.upper())
    return coupled


def get_default_search_roots(asc_path: Optional[str] = None) -> List[str]:
    """Generates an ordered list of search directories, prioritizing local and official lib paths."""
    user_home = Path.home()
    priority_roots = []
    if asc_path:
        asc_dir = Path(asc_path).parent
        if asc_dir.exists():
            priority_roots.append(asc_dir)

    ltspice_libs = [
        user_home / "AppData/Local/LTspice/lib",
        user_home / "Documents/LtspiceXVII/lib",
        user_home / "Library/Application Support/LTspice/lib",
    ]

    broad_user_dirs = [
        user_home / "Documents",
        user_home / "Desktop",
        user_home / "Downloads",
    ]

    all_roots = priority_roots + ltspice_libs + broad_user_dirs
    valid_roots = []
    for r in all_roots:
        sp = str(r.resolve()) if r.exists() else str(r)
        if os.path.isdir(sp) and sp not in valid_roots:
            valid_roots.append(sp)

    return valid_roots


DEFAULT_SEARCH_ROOTS = get_default_search_roots()


def force_save_qsch(qsch_editor, path: str):
    """Forces QschEditor to write to disk, falling back to direct string serialization if spicelib skips.

    QschEditor.save_as() only writes when self.updated is True, but `updated`
    is a plain per-instance attribute set False on load -- not a property --
    so the module-level `QschEditor.updated = _always_updated` patch above
    can't override it (instance attributes shadow plain class attributes;
    only a real descriptor like @property would be intercepted). It must be
    set on the instance directly, here, before saving.
    """
    qsch_editor.updated = True
    qsch_editor.canvas_updated = True
    try:
        qsch_editor.was_modified = True
    except Exception:
        pass

    try:
        qsch_editor.save_netlist(path)
    except Exception:
        pass

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        if hasattr(qsch_editor, "schematic") and qsch_editor.schematic is not None:
            with open(path, "w", encoding="cp1252", errors="replace") as f:
                f.write(str(qsch_editor.schematic))


def _patch_spicelib_qsch_colon_parsing(log: Callable[[str], None] = print) -> bool:
    """Fix a parsing bug in spicelib's QschTag.parse()."""
    try:
        import spicelib_vendor.editor.qsch_editor as _qsch_mod
    except ImportError:
        return False
    QschTag_cls = getattr(_qsch_mod, "QschTag", None)
    smart_split = getattr(_qsch_mod, "smart_split", None)
    if QschTag_cls is None or smart_split is None:
        return False
    if getattr(QschTag_cls, "_colon_quote_patch_applied", False):
        return True

    @classmethod
    def _patched_parse(cls, stream: str, start: int = 0):
        self = cls()
        assert stream[start] == "«"
        i = start + 1
        i0 = i
        stop = None
        while i < len(stream):
            if stream[i] == "«":
                child, i = cls.parse(stream, i)
                i0 = i + 1
                self.items.append(child)
            elif stream[i] == '"':
                i += 1
                while i < len(stream) and stream[i] != '"':
                    i += 1
            elif stream[i] == "»":
                stop = i + 1
                break
            elif stream[i] == "\n":
                if i > i0:
                    self.tokens.extend(smart_split(stream[i0:i]))
                i0 = i + 1
            i += 1
        else:
            raise OSError("Missing » when reading file")
        line = stream[i0:i]
        if ": " in line and '"' not in line:
            name, text = line.split(": ", 1)
            self.tokens.append(name + ":")
            self.tokens.append(text)
        else:
            self.tokens.extend(smart_split(line))
        return self, stop

    try:
        QschTag_cls.parse = _patched_parse
        QschTag_cls._colon_quote_patch_applied = True
    except Exception:
        return False
    return True


_patch_spicelib_qsch_colon_parsing(log=lambda _msg: None)


def _clean_path(path: str) -> str:
    """Remove accidental whitespace, non-breaking spaces, quotes and normalize path separators."""
    cleaned = str(path).replace("\xa0", " ").strip()
    cleaned = cleaned.strip('"').strip("'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned) if "\n" not in cleaned else cleaned
    return os.path.normpath(cleaned)


def _is_zero_ohm_value(value: object) -> bool:
    """Best-effort detection of a literal 0-ohm resistor value."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return abs(float(value)) <= ZERO_OHM_TOLERANCE
    text = (
        str(value).strip().replace("Ω", "").replace("ohm", "").replace("OHM", "").strip()
    )
    if not text:
        return False
    text_clean = re.sub(r"^[0\.]+[rR]?[0]*$", "0", text)
    try:
        return abs(float(text_clean)) <= ZERO_OHM_TOLERANCE
    except ValueError:
        return False


def bare_model_name(symtype: str) -> str:
    if not symtype:
        return ""
    text = str(symtype).strip(' "\'')
    if not text:
        return ""
    first_token = text.split()[0]
    if "=" in first_token:
        return ""
    name = re.split(r"[\\/]", first_token)[-1]
    if name and re.search(r"[=()*+/]", name):
        return ""
    return name


def flatten_model_statement(text: str) -> str:
    lines = text.splitlines()
    clean_lines = []
    for l in lines:
        l_clean = re.sub(r"\s+[;\*].*$", "", l).strip()
        if l_clean and not l_clean.startswith("*") and not l_clean.startswith(";"):
            clean_lines.append(l_clean)
    if not clean_lines:
        return text.strip()

    if clean_lines[0].lower().startswith(".model"):
        parts = [clean_lines[0]]
        for cl in clean_lines[1:]:
            if cl.startswith("+"):
                parts.append(cl[1:].strip())
            else:
                parts.append(cl)
        return " ".join(parts)
    return "\n".join(clean_lines)


def parse_asc_ref_to_symbol(asc_path: str) -> Dict[str, str]:
    lines = _read_text_file_robust(asc_path)
    if not lines:
        return {}
    data = "".join(lines)

    mapping: Dict[str, str] = {}
    blocks = re.split(r"(?=^SYMBOL )", data, flags=re.MULTILINE)
    for block in blocks:
        if not block.startswith("SYMBOL"):
            continue
        tokens = block.split()
        if len(tokens) < 2:
            continue
        symtype = tokens[1]

        ref_match = re.search(r"SYMATTR InstName (\S+)", block)
        if not ref_match:
            continue
        ref = ref_match.group(1)

        val_match = re.search(r"SYMATTR Value (\S+)", block)
        val2_match = re.search(r"SYMATTR Value2 (\S+)", block)
        spicemodel_match = re.search(r"SYMATTR SpiceModel (\S+)", block)

        val = val_match.group(1) if val_match else None
        val2 = val2_match.group(1) if val2_match else None
        spicemodel = spicemodel_match.group(1) if spicemodel_match else None

        bare_sym = bare_model_name(symtype).lower()

        if spicemodel and bare_model_name(spicemodel):
            mapping[ref] = bare_model_name(spicemodel)
        elif val2 and (
            not val
            or val.lower() in GENERIC_OPAMP_SYMBOLS
            or val.lower() == bare_sym
        ):
            mapping[ref] = bare_model_name(val2)
        elif val and val.lower() not in GENERIC_OPAMP_SYMBOLS:
            mapping[ref] = bare_model_name(val)
        else:
            mapping[ref] = bare_model_name(symtype)

    return mapping


def parse_qsch_x_type_refs(qsch_path: str) -> Set[str]:
    lines = _read_text_file_robust(qsch_path)
    if not lines:
        return set()
    data = "".join(lines)

    x_refs: Set[str] = set()
    comp_blocks = re.split(r"(?=«component )", data)
    ref_text_pattern = re.compile(
        r'«text \([^)]*\) 1 7 0 0x1000000 -1 -1 "([^"]+)"»'
    )
    for block in comp_blocks:
        if not block.startswith("«component"):
            continue
        if "«type: X»" not in block:
            continue
        texts = ref_text_pattern.findall(block)
        if texts:
            x_refs.add(texts[0])
    return x_refs


def _attr(obj: object, *names: str, default=None):
    attrs = getattr(obj, "attributes", None) or {}
    for name in names:
        if isinstance(attrs, dict) and name in attrs:
            val = attrs[name]
            if val not in (None, ""):
                return val
    for name in names:
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val not in (None, ""):
                return val
    return default


def _read_text_file_robust(file_path: str) -> Optional[List[str]]:
    # UTF-16 is only attempted when the file actually starts with a real
    # UTF-16 BOM. Without this guard, "try each encoding, accept whichever
    # doesn't raise" is unsound: UTF-16 decoding rarely raises on arbitrary
    # binary (e.g. QSpice's native .qsch files start with a 4-byte binary
    # signature, FF D8 FF DB, ahead of the plain cp1252/latin1 text body),
    # so it can silently "succeed" on a wrong decode purely by luck of the
    # byte sequence, corrupting every downstream parse of that file.
    try:
        with open(file_path, "rb") as f:
            head = f.read(2)
    except Exception:
        head = b""
    if head == b"\xff\xfe":
        encodings = ("utf-16-le", "utf-8-sig", "cp1252", "latin1")
    elif head == b"\xfe\xff":
        encodings = ("utf-16-be", "utf-8-sig", "cp1252", "latin1")
    else:
        encodings = ("utf-8-sig", "cp1252", "latin1")
    for encoding in encodings:
        try:
            with open(file_path, encoding=encoding) as f:
                content = f.read()
                if "\x00" in content:
                    continue
                return content.splitlines(keepends=True)
        except Exception:
            continue
    try:
        with open(file_path, encoding="cp1252", errors="replace") as f:
            lines = f.readlines()
            return [l.replace("\x00", "") for l in lines]
    except Exception:
        return None


_MODEL_CANDIDATE_INDEX_CACHE: Dict[Tuple[str, ...], Dict[str, List[str]]] = {}


def _clear_model_candidate_cache() -> None:
    """Drops every cached directory-scan index built by
    find_model_candidates(). Called once at the start of process_models()
    so each separate conversion re-scans the filesystem fresh (picking up
    any library file added since the last conversion), while every
    find_model_candidates() call WITHIN that one conversion -- one per
    missing model, plus any redo during the review step -- reuses the same
    index instead of re-walking the same directories from scratch each
    time.
    """
    _MODEL_CANDIDATE_INDEX_CACHE.clear()


def _build_model_candidate_index(search_roots: Iterable[str]) -> Dict[str, List[str]]:
    """Walks every search root ONCE, reading each matching-extension file
    ONCE, and indexes every ".model"/".subckt" name found by its
    Q-stripped uppercase form -> list of file paths that define it.

    This replaces find_model_candidates' original approach of re-walking
    every search root and re-reading every file from scratch for EACH
    model being looked up -- confirmed by profiling a real conversion:
    with 6 missing models, that meant the exact same multi-thousand-file
    directory tree got scanned 6 separate times, accounting for 88.6 of
    93.8 total seconds (94% of the whole conversion's runtime). Building
    one index up front and looking model names up in it afterward does
    the identical filesystem work exactly once, independent of how many
    models need resolving.

    Matching rule preserved exactly from the original per-call scan: a
    file "matches" a target if its own found name, with any leading "Q"
    characters stripped, equals the target's name with leading "Q"s
    stripped the same way (the original's extra "found_token == target"
    check was strictly redundant -- if two strings are equal, stripping
    the same leading characters from both is trivially still equal -- so
    indexing by the Q-stripped form alone reproduces the original result
    set exactly).
    """
    card_re = re.compile(r"^\s*\.(?:subckt|model)\s+([^\s\(\);]+)", re.IGNORECASE)
    skip_dirs = {
        "windows",
        "$recycle.bin",
        "system volume information",
        "node_modules",
        "appdata/local/temp",
    }

    index: Dict[str, List[str]] = defaultdict(list)
    seen_files: Set[str] = set()

    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]

            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (
                    ".lib",
                    ".cir",
                    ".txt",
                    ".sub",
                    ".mod",
                    ".inc",
                    ".prm",
                    ".bjt",
                    ".dio",
                    ".mos",
                ):
                    continue
                fpath = os.path.normpath(os.path.join(dirpath, fname))
                if fpath in seen_files:
                    continue
                seen_files.add(fpath)
                lines = _read_text_file_robust(fpath)
                if not lines:
                    continue
                keys_in_this_file: Set[str] = set()
                for line in lines:
                    line_str = line.strip()
                    if (
                        not line_str
                        or line_str.startswith("*")
                        or line_str.startswith(";")
                    ):
                        continue
                    m = card_re.match(line_str)
                    if m:
                        found_raw = m.group(1).strip('"\'')
                        found_key = bare_model_name(found_raw).upper().lstrip("Q")
                        keys_in_this_file.add(found_key)
                for key in keys_in_this_file:
                    index[key].append(fpath)
    return dict(index)


def find_model_candidates(
    model_name: str, search_roots: Iterable[str]
) -> List[str]:
    target = bare_model_name(model_name).upper()

    if (
        not target
        or is_qspice_local_library_component(target)
        or target.lower() in GENERIC_OPAMP_SYMBOLS
    ):
        return []

    target_no_q = target.lstrip("Q")
    roots_key = tuple(sorted({os.path.normpath(r) for r in search_roots if r}))
    index = _MODEL_CANDIDATE_INDEX_CACHE.get(roots_key)
    if index is None:
        index = _build_model_candidate_index(search_roots)
        _MODEL_CANDIDATE_INDEX_CACHE[roots_key] = index
    return list(index.get(target_no_q, []))


def _extract_model_definition(
    file_path: str, model_name: str, log: Optional[Callable[[str], None]] = None
) -> Optional[str]:
    target = bare_model_name(model_name).upper()
    target_no_q = target.lstrip("Q")

    lines = _read_text_file_robust(file_path)
    if not lines:
        return None

    card_re = re.compile(r"^\s*\.(model|subckt)\s+([^\s\(\);]+)", re.IGNORECASE)

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        line_str = line.strip()
        if not line_str or line_str.startswith("*") or line_str.startswith(";"):
            idx += 1
            continue

        m = card_re.match(line_str)
        if m:
            found_raw = m.group(2).strip('"\'' )
            found_token = bare_model_name(found_raw).upper()
            found_no_q = found_token.lstrip("Q")

            if found_token == target or found_no_q == target_no_q:
                kind = m.group(1).lower()
                if kind == "model":
                    collected = [line_str]
                    clean_line = re.sub(r"\s+[;\*].*$", "", line_str)
                    depth = clean_line.count("(") - clean_line.count(")")
                    j = idx + 1
                    while j < len(lines):
                        nxt = lines[j].strip()
                        if not nxt or nxt.startswith("*") or nxt.startswith(";"):
                            j += 1
                            continue
                        clean_nxt = re.sub(r"\s+[;\*].*$", "", nxt)
                        if depth > 0 or nxt.startswith("+"):
                            collected.append(nxt)
                            depth += clean_nxt.count("(") - clean_nxt.count(")")
                            j += 1
                        else:
                            break
                    raw_model = "\n".join(collected)
                    return flatten_model_statement(raw_model)

                elif kind == "subckt":
                    ends_re = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
                    collected = [line_str]
                    j = idx + 1
                    while j < len(lines):
                        nxt = lines[j]
                        collected.append(nxt.strip())
                        if ends_re.match(nxt):
                            break
                        j += 1
                    return "\n".join(collected)
        idx += 1

    return None


def _inject_raw_instruction(
    qsch_editor, instruction: str, log: Callable[[str], None] = print
) -> None:
    from spicelib_vendor.editor.qsch_editor import QSCH_TEXT_INSTR_QUALIFIER, QschTag

    instruction = instruction.strip()
    x, y = qsch_editor._get_text_space()
    tag = QschTag()
    tag.tokens = [
        "text",
        f"({x},{y})",
        "1",
        "0",
        "0",
        "0x1000000",
        "-1",
        "-1",
        f'"{QSCH_TEXT_INSTR_QUALIFIER}{instruction}"',
    ]
    qsch_editor.schematic.items.append(tag)
    qsch_editor.canvas_updated = True
    log(f"  Injected (safe): {instruction}")


def _inject_lib_instruction(
    qsch_editor,
    path: str,
    kind: str = "lib",
    log: Callable[[str], None] = print,
) -> None:
    clean_path = _clean_path(path)
    instruction = f'.{kind} "{clean_path}"'
    _inject_raw_instruction(qsch_editor, instruction, log=log)


DEVICE_MODEL_TYPE_EXPECTATIONS: Dict[str, set] = {
    "D": {"D"},
    "QN": {"NPN"},
    "QP": {"PNP"},
    "NMOS": {"NMOS", "VDMOS"},
    "PMOS": {"PMOS", "VDMOS"},
    "NJF": {"NJF"},
    "PJF": {"PJF"},
}

PASSIVE_QSCH_TYPES = {
    "R", "C", "L", "V", "I", "K", "B", "E",
}


def parse_qsch_primitive_device_refs(
    qsch_editor, log: Callable[[str], None] = print
) -> Dict[str, List[tuple]]:
    refs_by_model: Dict[str, List[tuple]] = defaultdict(list)
    try:
        components = list(getattr(qsch_editor, "components", {}).items())
    except Exception as exc:
        log(f"  WARNING: could not enumerate components: {exc}")
        return refs_by_model

    for refdes, comp in components:
        comp_type = (
            str(_attr(comp, "type", "Type", "symbol_type", default=""))
            .strip()
            .upper()
        )
        if comp_type in PASSIVE_QSCH_TYPES:
            continue

        # Skip QSpice's own native multi-pin behavioral parts (gates,
        # flip-flops, and everything else placed via
        # _lookup_native_gate_shape / the native-part refdes prefixes
        # QSpice itself uses for these -- confirmed against a real
        # exported gate: these are hardcoded inside QSpice itself and
        # never need an external .lib/.model, regardless of what their
        # "value" text says (e.g. a native AND gate's value is just the
        # word "AND", which would otherwise look exactly like a missing
        # model named "AND" to the check below). Without this, every
        # native gate this tool places would incorrectly show up as an
        # unresolved model needing a manual import.
        if comp_type in ("¥", "Ã", "€", "£"):
            continue

        # Skip components that already carry a fully self-contained,
        # embedded subcircuit (QSpice's own "|.subckt ... .ends" embedding
        # on the symbol's "library file:" attribute -- e.g. every gate
        # synthesize_ltspice_digital_primitives produces). These need no
        # external .lib/.model lookup at all; without this check they'd
        # always show up below as an "unresolved" model needing a manual
        # import, even though nothing is actually missing.
        component_tag = _attr(comp, "tag", default=None)
        if component_tag is not None:
            try:
                symbol_items = component_tag.get_items("symbol")
                symbol_tag = symbol_items[0] if symbol_items else None
                embedded_lib = (
                    symbol_tag.get_text("library file", default="")
                    if symbol_tag is not None
                    else ""
                )
            except Exception:
                embedded_lib = ""
            if isinstance(embedded_lib, str) and embedded_lib.startswith("|"):
                continue

        ref = str(
            _attr(comp, "reference", "refdes", "InstName", default=refdes)
        ).strip() or str(refdes)
        value = _attr(comp, "value", "Value", default=None)
        if value is None:
            continue

        model_name = bare_model_name(str(value))
        if (
            not model_name
            or is_qspice_local_library_component(model_name)
            or model_name.lower() in GENERIC_OPAMP_SYMBOLS
        ):
            continue

        refs_by_model[model_name].append((ref, comp_type))
    return refs_by_model


def _existing_model_definitions(qsch_editor) -> Dict[str, str]:
    from spicelib_vendor.editor.qsch_editor import (
        QSCH_TEXT_INSTR_QUALIFIER,
        QSCH_TEXT_STR_ATTR,
    )

    found: Dict[str, str] = {}
    model_re = re.compile(
        r"^\s*\.(model|subckt)\s+(\S+)\s*(\w+)?", re.IGNORECASE
    )
    for tag in qsch_editor.schematic.get_items("text"):
        try:
            content = tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        if content.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            content = content[len(QSCH_TEXT_INSTR_QUALIFIER) :]
        m = model_re.match(content)
        if m:
            m_name = bare_model_name(m.group(2))
            m_type = m.group(3).upper() if m.group(3) else "SUBCKT"
            found[m_name] = m_type
    return found


def _parse_model_lines(raw_text: str) -> List[tuple]:
    model_re = re.compile(r"^\s*\.model\s+(\S+)\s+(\w+)", re.IGNORECASE)
    results = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = model_re.match(line)
        if m:
            results.append((line, m.group(1), m.group(2).upper()))
        else:
            results.append((line, None, None))
    return results


def validate_and_inject_device_model(
    qsch_editor,
    model_name: str,
    raw_text: str,
    refs: List[tuple],
    log: Callable[[str], None] = print,
) -> int:
    clean_text = flatten_model_statement(raw_text)
    lines = _parse_model_lines(clean_text)
    if not lines:
        log(f"  WARNING: no non-blank lines to inject for {model_name}; skipping.")
        return 0
    injected = 0
    for line, parsed_name, parsed_type in lines:
        if parsed_name is not None and parsed_type is not None:
            for ref, qsch_type in refs:
                exp = DEVICE_MODEL_TYPE_EXPECTATIONS.get(qsch_type)
                if exp and parsed_type not in exp:
                    log(
                        f"  WARNING: {ref} is placed as {qsch_type} "
                        f'(expects {"/".join(sorted(exp))}), but the model '
                        f'supplied for "{model_name}" is {parsed_type}.'
                    )
        _inject_raw_instruction(qsch_editor, line, log=log)
        injected += 1
    return injected


def _normalize_lib_tags(qsch_editor, log: Callable[[str], None] = print) -> int:
    changed = 0
    instr_re = re.compile(
        r"^(?P<qual>.*?)\.(?P<kind>lib|include)\s+(?P<rest>.*)$", re.IGNORECASE
    )
    for tag in qsch_editor.schematic.get_items("text"):
        tokens = tag.tokens
        if len(tokens) < 9:
            continue
        if (
            len(tokens) == 9
            and tokens[8].startswith('"')
            and tokens[8].endswith('"')
        ):
            content = tokens[8][1:-1]
            m = instr_re.match(content)
            if not m:
                continue
            qual, kind, rest = (
                m.group("qual"),
                m.group("kind").lower(),
                m.group("rest").strip(),
            )
            rest = rest.strip('"').strip("'").strip()
            cleaned = _clean_path(rest)
            new_instruction = f'.{kind} "{cleaned}"'
            new_token = f'"{qual}{new_instruction}"'
            if new_token != tokens[8]:
                tokens[8] = new_token
                changed += 1
                log(f"  Normalized: {new_instruction}")
            continue
        head = tokens[8]
        m = re.match(
            r'^"(?P<qual>.*?)\.(?P<kind>lib|include)\s*"$', head, re.IGNORECASE
        )
        if not m:
            continue
        qual, kind = m.group("qual"), m.group("kind").lower()
        tail = tokens[-1]
        path_tokens = tokens[9:-1] if tail in ('""', '"') else tokens[9:]
        raw_path = " ".join(path_tokens)
        cleaned = _clean_path(raw_path)
        new_instruction = f'.{kind} "{cleaned}"'
        tag.tokens = tokens[:8] + [f'"{qual}{new_instruction}"']
        changed += 1
        log(f"  Repaired corrupted instruction: {new_instruction}")
    return changed


def _remove_tag_from_tree(root_tag, target_tag) -> bool:
    items = getattr(root_tag, "items", None)
    if not items:
        return False
    for idx, child in enumerate(list(items)):
        if child is target_tag:
            del items[idx]
            return True
        if _remove_tag_from_tree(child, target_tag):
            return True
    return False


def _next_wire_name(qsch_editor) -> str:
    max_no = 0

    def scan_name(name: object) -> None:
        nonlocal max_no
        if not isinstance(name, str):
            return
        m = re.fullmatch(r"N(\d+)", name.strip())
        if m:
            max_no = max(max_no, int(m.group(1)))

    try:
        for wire in qsch_editor.schematic.get_items("wire"):
            try:
                scan_name(wire.get_attr(3))
            except Exception:
                pass
        for net in qsch_editor.schematic.get_items("net"):
            try:
                scan_name(net.get_attr(5))
            except Exception:
                pass
    except Exception:
        pass
    return f"N{max_no + 1:02d}"


def replace_zero_ohm_resistors_with_wires(
    qsch_editor, log: Callable[[str], None] = print
) -> int:
    from spicelib_vendor.editor.base_schematic import Line, Point
    from spicelib_vendor.editor.qsch_editor import QschTag

    def _point_xy(pos: object):
        if pos is None:
            raise ValueError("missing component position")
        if isinstance(pos, tuple) and len(pos) >= 2:
            return int(pos[0]), int(pos[1])
        for ax_x, ax_y in (("X", "Y"), ("x", "y")):
            if hasattr(pos, ax_x) and hasattr(pos, ax_y):
                return int(getattr(pos, ax_x)), int(getattr(pos, ax_y))
        raise ValueError(f"unrecognized position object: {pos!r}")

    replaced = 0
    to_remove: List[str] = []
    wires_to_add: List[Tuple[int, int, int, int, str]] = []
    components = list(getattr(qsch_editor, "components", {}).items())
    for refdes, comp in components:
        ref = str(
            _attr(comp, "reference", "refdes", "InstName", default=refdes)
        ).strip() or str(refdes)
        comp_type = (
            str(_attr(comp, "type", "Type", "symbol_type", default=""))
            .strip()
            .upper()
        )
        value = _attr(comp, "value", "Value", default=None)
        if comp_type != "R" and not ref.upper().startswith("R"):
            continue
        if not _is_zero_ohm_value(value):
            continue
        component_tag = _attr(comp, "tag", default=None)
        symbol_tag = None
        if component_tag is not None:
            try:
                symbol_items = component_tag.get_items("symbol")
                if symbol_items:
                    symbol_tag = symbol_items[0]
            except Exception:
                symbol_tag = None
        if symbol_tag is None:
            log(
                f"  WARNING: {ref} is 0 ohm but has no symbol geometry; skipping wire replacement."
            )
            continue
        try:
            pins = list(symbol_tag.get_items("pin"))
        except Exception:
            pins = []
        if len(pins) < 2:
            log(
                f"  WARNING: {ref} is 0 ohm but has fewer than 2 pins; skipping wire replacement."
            )
            continue
        try:
            position = _attr(comp, "position", "pos", default=None)
            rot = _attr(comp, "rotation", "rot", default=0)
            rot_val = int(getattr(rot, "value", rot))
            # NOT "% 8": QSCH orientation codes are 0-15 (8-15 = mirrored
            # variants of 0-7 -- see _rotate_local_offset), and rot_val is
            # already in the matching 0-719 degree encoding (0-359 =
            # normal, 360-719 = mirrored, confirmed via a real mirrored
            # resistor: rot_val=360 // 45 = 8, the correct ground-truth
            # QSCH orientation for that component). An earlier "% 8" here
            # wrapped that 8 straight back down to 0, silently discarding
            # the mirror and computing the wrong pin position for any
            # mirrored zero-ohm resistor.
            orientation = rot_val // 45
            px, py = _point_xy(position)
            p1 = qsch_editor._find_pin_position((px, py), orientation, pins[0])
            p2 = qsch_editor._find_pin_position((px, py), orientation, pins[1])
        except Exception as exc:
            log(f"  WARNING: could not derive pin positions for {ref}: {exc}")
            continue
        net1 = None
        net2 = None
        try:
            net1 = qsch_editor._find_net_at_position(*p1)
        except Exception:
            pass
        try:
            net2 = qsch_editor._find_net_at_position(*p2)
        except Exception:
            pass
        wire_net = net1 or net2 or _next_wire_name(qsch_editor)
        wires_to_add.append(
            (int(p1[0]), int(p1[1]), int(p2[0]), int(p2[1]), wire_net)
        )
        to_remove.append(refdes)
        replaced += 1
        log(f"  Replaced 0-ohm resistor {ref} with wire {wire_net}")
    for refdes in to_remove:
        comp = qsch_editor.components.get(refdes)
        if comp is None:
            continue
        tag = _attr(comp, "tag", "qsch_tag", default=None)
        if tag is not None:
            removed = _remove_tag_from_tree(qsch_editor.schematic, tag)
            if not removed:
                log(
                    f"  WARNING: could not remove {refdes} from schematic tree cleanly."
                )
        qsch_editor.components.pop(refdes, None)
    for x1, y1, x2, y2, net in wires_to_add:
        wire_tag, _ = QschTag.parse(f'«wire ({x1},{y1}) ({x2},{y2}) "{net}"»')
        qsch_editor.schematic.items.append(wire_tag)
        try:
            qsch_editor.wires.append(
                Line(Point(x1, y1), Point(x2, y2), net=net)
            )
        except Exception:
            pass
        qsch_editor.canvas_updated = True
    return replaced


def ensure_inductor_damping(qsch_editor, log: Callable[[str], None] = print) -> int:
    """
    Scans all inductor components in QSCH tree and ensures standalone inductors
    have default series damping (Rser=1m) to prevent high-frequency ringing / solver crashes in QSpice.
    """
    schematic_text = ""
    try:
        for tag in qsch_editor.schematic.get_items("text"):
            schematic_text += str(tag) + "\n"
    except Exception:
        pass

    coupled_inductors = find_coupled_inductors(schematic_text)
    fixed = 0

    components = list(getattr(qsch_editor, "components", {}).items())
    for refdes, comp in components:
        ref = str(_attr(comp, "reference", "refdes", "InstName", default=refdes)).strip()
        comp_type = str(_attr(comp, "type", "Type", "symbol_type", default="")).strip().upper()

        if comp_type == "L" or ref.upper().startswith("L"):
            if ref.upper() in coupled_inductors:
                continue

            value = str(_attr(comp, "value", "Value", default="") or "").strip()
            if not value:
                continue

            if "RSER" not in value.upper():
                new_value = f"{value} Rser=1m"
                try:
                    qsch_editor.set_component_value(refdes, new_value)
                    fixed += 1
                    log(f"  Injected default damping into standalone inductor {ref}: '{value}' -> '{new_value}'")
                except Exception as e:
                    log(f"  WARNING: Could not update inductor value for {ref}: {e}")

    if fixed:
        qsch_editor.canvas_updated = True
    return fixed


def convert_startup_to_uic(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Replaces ".tran ... startup" with ".tran ... uic" -- a plain text
    swap, nothing else. No components are moved, added, or rewired.

    This exists because QSpice silently ignores "startup" outright
    (confirmed via Qorvo's own forum -- HANDOFF.md section 4.2): left
    alone, QSpice falls back to a normal bias-point-solved start instead.

    An earlier version of this also tried to replicate LTspice's actual
    ramped-startup behavior generally (relocating every independent
    source and adding a behavioral scaler to ramp it in over 20us).
    Removed at the user's explicit direction after real-world testing
    showed it added a wall of disconnected-looking staged components
    without visibly changing the simulated result -- not worth the
    schematic clutter for a benefit that didn't show up in practice. If
    revisiting this, the ramp mechanism and the two real bugs found
    while building it are preserved in git history and in HANDOFF.md
    section 6.6 for reference.
    """
    tran_fixed = 0
    for text_tag in qsch_editor.schematic.get_items("text"):
        try:
            raw = text_tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(raw, str) or not raw.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            continue
        content = raw[len(QSCH_TEXT_INSTR_QUALIFIER):]
        lines = content.replace("\\n", "\n").split("\n")
        changed = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith(".TRAN"):
                tokens = stripped.split()
                tokens = [t for t in tokens if t.upper() != "STARTUP"]
                if not any(t.upper() == "UIC" for t in tokens):
                    tokens.append("uic")
                new_line = " ".join(tokens)
                if new_line != stripped:
                    changed = True
                new_lines.append(new_line)
            else:
                new_lines.append(line)
        if changed:
            new_content = "\\n".join(new_lines) if "\\n" in content else "\n".join(new_lines)
            try:
                text_tag.set_attr(QSCH_TEXT_STR_ATTR, QSCH_TEXT_INSTR_QUALIFIER + new_content)
                tran_fixed += 1
                log('  Rewrote ".tran ... startup" to use "uic" instead (QSpice ignores "startup" entirely).')
            except Exception as e:
                log(f"  WARNING: could not rewrite .tran directive: {e}")

    if tran_fixed:
        qsch_editor.canvas_updated = True
    return tran_fixed


@dataclass
class ProcessingResult:
    fixed_count: int
    injected_count: int
    skipped_models: List[str]
    resolved_paths: Dict[str, str]
    reclassified_count: int = 0
    replaced_zero_ohm_count: int = 0
    device_models_injected: int = 0
    device_models_skipped: List[str] = field(default_factory=list)
    inductors_damped_count: int = 0
    qspice_local_library_count: int = 0
    digital_primitives_synthesized: int = 0
    cancelled: bool = False
    tran_uic_rewritten_count: int = 0


def fix_misclassified_comments(
    qsch_editor, log: Callable[[str], None] = print
) -> int:
    from spicelib_vendor.editor.qsch_editor import (
        QSCH_TEXT_COMMENT,
        QSCH_TEXT_INSTR_QUALIFIER,
        QSCH_TEXT_STR_ATTR,
    )

    fixed = 0
    text_tags = qsch_editor.schematic.get_items("text")
    for tag in text_tags:
        try:
            if tag.get_attr(QSCH_TEXT_COMMENT) == 1:
                continue
            content = tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        stripped = content
        if stripped.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            stripped = stripped[len(QSCH_TEXT_INSTR_QUALIFIER) :]
        stripped = stripped.strip()
        if not stripped:
            continue

        # A single text box can hold several lines joined by a literal
        # "\n" escape (LTspice's own convention for a multi-line directive
        # box, e.g. a commented-out alternate value sitting right above the
        # real one: ";tran 20m startup\n.tran 40m startup"). Checking only
        # the box's raw start -- as this function used to -- misses a real,
        # active directive that isn't on the first line, and demotes the
        # WHOLE box (including that real line) to an inert comment.
        # Confirmed causing exactly that on a real circuit: a genuine
        # ".tran 40m startup" was being silently reclassified as a comment
        # here because the box started with a commented-out ";tran 20m
        # startup" line first. Splitting and checking every line avoids
        # that: the box stays active if ANY line in it looks like a real
        # directive (a dot-command, or a bare K-coupling statement).
        lines_in_box = stripped.replace("\\n", "\n").split("\n")
        has_real_directive_line = any(
            line.strip().startswith(".")
            or re.match(r"^K\d*\s+\S+\s+\S+", line.strip(), re.IGNORECASE)
            for line in lines_in_box
        )
        if not has_real_directive_line:
            tag.set_attr(QSCH_TEXT_COMMENT, 1)
            fixed += 1
            preview = stripped[:80] + ("..." if len(stripped) > 80 else "")
            log(f'  Reclassified as comment: "{preview}"')
    if fixed:
        qsch_editor.canvas_updated = True
    return fixed


DEFAULT_LOGIC_VLOW = 0.0
DEFAULT_LOGIC_VHIGH = 5.0

DIGITAL_PRIMITIVE_STATELESS_MODELS = {"AND", "OR", "XOR", "BUF", "NAND", "NOR", "XNOR", "INV", "NOT"}


def _get_reliable_asc_netlist(asc_file: str, log: Callable[[str], None]) -> Optional[str]:
    """Returns a path to a netlist for asc_file with real, LTspice-resolved
    pin-to-net connectivity, by reusing an existing sibling .net file if one
    is present -- e.g. from the user having already simulated the circuit in
    LTspice, which is exactly what produced the real, verified connectivity
    used during development of this feature.

    This deliberately does NOT invoke LTspice.exe itself to generate a fresh
    netlist on demand: that was tried during development and, despite
    passing an explicit timeout to spicelib's LTspice.create_netlist(), the
    LTspice process did not reliably respect it -- it was observed hanging
    for hours on a real circuit instead of returning promptly. Silently
    launching an external GUI process that might hang indefinitely is worse
    than doing nothing, so this only ever reads a netlist that already
    exists. Returns None -- never a guess -- if no sibling .net is found;
    callers must treat that as "connectivity unknown" rather than fall back
    to approximate geometry-based lookups (also found unreliable on real
    files during development).
    """
    net_path = os.path.splitext(asc_file)[0] + ".net"
    if os.path.isfile(net_path):
        log(f"  Using existing netlist: {net_path}")
        return net_path
    return None


def _parse_netlist_device_tokens(net_path: str) -> Dict[str, List[str]]:
    lines = _read_text_file_robust(net_path) or []
    result: Dict[str, List[str]] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("."):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        result[tokens[0].upper()] = tokens[1:]
    return result


MAX_RAW_FILE_SIZE_FOR_LOGIC_LEVEL_LOOKUP = 50 * 1024 * 1024  # 50 MB


def _load_raw_for_logic_levels(asc_file: str, log: Callable[[str], None]) -> Optional[object]:
    """Parses a sibling .raw simulation result for asc_file once, for reuse
    across every net-level lookup in a single synthesis run. RawRead parses
    the whole (potentially large, many-trace) waveform file, so calling it
    fresh per-lookup was measured taking minutes on a real circuit -- this
    caches the single parse instead.

    Skips files above MAX_RAW_FILE_SIZE_FOR_LOGIC_LEVEL_LOOKUP entirely: a
    real 14.7 GB .raw from a long fine-grained transient run was measured
    taking minutes and tens of GB of RAM to parse just for this lookup --
    unacceptable for what's meant to be an automatic, always-fast step.
    Callers fall back to DEFAULT_LOGIC_VLOW/VHIGH in that case, which is
    still a correct choice of *some* clean digital swing, just not
    necessarily matching this circuit's own original analog rail voltage.

    Returns None on any failure/absence/oversize so callers always have a
    safe, fast fallback."""
    raw_path = os.path.splitext(asc_file)[0] + ".raw"
    if not os.path.isfile(raw_path):
        return None
    try:
        size = os.path.getsize(raw_path)
    except OSError:
        return None
    if size > MAX_RAW_FILE_SIZE_FOR_LOGIC_LEVEL_LOOKUP:
        log(
            f"  Skipping empirical logic-level lookup: {raw_path} is "
            f"{size / (1024*1024):.0f} MB, too large to parse just for this; "
            f"using the default {DEFAULT_LOGIC_VLOW:g}V/{DEFAULT_LOGIC_VHIGH:g}V swing instead."
        )
        return None
    try:
        from spicelib_vendor.raw.raw_read import RawRead
        return RawRead(raw_path)
    except Exception as exc:
        log(f"  WARNING: could not read {raw_path} for empirical logic levels: {exc}")
        return None


def _lookup_raw_logic_levels(raw, net_name: str) -> Optional[Tuple[float, float]]:
    """Best-effort: if the already-loaded raw (see _load_raw_for_logic_levels)
    contains a trace for net_name, returns the (min, max) voltage actually
    observed in that real run -- empirical evidence of this circuit's own
    logic swing, preferred over any fixed default. Returns None on any
    failure/absence so callers always have a safe fallback."""
    if raw is None:
        return None
    try:
        target = f"v({net_name})".lower()
        for name in raw.get_trace_names():
            if name.lower() == target:
                data = raw.get_trace(name).get_wave(0)
                lo, hi = float(min(data)), float(max(data))
                if hi > lo:
                    return lo, hi
                return None
    except Exception:
        return None
    return None


def _digital_bool_expr(model: str, terms: List[str]) -> str:
    if not terms:
        raise ValueError("no wired inputs to synthesize from")
    if model == "AND":
        return "*".join(terms)
    if model == "OR":
        expr = terms[0]
        for t in terms[1:]:
            expr = f"(1-(1-({expr}))*(1-({t})))"
        return expr
    if model == "XOR":
        expr = terms[0]
        for t in terms[1:]:
            expr = f"(({expr})+({t})-2*({expr})*({t}))"
        return expr
    if model == "BUF":
        if len(terms) != 1:
            raise ValueError("BUF expects exactly one wired input")
        return terms[0]
    if model in ("INV", "NOT"):
        if len(terms) != 1:
            raise ValueError(f"{model} expects exactly one wired input")
        return f"(1-({terms[0]}))"
    if model == "NAND":
        return f"(1-({_digital_bool_expr('AND', terms)}))"
    if model == "NOR":
        return f"(1-({_digital_bool_expr('OR', terms)}))"
    if model == "XNOR":
        return f"(1-({_digital_bool_expr('XOR', terms)}))"
    raise ValueError(f"unsupported digital primitive model for synthesis: {model}")


_DIGITAL_INPUT_LEAD_STUB_LEN = 300
"""Length (schematic units) of the wire stub drawn from each pin's original
artwork position out to its net label -- see _cosmetic_stub_endpoint and its
use inside synthesize_ltspice_digital_primitives. Matches the typical short
wire length already used throughout the rest of a real converted circuit
(checked directly: most short wire segments in a real file run 100-300
units) -- an earlier, much shorter length (80) looked like it wasn't really
connected to anything, sitting right against the gate body instead of
reading as a normal wire lead the way every other connection in the same
circuit does.
"""


def _cosmetic_stub_endpoint(local_xy: Tuple[int, int], length: int) -> Tuple[int, int]:
    """Returns a point `length` units further out from `local_xy`, extended
    along the ray from the local origin (0,0) through local_xy. Used to
    place a short wire stub for a pin lead without needing to know the
    symbol's actual rotation/justification -- works for a pin on any side
    of the body since it just pushes further away from center.
    """
    lx, ly = local_xy
    mag = math.hypot(lx, ly)
    if mag == 0:
        # Degenerate case: pin drawn at the symbol's own origin. Push left
        # as a safe arbitrary default instead of a zero-length wire.
        return (lx - length, ly)
    ux, uy = lx / mag, ly / mag
    return (int(round(lx + ux * length)), int(round(ly + uy * length)))


def _parse_xy(xy_str: str) -> Tuple[int, int]:
    """Parses a "(x,y)" coordinate string into an (int, int) tuple."""
    x_str, y_str = xy_str.strip("()").split(",")
    return int(x_str), int(y_str)


# Sourced directly from the user's own compiled QSpice library file
# (qspice_gate_library.qsch, a flat sheet of every native QSpice component
# the user placed by hand from QSpice's own Symbol menu) via a one-time
# parse -- every geometry/pin coordinate below is copied exactly from a
# real QSpice-exported symbol, not hand-drawn, hand-typed, or extrapolated.
# Covers AND2-5, OR2-4, XOR2-4, and BUF/INV, each in 'both' (Q + ¬Q),
# 'Q'-only, and 'NQ'-only (the NAND/NOR/XNOR variant) forms.
_QSPICE_NATIVE_GATE_LIBRARY = {
    ('AND', 2, 'NQ'): {
        'symbol_name': 'AND2_Q',
        'description': '2-Input NAND gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('AND', 2, 'Q'): {
        'symbol_name': 'AND2Q',
        'description': '2-Input AND gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('AND', 2, 'both'): {
        'symbol_name': 'AND2',
        'description': '2-Input AND w/ complementary outputs',
        'geometry': [
            'line (400,-100) (365,-100) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'line (400,100) (283,100) 0 0 0x1000000 -1 -1',
            'ellipse (285,-60) (365,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('AND', 3, 'NQ'): {
        'symbol_name': 'AND3_Q',
        'description': '3-Input NAND gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('AND', 3, 'Q'): {
        'symbol_name': 'AND3Q',
        'description': '3-Input AND gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('AND', 3, 'both'): {
        'symbol_name': 'AND3',
        'description': '3-Input AND w/ complementary outputs',
        'geometry': [
            'line (400,-100) (365,-100) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'line (400,100) (283,100) 0 0 0x1000000 -1 -1',
            'ellipse (285,-60) (365,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('AND', 4, 'Q'): {
        'symbol_name': 'AND4Q',
        'description': '4-Input AND gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('AND', 4, 'both'): {
        'symbol_name': 'AND4',
        'description': '4-Input AND w/ complementary outputs',
        'geometry': [
            'line (400,-100) (365,-100) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (-200,300) 0 0 0x1000000 -1 -1',
            'line (-200,300) (0,300) 0 0 0x1000000 -1 -1',
            'line (-200,-300) (0,-300) 0 0 0x1000000 -1 -1',
            'line (400,100) (283,100) 0 0 0x1000000 -1 -1',
            'ellipse (285,-60) (365,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (0,-300) (0,300) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('AND', 5, 'Q'): {
        'symbol_name': 'AND5Q',
        'description': '5-Input AND gate',
        'geometry': [
            'line (500,0) (400,0) 0 0 0x1000000 -1 -1',
            'line (-200,-400) (-200,400) 0 0 0x1000000 -1 -1',
            'line (-200,400) (0,400) 0 0 0x1000000 -1 -1',
            'line (-200,-400) (0,-400) 0 0 0x1000000 -1 -1',
            'arc3p (0,-400) (0,400) (0,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,400)', False),
            ('Vss', '(-100,-400)', False),
            ('Q', '(500,0)', False),
            ('¬Q', '(0,0)', True),
            ('b0', '(-200,400)', False),
            ('b1', '(-200,200)', False),
            ('b2', '(-200,0)', False),
            ('b3', '(-200,-200)', False),
            ('b5', '(-200,-400)', False),
        ],
    },
    ('BUF', 1, 'NQ'): {
        'symbol_name': 'INV',
        'description': 'Inverter',
        'geometry': [
            'line (300,0) (280,0) 0 0 0x1000000 -1 -1',
            'line (-100,-150) (200,0) 0 0 0x1000000 -1 -1',
            'line (200,0) (-100,150) 0 0 0x1000000 -1 -1',
            'line (-100,150) (-100,-150) 0 0 0x1000000 -1 -1',
            'ellipse (200,40) (280,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(0,100)', False),
            ('Vss', '(0,-100)', False),
            ('Q', '(100,0)', True),
            ('¬Q', '(300,0)', False),
            ('b0', '(-100,0)', False),
        ],
    },
    ('BUF', 1, 'Q'): {
        'symbol_name': 'BUFQ',
        'description': 'Buffer',
        'geometry': [
            'line (-100,-150) (200,0) 0 0 0x1000000 -1 -1',
            'line (200,0) (-100,150) 0 0 0x1000000 -1 -1',
            'line (-100,150) (-100,-150) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(0,100)', False),
            ('Vss', '(0,-100)', False),
            ('Q', '(200,0)', False),
            ('¬Q', '(100,0)', True),
            ('b0', '(-100,0)', False),
        ],
    },
    ('BUF', 1, 'both'): {
        'symbol_name': 'BUF',
        'description': 'Buffer w/ complementary outputs',
        'geometry': [
            'line (-100,-150) (200,0) 0 0 0x1000000 -1 -1',
            'line (200,0) (-100,150) 0 0 0x1000000 -1 -1',
            'line (-100,150) (-100,-150) 0 0 0x1000000 -1 -1',
            'line (200,-100) (129,-100) 0 0 0x1000000 -1 -1',
            'line (200,100) (90,100) 0 0 0x1000000 -1 -1',
            'line (90,100) (90,55) 0 0 0x1000000 -1 -1',
            'ellipse (49,-60) (129,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(0,100)', False),
            ('Vss', '(0,-100)', False),
            ('Q', '(200,100)', False),
            ('¬Q', '(200,-100)', False),
            ('b0', '(-100,0)', False),
        ],
    },
    ('OR', 2, 'Q'): {
        'symbol_name': 'OR2Q',
        'description': '2-Input OR gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('OR', 2, 'both'): {
        'symbol_name': 'OR2',
        'description': '2-Input OR w/ complementary outputs',
        'geometry': [
            'line (400,-100) (336,-100) 0 0 0x1000000 -1 -1',
            'line (400,100) (248,100) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'ellipse (256,-60) (336,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('OR', 3, 'NQ'): {
        'symbol_name': 'OR3_Q',
        'description': '3-Input NOR gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('OR', 3, 'Q'): {
        'symbol_name': 'OR3Q',
        'description': '3-Input OR gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('OR', 3, 'both'): {
        'symbol_name': 'OR3',
        'description': '3-Input OR w/ complementary outputs',
        'geometry': [
            'line (400,-100) (336,-100) 0 0 0x1000000 -1 -1',
            'line (400,100) (248,100) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'ellipse (256,-60) (336,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('OR', 4, 'NQ'): {
        'symbol_name': 'OR4_Q',
        'description': '4-Input NOR gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,100) (-137,100) 0 0 0x1000000 -1 -1',
            'line (-200,-100) (-137,-100) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('OR', 4, 'Q'): {
        'symbol_name': 'OR4Q',
        'description': '4-Input OR gate',
        'geometry': [
            'line (400,0) (300,0) 0 0 0x1000000 -1 -1',
            'line (-200,100) (-137,100) 0 0 0x1000000 -1 -1',
            'line (-200,-100) (-137,-100) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-200,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('XOR', 2, 'NQ'): {
        'symbol_name': 'XOR2_Q',
        'description': '2-Input XNOR gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('XOR', 2, 'Q'): {
        'symbol_name': 'XOR2Q',
        'description': '2-Input XOR gate',
        'geometry': [
            'line (400,0) (298,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('XOR', 2, 'both'): {
        'symbol_name': 'XOR2',
        'description': '2-Input XOR w/ complementary outputs',
        'geometry': [
            'line (400,-100) (336,-100) 0 0 0x1000000 -1 -1',
            'line (400,100) (248,100) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'ellipse (256,-60) (336,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,-200)', False),
        ],
    },
    ('XOR', 3, 'NQ'): {
        'symbol_name': 'XOR3_Q',
        'description': '3-Input XNOR gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('XOR', 3, 'Q'): {
        'symbol_name': 'XOR3Q',
        'description': '3-Input XOR gate',
        'geometry': [
            'line (400,0) (298,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('XOR', 3, 'both'): {
        'symbol_name': 'XOR3',
        'description': '3-Input XOR w/ complementary outputs',
        'geometry': [
            'line (400,-100) (336,-100) 0 0 0x1000000 -1 -1',
            'line (400,100) (248,100) 0 0 0x1000000 -1 -1',
            'line (-200,0) (-129,0) 0 0 0x1000000 -1 -1',
            'line (-200,-200) (-160,-200) 0 0 0x1000000 -1 -1',
            'line (-200,200) (-160,200) 0 0 0x1000000 -1 -1',
            'ellipse (256,-60) (336,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,200)', False),
            ('b1', '(-200,0)', False),
            ('b2', '(-200,-200)', False),
        ],
    },
    ('XOR', 4, 'NQ'): {
        'symbol_name': 'XOR4_Q',
        'description': '4-Input XNOR gate',
        'geometry': [
            'line (400,0) (380,0) 0 0 0x1000000 -1 -1',
            'line (-200,100) (-137,100) 0 0 0x1000000 -1 -1',
            'line (-200,-100) (-137,-100) 0 0 0x1000000 -1 -1',
            'ellipse (300,40) (380,-40) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(200,0)', True),
            ('¬Q', '(400,0)', False),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('XOR', 4, 'Q'): {
        'symbol_name': 'XOR4Q',
        'description': '4-Input XOR gate',
        'geometry': [
            'line (400,0) (298,0) 0 0 0x1000000 -1 -1',
            'line (-200,100) (-137,100) 0 0 0x1000000 -1 -1',
            'line (-200,-100) (-137,-100) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,0)', False),
            ('¬Q', '(200,0)', True),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
    ('XOR', 4, 'both'): {
        'symbol_name': 'XOR4',
        'description': '4-Input XOR w/ complementary outputs',
        'geometry': [
            'line (400,-100) (336,-100) 0 0 0x1000000 -1 -1',
            'line (400,100) (248,100) 0 0 0x1000000 -1 -1',
            'line (-200,100) (-137,100) 0 0 0x1000000 -1 -1',
            'line (-200,-100) (-137,-100) 0 0 0x1000000 -1 -1',
            'ellipse (256,-60) (336,-140) 0 0 0 0x1000000 0x1000000 -1 -1',
            'arc3p (-150,-300) (300,0) (-100,200) 0 0 0x1000000 -1 -1',
            'arc3p (300,0) (-150,300) (-100,-200) 0 0 0x1000000 -1 -1',
            'arc3p (-200,-300) (-200,300) (-800,0) 0 0 0x1000000 -1 -1',
            'arc3p (-150,-300) (-150,300) (-750,0) 0 0 0x1000000 -1 -1',
        ],
        'pins': [
            ('Vdd', '(-100,300)', False),
            ('Vss', '(-100,-300)', False),
            ('Q', '(400,100)', False),
            ('¬Q', '(400,-100)', False),
            ('b0', '(-200,300)', False),
            ('b1', '(-200,100)', False),
            ('b2', '(-200,-100)', False),
            ('b3', '(-200,-300)', False),
        ],
    },
}


_QSPICE_NATIVE_GATE_FUNCTION: Dict[str, Tuple[str, bool]] = {
    "AND": ("AND", False), "NAND": ("AND", True),
    "OR": ("OR", False), "NOR": ("OR", True),
    "XOR": ("XOR", False), "XNOR": ("XOR", True),
    "BUF": ("BUF", False), "INV": ("BUF", True), "NOT": ("BUF", True),
}
"""Maps every LTspice ideal digital primitive model this tool can
synthesize to (QSpice native gate family, whether the LTspice model's own
"Q" pin is the INVERTED function relative to that family's plain Q output).
E.g. NAND's own "Q" pin already carries the inverted-AND result, so it
maps to the AND family with inverted=True -- meaning it should be wired to
the native part's "¬Q" pin, not "Q".
"""


def _lookup_native_gate_shape(
    model: str, n_inputs: int, has_q: bool, has_nq: bool
) -> Optional[Tuple[dict, str, str]]:
    """Returns (shape, native_q_pin, native_nq_pin) for the QSpice-native
    gate part that covers this (model, n_inputs, which outputs are wired)
    combination, or None if nothing in _QSPICE_NATIVE_GATE_LIBRARY covers
    it (caller should fall back to the synthesized-behavioral-source path
    in that case).

    native_q_pin/native_nq_pin say which of the returned part's own pins
    ("Q" or "¬Q") the caller should wire the LTspice gate's own Q net /
    _Q net to -- for an inverted model (NAND/NOR/XNOR/INV/NOT) these are
    swapped, since that model's own "Q" pin already carries the negated
    function.

    Every (function, n_inputs, mode) combination actually used here is
    looked up in a table built directly from a real QSpice gate-library
    export (see _QSPICE_NATIVE_GATE_LIBRARY's own header) -- nothing here
    is extrapolated or guessed. If the exact single-output variant needed
    isn't in the library (QSpice doesn't ship every combination -- e.g.
    there's no dedicated 4-input NAND-only icon), this falls back to the
    "both outputs" variant for that same (function, n_inputs) when one
    exists, wiring only the pin actually needed and leaving the other at
    QSpice's own standard "not connected" marker -- still a real,
    confirmed part, just not the narrowest icon QSpice happens to ship.
    """
    info = _QSPICE_NATIVE_GATE_FUNCTION.get(model)
    if info is None:
        return None
    function, inverted = info
    native_q_pin, native_nq_pin = ("¬Q", "Q") if inverted else ("Q", "¬Q")
    if has_q and has_nq:
        mode = "both"
    elif has_q:
        mode = "NQ" if inverted else "Q"
    elif has_nq:
        mode = "Q" if inverted else "NQ"
    else:
        return None
    shape = _QSPICE_NATIVE_GATE_LIBRARY.get((function, n_inputs, mode))
    if shape is None and mode != "both":
        shape = _QSPICE_NATIVE_GATE_LIBRARY.get((function, n_inputs, "both"))
    if shape is None:
        return None
    return shape, native_q_pin, native_nq_pin


_logic_supply_cache_key = "_qspice_logic_supply_nets"


def _get_or_create_logic_supply_net(
    qsch_editor, vhigh: float, ox: int, oy: int, log: Callable[[str], None]
) -> Tuple[str, Tuple[int, int], Tuple[int, int]]:
    """Returns (net_name, plus_point, minus_point) for a supply carrying
    +vhigh volts relative to ground, reusing a nearby existing supply
    source for this voltage if one is already within reach of (ox, oy),
    or creating a new small local one right next to it otherwise.

    plus_point/minus_point are the exact absolute coordinates of that
    source's own already-labeled connection points. Callers should wire
    directly to these coordinates (a real wire, no new label) rather than
    drawing another same-named net label of their own -- every additional
    labeled stub is a full separate text label QSpice draws on the
    schematic, and with every gate needing both Vdd and Vss this was
    producing a wall of repeated "N_QSpiceLogicVddXV" text (confirmed by
    counting them in a real converted file: 10 separate copies of the
    same label from just 8 gates). Wiring directly to one shared point
    instead means only the source itself carries a label, no matter how
    many gates draw power from it.

    QSpice's native gate-library parts (unlike LTspice's ideal digital
    primitives) are physically powered -- confirmed both by QSpice's own
    documentation and by a real exported AND2 gate having "Vdd"/"Vss"
    pins -- so using one requires a real supply connection that the
    original ideal-gate circuit never had.

    A single supply shared by the whole schematic was tried first, but on
    a real, spread-out circuit it meant every gate except the very first
    one got a same-named net label with no visible wire anywhere near it
    (the single source sat next to just one gate, far from the rest) --
    confirmed by hand, and it looked exactly as broken as it was
    electrically fine: a scatter of disconnected-looking labels. Capping
    reuse to gates that are actually near each other keeps genuinely
    nearby gates sharing one clean local source (the common case for
    gates in the same functional block) while anything far away gets its
    own, so no wire has to cross the whole schematic.
    """
    cache = getattr(qsch_editor, _logic_supply_cache_key, None)
    if cache is None:
        cache = {}
        setattr(qsch_editor, _logic_supply_cache_key, cache)
    key = round(float(vhigh), 6)
    sources = cache.setdefault(key, [])

    _REUSE_RADIUS = 2500
    for (sx, sy), existing_net_name, existing_plus, existing_minus in sources:
        if math.hypot(sx - ox, sy - oy) <= _REUSE_RADIUS:
            return existing_net_name, existing_plus, existing_minus

    supply_index = sum(len(v) for v in cache.values()) + 1
    net_name = f"N_QSpiceLogicVdd_{key:g}V_{supply_index}".replace(".", "p").replace("-", "m")
    vx, vy = ox - 1600, oy
    comp_tag = QschTag()
    comp_tag.tokens = ["component", f"({vx},{vy})", "0", "0"]
    sym_tag = QschTag("symbol", "V")
    for raw_item in (
        "«type: V»",
        "«description: Independent Voltage Source»",
        "«shorted pins: false»",
        "«line (0,-130) (0,-200) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,130) 0 0 0x1000000 -1 -1»",
        "«ellipse (-130,130) (130,-130) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«line (-60,90) (60,90) 0 0 0x1000000 -1 -1»",
        "«line (-60,-90) (60,-90) 0 0 0x1000000 -1 -1»",
        "«line (0,-30) (0,30) 0 0 0x1000000 -1 -1»",
        "«line (-30,60) (30,60) 0 0 0x1000000 -1 -1»",
    ):
        tag, _ = QschTag.parse(raw_item)
        sym_tag.items.append(tag)
    ref_txt, _ = QschTag.parse(f'«text (150,150) 1 7 0 0x1000000 -1 -1 "V_LOGIC{supply_index}"»')
    val_txt, _ = QschTag.parse(f'«text (150,-150) 1 7 0 0x1000000 -1 -1 "{key:g}"»')
    plus_pin, _ = QschTag.parse('«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»')
    minus_pin, _ = QschTag.parse('«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»')
    sym_tag.items.append(ref_txt)
    sym_tag.items.append(val_txt)
    sym_tag.items.append(plus_pin)
    sym_tag.items.append(minus_pin)
    comp_tag.items.append(sym_tag)
    qsch_editor.schematic.items.append(comp_tag)

    plus_stub = (vx, vy + 260)
    minus_stub = (vx, vy - 260)
    plus_wire, _ = QschTag.parse(f'«wire ({vx},{vy+200}) ({plus_stub[0]},{plus_stub[1]}) "{net_name}"»')
    minus_wire, _ = QschTag.parse(f'«wire ({vx},{vy-200}) ({minus_stub[0]},{minus_stub[1]}) "0"»')
    qsch_editor.schematic.items.append(plus_wire)
    qsch_editor.schematic.items.append(minus_wire)
    plus_net, _ = QschTag.parse(f'«net ({plus_stub[0]},{plus_stub[1]}) 1 13 0 "{net_name}"»')
    minus_net, _ = QschTag.parse(f'«net ({minus_stub[0]},{minus_stub[1]}) 1 13 0 "0"»')
    qsch_editor.schematic.items.append(plus_net)
    qsch_editor.schematic.items.append(minus_net)

    log(f"  Added local logic-supply source V_LOGIC{supply_index} = {key:g}V (net {net_name}) near ({ox},{oy}).")
    sources.append(((ox, oy), net_name, plus_stub, minus_stub))
    return net_name, plus_stub, minus_stub


def _fix_stale_wire_names_at(qsch_editor, point: Tuple[int, int], correct_name: str) -> int:
    """Corrects the stored net-name token on any existing 'wire' tag whose
    endpoint exactly matches `point`, to `correct_name`.

    Necessary because every wire this tool's own step-1 conversion writes
    from the original .asc carries a fixed literal "0" as its own stored
    net-name token (a spicelib copy_from() template artifact, not a real
    one -- see _build_qsch_wire_graph's own docstring). Confirmed directly
    on a real circuit: QSpice's own schematic checker reads a wire's own
    stored name as authoritative, so reaching a new, correctly-named wire
    back to physically touch one of these old dangling wires (needed for
    real continuity to whatever the original wire actually leads to)
    produced a genuine "conflicting_net_labels" finding as a direct result
    -- not a false positive, the old wire's own literal-"0" token really
    was disagreeing with the new, correct label at the exact same point.
    Fixing the old wire's own name at its root, instead of just adding a
    competing tag/wire next to it, is what actually resolves that.
    """
    fixed = 0
    for wire in qsch_editor.schematic.get_items("wire"):
        try:
            p1 = tuple(wire.get_attr(1))
            p2 = tuple(wire.get_attr(2))
        except Exception:
            continue
        if p1 == point or p2 == point:
            try:
                wire.set_attr(3, correct_name)
                fixed += 1
            except Exception:
                pass
    return fixed


def _connect_synth_pin_by_label(
    qsch_editor,
    ox: int,
    oy: int,
    local_xy: Tuple[int, int],
    net_name: str,
    orig_abs: Optional[Tuple[int, int]],
    _known_net_names: Set[str],
    stub_len: int = _DIGITAL_INPUT_LEAD_STUB_LEN,
) -> Tuple[int, int]:
    """Connects a synthesized component's pin to `net_name`.

    When `orig_abs` is known (the exact spot the removed original gate's
    own pin used to sit), this draws one real, direct wire from the new
    pin all the way to that point, plus a stale-wire-name fix and a net
    label at that far end. A same-name-only label was tried instead (no
    physical wire) and measurably failed on a real circuit: QSpice_MCP's
    own authoritative net connectivity report showed 6 gate outputs each
    landing on a real, 1-pin-only isolated net despite their target name
    already appearing as a flag elsewhere in the file -- same-name matching
    across two physically disjoint points is evidently not reliable in
    this tool's own output, for reasons not fully pinned down (likely
    related to this circuit's pre-existing stale-wire-name corruption
    documented in _fix_stale_wire_names_at). A direct physical wire has no
    such ambiguity and is the one mechanism confirmed, first-hand, to
    actually simulate correctly in real QSpice on this exact circuit.
    Gate placement is kept close to each gate's own original position
    specifically so this wire stays short.

    When `orig_abs` is None (a brand-new net this tool invents itself,
    e.g. a shared logic supply rail with no original-circuit position to
    reach), falls back to a short local wire stub + net label instead,
    matching this circuit's own convention for a signal tapped in several
    places (e.g. "Pos_15V_Bias" has 16 separate flags spread across the
    file).

    Returns the absolute (x, y) of the pin's own wire endpoint, for
    callers that also want it.
    """
    pin_abs = (ox + local_xy[0], oy + local_xy[1])
    if net_name != "0" and orig_abs is not None:
        _fix_stale_wire_names_at(qsch_editor, orig_abs, net_name)
        wire_tag, _ = QschTag.parse(
            f'«wire ({pin_abs[0]},{pin_abs[1]}) ({orig_abs[0]},{orig_abs[1]}) "{net_name}"»'
        )
        far_net_tag, _ = QschTag.parse(
            f'«net ({orig_abs[0]},{orig_abs[1]}) 1 13 0 "{net_name}"»'
        )
        qsch_editor.schematic.items.append(wire_tag)
        qsch_editor.schematic.items.append(far_net_tag)
        _known_net_names.add(net_name)
        return orig_abs
    stub_xy = _cosmetic_stub_endpoint(local_xy, stub_len)
    dest_abs = (ox + stub_xy[0], oy + stub_xy[1])
    wire_tag, _ = QschTag.parse(
        f'«wire ({pin_abs[0]},{pin_abs[1]}) ({dest_abs[0]},{dest_abs[1]}) "{net_name}"»'
    )
    net_tag, _ = QschTag.parse(f'«net ({dest_abs[0]},{dest_abs[1]}) 1 13 0 "{net_name}"»')
    qsch_editor.schematic.items.append(wire_tag)
    qsch_editor.schematic.items.append(net_tag)
    _known_net_names.add(net_name)
    return dest_abs


def _build_qsch_wire_graph(qsch_editor):
    """Builds a union-find graph over every wire endpoint and net-label
    position in the schematic, so connectivity between two points can be
    correctly traced through any number of wire-to-wire hops.

    This exists because spicelib's own component.ports property (and the
    single-hop position lookup it's built on) only checks a pin's own
    exact position for a real "net" label or a wire DIRECTLY touching
    it -- and every wire this tool's own step-1 conversion produces from
    the original .asc carries a fixed literal "0" as its own stored net
    field (a base spicelib copy_from() template value, not a real one),
    so a pin one wire-hop away from its real label incorrectly reads as
    tied to ground. Confirmed directly against a real circuit: a gate's
    real output pins read "0" this way despite being live signals one
    hop away from their actual label. Tracing the full wire chain here
    instead of trusting a wire's own stored name fixes that.

    Returns (find, group_names, wired_points): find(point) -> canonical
    group id for that point; group_names: dict mapping a group id to the
    real net name reachable in that group, if any "net" (FLAG-derived)
    label exists anywhere in it; wired_points: the set of exact
    coordinates that are actually part of some wire, so a caller can tell
    "connected via wires but no label reachable" (need the position-
    reach-back fallback) apart from "no wire touches this point at all"
    (a pin genuinely left unconnected in the original circuit) -- both
    would otherwise look identical (group_names.get(root) is None either
    way) once find() is called, since find() auto-creates a singleton
    group for any point it's asked about, wired or not.
    """
    parent: Dict[Tuple[int, int], Tuple[int, int]] = {}

    def find(p: Tuple[int, int]) -> Tuple[int, int]:
        parent.setdefault(p, p)
        root = p
        while parent[root] != root:
            root = parent[root]
        while parent[p] != root:
            parent[p], p = root, parent[p]
        return root

    def union(a: Tuple[int, int], b: Tuple[int, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for wire in qsch_editor.schematic.get_items("wire"):
        try:
            p1 = tuple(wire.get_attr(1))
            p2 = tuple(wire.get_attr(2))
        except Exception:
            continue
        union(p1, p2)

    wired_points = set(parent.keys())

    group_names: Dict[Tuple[int, int], str] = {}
    for net in qsch_editor.schematic.get_items("net"):
        try:
            pos = tuple(net.get_attr(1))
            name = net.get_attr(5)
        except Exception:
            continue
        if not name:
            continue
        root = find(pos)
        if root not in group_names:
            group_names[root] = name

    return find, group_names, wired_points


def _rotate_local_offset(local_xy: Tuple[int, int], orientation: int) -> Tuple[int, int]:
    """Rotates/mirrors a symbol-local (x, y) offset by a QSCH orientation
    code and returns the transformed offset, ready to add to a component's
    own placement position to get an absolute coordinate.

    QSCH's real orientation range is 0-15, NOT 0-7 as an earlier version of
    this function assumed: 0-7 are the four 0/90/180/270-degree rotations
    plus their mirrored counterparts, but 8-15 are a SEPARATE mirror
    variant (mirrored across the X axis) with the X-offset's sign flipped
    relative to the 0-7 case -- confirmed directly from spicelib's own
    QschEditor._find_pin_position, which is the actual ground truth QSpice
    itself was built against. The earlier 0-7-only version silently
    computed a wrong (unflipped) position for any orientation of 8 or
    above -- confirmed on a real circuit: resistor R34 at orientation 8
    resolved to a position 200 units off from either of its real wire
    endpoints, causing this function's caller to treat a genuinely-wired
    pin as unconnected.

    Reimplements the same rotation convention QschEditor's own internal
    pin-position lookup uses, without that method's rounding to the
    nearest 100 units -- fine for that method's own purpose (matching
    against other already-rounded lookups) but wrong here, where an exact
    coordinate is needed to key into a wire graph built from exact wire
    endpoints.
    """
    x, y = local_xy
    hyp = math.hypot(x, y)
    if hyp == 0:
        return (0, 0)
    if not (0 <= orientation <= 15):
        raise ValueError(f"Invalid orientation: {orientation}")
    mirrored = orientation >= 8
    base = orientation - 8 if mirrored else orientation
    theta = math.atan2(y, x) + math.radians(base * 45)
    if base % 2:
        hyp *= 1.41421356237
    dx = hyp * math.cos(theta)
    dy = hyp * math.sin(theta)
    if mirrored:
        dx = -dx
    return (int(round(dx)), int(round(dy)))


def _compute_schematic_bounds(qsch_editor) -> Tuple[int, int, int, int]:
    """Returns (min_x, min_y, max_x, max_y) across every wire endpoint, net
    label, and component position currently in the schematic. Used to find
    a placement area clearly outside anything the circuit already uses --
    see the "staging area" note in synthesize_ltspice_digital_primitives
    for why a small fixed offset from each replaced gate's own old
    position isn't safe on a real, densely-packed circuit.
    """
    xs: List[int] = []
    ys: List[int] = []
    for wire in qsch_editor.schematic.get_items("wire"):
        for idx in (1, 2):
            try:
                x, y = wire.get_attr(idx)
                xs.append(x)
                ys.append(y)
            except Exception:
                pass
    for net in qsch_editor.schematic.get_items("net"):
        try:
            x, y = net.get_attr(1)
            xs.append(x)
            ys.append(y)
        except Exception:
            pass
    for comp in qsch_editor.schematic.get_items("component"):
        try:
            x, y = comp.get_attr(1)
            xs.append(x)
            ys.append(y)
        except Exception:
            pass
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def synthesize_ltspice_digital_primitives(
    qsch_editor,
    asc_file: str,
    log: Callable[[str], None] = print,
) -> int:
    """Replaces LTspice's ideal, zero-power digital primitives (device type
    'A': AND/OR/XOR/BUF) with QSpice-compatible behavioral voltage sources
    ('B' devices) implementing the identical boolean function of their wired
    inputs. This exists because QSpice's engine has no 'A' device support at
    all (confirmed empirically: it raises "Unknown device type: 'A'" on any
    such component), and QSpice's own native gate library (AND2, ...) is a
    different, physically-powered device needing Vdd/Vss wired to a real
    supply rail -- not a drop-in replacement, and not something this
    function invents wiring for.

    Pin connectivity is taken from a real, LTspice-resolved netlist when
    one is available (see _get_reliable_asc_netlist), and otherwise traced
    directly from this tool's own already-converted schematic wiring (see
    _build_qsch_wire_graph) -- a full multi-hop trace through the actual
    wire geometry, not a guess. Verified against a real 8-gate circuit
    with no .net file available: reproduces the exact same connectivity,
    pin for pin, as that same circuit's real LTspice-resolved .net gave.
    An earlier, cruder version of this same idea (spicelib's own single-
    hop component.ports lookup) was tried first and rejected after
    testing: it misread real signal pins as tied to ground because every
    wire this tool's own step-1 conversion writes carries a fixed literal
    "0" as its own stored net field, and a single-hop check trusts that
    instead of tracing further. The full graph trace here doesn't have
    that problem, since it follows the wire chain to whatever real label
    is actually reachable, however many hops away.

    Stateful primitives (flip-flops, counters, Schmitt triggers) are
    intentionally left untouched -- their behavior depends on past state or
    hysteresis, not just the current input voltages, so no fixed algebraic
    expression can reproduce them correctly; they need a real QSpice native
    flop/Schmitt symbol swapped in by hand.
    """
    from spicelib_vendor.editor.qsch_editor import QschTag

    components = list(getattr(qsch_editor, "components", {}).items())

    net_path = _get_reliable_asc_netlist(asc_file, log)
    if net_path:
        device_tokens = _parse_netlist_device_tokens(net_path)
    else:
        find, group_names, wired_points = _build_qsch_wire_graph(qsch_editor)

        # Wired-but-unlabeled groups (an LTspice auto-generated internal
        # node with no real FLAG) get a short "N###" name in the same
        # style LTspice itself already uses for this exact situation
        # elsewhere in the same circuit (e.g. "N003", "N004") -- checked
        # against every name already in use anywhere in the file first,
        # so it can't collide with a real, already-existing "N003" this
        # circuit happens to have from LTspice's own auto-numbering. A
        # longer, coordinate-based name was tried first and worked
        # correctly but looked like an obvious artifact next to the
        # circuit's own clean "N003"-style names -- this reads as
        # completely normal instead, because it's the same convention.
        _existing_names: Set[str] = {
            n.get_attr(5) for n in qsch_editor.schematic.get_items("net")
        }
        _internal_names: Dict[Tuple[int, int], str] = {}
        _next_internal = [1]

        def _internal_name_for(root: Tuple[int, int]) -> str:
            if root in _internal_names:
                return _internal_names[root]
            while True:
                candidate = f"N{_next_internal[0]:03d}"
                _next_internal[0] += 1
                if candidate not in _existing_names and candidate not in _internal_names.values():
                    break
            _internal_names[root] = candidate
            return candidate

        device_tokens = {}
        for refdes, comp in components:
            ref = str(_attr(comp, "reference", "refdes", "InstName", default=refdes)).strip() or str(refdes)
            if not ref.upper().startswith("A"):
                continue
            value = str(_attr(comp, "value", "Value", default="") or "").strip()
            model = bare_model_name(value).upper()
            if model not in DIGITAL_PRIMITIVE_STATELESS_MODELS:
                continue
            component_tag = _attr(comp, "tag", default=None)
            if component_tag is None:
                continue
            try:
                symbol_items = component_tag.get_items("symbol")
                symbol_tag = symbol_items[0] if symbol_items else None
                pins = list(symbol_tag.get_items("pin")) if symbol_tag is not None else []
                comp_pos = component_tag.get_attr(1)
                orientation = int(component_tag.get_attr(2))
            except Exception:
                continue
            if not pins or comp_pos is None:
                continue
            tokens: List[str] = []
            for pin in pins:
                try:
                    local_xy = pin.get_attr(1)
                except Exception:
                    tokens.append("0")
                    continue
                offset = _rotate_local_offset(local_xy, orientation)
                abs_pos = (comp_pos[0] + offset[0], comp_pos[1] + offset[1])
                if abs_pos not in wired_points:
                    tokens.append("0")  # genuinely unconnected in the original circuit
                    continue
                root = find(abs_pos)
                label = group_names.get(root)
                tokens.append(label if label else _internal_name_for(root))
            tokens.append(model)
            device_tokens[ref.upper()] = tokens

        if device_tokens:
            log(
                "  No .net file was found; resolved gate connectivity "
                f"directly from this circuit's own converted wiring instead "
                f"({len(device_tokens)} gate(s))."
            )
        else:
            log("")
            log("!" * 60)
            log("WARNING: DIGITAL GATES WILL NOT WORK IN QSPICE")
            log("!" * 60)
            log(
                "  No .net file was found for this circuit, and this "
                "circuit's own converted wiring didn't resolve any gates "
                "either (checked for LTspice itself to generate a .net, an "
                "existing sibling .net file, and this tool's own already-"
                "converted schematic wiring -- none gave usable results)."
            )
            log(
                "  Every LTspice ideal digital-gate primitive (AND/OR/NAND/"
                "etc.) in this circuit is being left completely unconverted "
                "as a result. Left as-is, these gates keep their original "
                "LTspice shape but QSpice's engine cannot simulate them at "
                "all -- expect a parse error naming one of these gates' "
                "refdes (A1, A2, ...) if you try to run this circuit as "
                "converted right now."
            )
            log("!" * 60)
            log("")
            return 0

    raw_data = _load_raw_for_logic_levels(asc_file, log)
    replaced = 0

    # Every net name a wire can be labeled with here falls into one of two
    # very different cases, and treating them the same is what caused
    # real floating-node simulation failures on a real circuit:
    #
    #  - EXPLICITLY NAMED nets (anything with a real FLAG in the original
    #    .asc, e.g. "Pre_Comparator_Q_Out_1") already have a "net" tag
    #    somewhere else in this schematic. QSpice matches same-named net
    #    tags by NAME, not position, so a new tag with the same name is
    #    safely connected no matter where it's physically drawn.
    #
    #  - AUTO-GENERATED internal names LTspice invents for an otherwise
    #    unlabeled wire (e.g. "N004") have NO tag anywhere else in the
    #    file -- the original connection existed purely through wires
    #    physically touching at an exact coordinate, never through a
    #    name. Dropping a same-named tag for one of these in a safe
    #    staging area (as an earlier version of this function did)
    #    creates a label that matches nothing: a real floating node,
    #    confirmed against a real circuit (QSpice's own "floating nodes"
    #    diagnostic named exactly the auto-generated nets this produced,
    #    and the simulation failed outright). These need the wire to
    #    physically reach the exact old pin position instead, to touch
    #    whatever original wire is still dangling there.
    #
    # _known_net_names starts as every net name that already has a tag
    # anywhere in the schematic (i.e. every genuinely-flagged net) and
    # grows as gates are processed, so two gates being replaced in the
    # same pass that share an auto-generated net between them (e.g. one
    # gate's output feeding another's input with no FLAG in between)
    # still get to use the safe, position-independent staging approach
    # once the first one has staked out that name.
    _known_net_names: Set[str] = {
        n.get_attr(5) for n in qsch_editor.schematic.get_items("net")
    }
    _original_known_net_names: Set[str] = set(_known_net_names)

    # LTspice's own auto-generated node names (e.g. "N003") for a wire it
    # never gave an explicit FLAG are NOT stable, portable identifiers --
    # they're just a sequential counter from THAT ONE netlisting pass, with
    # no meaning of their own. Confirmed the hard way on a real circuit:
    # QSpice_MCP's own schematic checker independently assigns its OWN
    # "N003"-style auto-names to unrelated unlabeled points while analyzing
    # the very same file (checked directly -- its connectivity report named
    # a completely different, unrelated pin "N003" too), and reusing
    # LTspice's short name verbatim as this tool's own net-tag text risked
    # colliding with that unrelated name, triggering a real
    # "conflicting_net_labels" error QSpice's checker didn't have before
    # this function touched the file. Every auto-generated name gets
    # rewritten here to something long and structurally distinct from any
    # plausible short auto-name a DIFFERENT tool's independent numbering
    # might produce, applied consistently to every gate that references the
    # same net in this pass (so a later gate's input matches an earlier
    # gate's output for the same net) -- this doesn't change what's
    # electrically connected (the wire-level connectivity, e.g. reaching
    # back to the original pin position, is unchanged), only the label text
    # written for it.
    _auto_net_rename: Dict[str, str] = {}

    def _resolve_net_name(raw_name: str) -> str:
        if raw_name in _original_known_net_names or raw_name == "0":
            return raw_name
        renamed = _auto_net_rename.get(raw_name)
        if renamed is None:
            renamed = f"QspiceSynAutoNet_{raw_name}"
            _auto_net_rename[raw_name] = renamed
            log(f"  Renamed LTspice's auto-generated net '{raw_name}' to '{renamed}' (avoids colliding with an unrelated same-named auto-net elsewhere).")
        return renamed

    # Placing a replacement gate's body at its own exact original position
    # is only safe when nothing else ends up sitting on top of it. Two
    # LTspice digital gates are routinely drawn close enough together
    # (confirmed on a real circuit: two adjacent gates only ~150 units
    # apart) that a real gate icon's own bounding box (roughly 600x600
    # units, from its widest embedded artwork) massively overlaps its
    # neighbor's if both are placed at their exact original origins --
    # visibly confirmed by the user's own QSpice screenshot showing two
    # gate bodies' curves crossing through each other. That kind of
    # overlap risks genuine coordinate collisions between unrelated pins/
    # wires, not just visual clutter. _resolve_collision_free_origin keeps
    # a gate at its exact original spot whenever there's room, and nudges
    # it the minimum distance needed to clear any already-placed gate
    # otherwise -- general collision avoidance, not a fix tuned to any one
    # circuit's specific layout.
    _placed_gate_origins: List[Tuple[float, float]] = []
    _MIN_GATE_SEPARATION = 700.0

    def _resolve_collision_free_origin(px: int, py: int) -> Tuple[int, int]:
        ox, oy = float(px), float(py)
        for _ in range(20):
            collided = False
            for opx, opy in _placed_gate_origins:
                dx, dy = ox - opx, oy - opy
                dist = math.hypot(dx, dy)
                if dist < _MIN_GATE_SEPARATION:
                    collided = True
                    if dist < 1e-6:
                        dx, dy, dist = _MIN_GATE_SEPARATION, 0.0, _MIN_GATE_SEPARATION
                    scale = (_MIN_GATE_SEPARATION - dist) / dist + 0.01
                    ox += dx * scale
                    oy += dy * scale
            if not collided:
                break
        result = (int(round(ox)), int(round(oy)))
        _placed_gate_origins.append(result)
        return result

    for refdes, comp in components:
        ref = str(_attr(comp, "reference", "refdes", "InstName", default=refdes)).strip() or str(refdes)
        if not ref.upper().startswith("A"):
            continue
        value = str(_attr(comp, "value", "Value", default="") or "").strip()
        model = bare_model_name(value).upper()
        if model not in DIGITAL_PRIMITIVE_STATELESS_MODELS:
            continue

        tokens = device_tokens.get(ref.upper())
        if not tokens:
            log(f"  SKIPPED {ref}: not found in the resolved netlist ({net_path}).")
            continue

        component_tag = _attr(comp, "tag", default=None)
        if component_tag is None:
            continue
        try:
            symbol_items = component_tag.get_items("symbol")
            symbol_tag = symbol_items[0] if symbol_items else None
            pins = list(symbol_tag.get_items("pin")) if symbol_tag is not None else []
        except Exception:
            pins = []
        if not pins:
            continue

        # tokens[i] corresponds to pins[i] positionally -- both preserve the
        # same order as the .asy's own PIN entries (SpiceOrder), which is how
        # spicelib's AsyReader draws them and how LTspice netlists them.
        node_count = min(len(pins), len(tokens) - 1)  # last token is the model name
        input_nets: List[str] = []
        # Parallel to input_nets: each input's own LTspice pin name (e.g.
        # "A", "B") and its location in the ORIGINAL gate artwork's local
        # coordinate frame (exactly where the drawn input lead line touches
        # the gate body) -- (net, pin_name, local_xy) triples, same order
        # as input_nets. Used below to give the synthesized replacement a
        # real pin at every original lead position, and to name the ports
        # of its embedded subcircuit. See the "QSpice-native multi-pin
        # gate" block further down for why this is needed.
        input_pin_locals: List[Tuple[str, str, Tuple[int, int]]] = []
        q_net = None
        nq_net = None
        q_local = nq_local = com_local = None
        for i in range(node_count):
            try:
                pin_name = pins[i].tokens[-1].strip('"')
                local_xy = pins[i].get_attr(1)
            except Exception:
                continue
            node = tokens[i]
            if pin_name == "Q":
                q_local = local_xy
            elif pin_name == "_Q":
                nq_local = local_xy
            elif pin_name == "com":
                com_local = local_xy
            if node == "0":
                continue
            if pin_name == "Q":
                q_net = node
            elif pin_name == "_Q":
                nq_net = node
            elif pin_name == "com":
                continue
            else:
                input_nets.append(node)
                if local_xy is not None:
                    input_pin_locals.append((node, pin_name, local_xy))

        if not input_nets or (q_net is None and nq_net is None):
            log(f"  SKIPPED {ref}: could not determine wired inputs/outputs from the netlist.")
            continue

        vlow, vhigh = DEFAULT_LOGIC_VLOW, DEFAULT_LOGIC_VHIGH
        for out_net in (q_net, nq_net):
            if out_net:
                levels = _lookup_raw_logic_levels(raw_data, out_net)
                if levels:
                    vlow, vhigh = levels
                    break
        vt = (vlow + vhigh) / 2.0

        # Renamed AFTER the raw-data lookup above (which must use LTspice's
        # own original name to find the matching trace in the .raw file)
        # but BEFORE anything else -- every remaining use of these values
        # (expressions, wire/net tags, the "already staked out this pass"
        # bookkeeping) should consistently see the renamed form from here
        # on, so a later gate consuming the same net as an earlier gate's
        # output still matches it correctly.
        q_net = _resolve_net_name(q_net) if q_net else None
        nq_net = _resolve_net_name(nq_net) if nq_net else None
        input_nets = [_resolve_net_name(n) for n in input_nets]
        input_pin_locals = [
            (_resolve_net_name(n), pin_name, local_xy) for n, pin_name, local_xy in input_pin_locals
        ]

        try:
            terms = [f"(V({n})>{vt:g})" for n in input_nets]
            bool_expr = _digital_bool_expr(model, terms)
        except ValueError as exc:
            log(f"  SKIPPED {ref}: {exc}")
            continue

        position = _attr(comp, "position", "pos", default=None)
        px, py = (position.X, position.Y) if position is not None else (0, 0)

        # Preserve the original gate's own drawn artwork (lines/arcs from the
        # LTspice symbol conversion) as pure decoration on the replacement --
        # keeps the familiar AND/OR gate silhouette on screen. Only "pin"
        # tags are real electrical connections in QSCH; a B-device's
        # netlist card is strictly 2 nodes (n+ n- V=expr), so the original
        # 5-8 pins (a..e, _Q, Q, com) cannot all be kept as real pins --
        # their nodes are referenced symbolically by name inside the
        # V=expression instead, which needs no physical wire. The 2 real
        # pins are placed at fixed, always-safe local offsets (not the old
        # gate's own pin coordinates) -- reusing the old coordinates was
        # tried and measurably collided with the old gate's own now-dangling
        # wires/labels still occupying that exact spot.
        geometry_items = []
        try:
            for item in symbol_tag.items:
                tag_name = item.tokens[0] if item.tokens else ""
                if tag_name not in ("pin", "text", "type:", "description:", "shorted pins:"):
                    geometry_items.append(item)
        except Exception:
            geometry_items = []

        removed = _remove_tag_from_tree(qsch_editor.schematic, component_tag)
        if not removed:
            log(f"  WARNING: could not remove {ref} from schematic tree cleanly; skipping.")
            continue
        qsch_editor.components.pop(refdes, None)

        # QSpice's own native gate library (AND2/OR2/etc, sourced directly
        # from the user's own compiled QSpice library file -- see
        # _QSPICE_NATIVE_GATE_LIBRARY's header) renders using QSpice's own
        # real gate icons, but its "type: ¥" token -- copied verbatim from
        # a raw library-sheet export -- is a placeholder QSpice's own GUI
        # resolves internally on real placement. Confirmed broken on a
        # real simulation attempt: it reproduces the exact "Fatal error:
        # Unknown device type: a" this tool exists to fix in the first
        # place, instead of resolving to a real native part. Disabled
        # until a concrete, netlist-safe type code is confirmed; the
        # kept-artwork "type: X" + embedded one-line subcircuit path below
        # is the one actually confirmed simulating correctly in real
        # QSpice.
        _USE_NATIVE_QSPICE_GATE_LIBRARY = False
        native_lookup = (
            _lookup_native_gate_shape(model, len(input_pin_locals), bool(q_net), bool(nq_net))
            if _USE_NATIVE_QSPICE_GATE_LIBRARY
            else None
        )
        if (
            input_pin_locals
            and len(input_pin_locals) == len(input_nets)
            and native_lookup
        ):
            # --- QSpice's own native gate library (Behavioral > Gates) ---
            #
            # shape/native_q_pin/native_nq_pin come from a lookup table
            # built directly from a real QSpice gate-library export (see
            # _QSPICE_NATIVE_GATE_LIBRARY) -- every geometry/pin coordinate
            # placed below is copied exactly from that file, not hand-drawn
            # or extrapolated, so this renders using QSpice's own built-in
            # appearance.
            #
            # NAND/NOR/XNOR/INV reuse the same part as their non-inverted
            # counterpart (AND/OR/XOR/BUF) rather than a separate symbol:
            # the real parts already expose both the non-inverted (Q) and
            # inverted (¬Q) output as real pins, so e.g. a NAND gate is
            # just an AND part wired to ¬Q as its primary output -- this
            # is QSpice's own "AND2 / AND2_Q" naming convention, not a
            # guess. See _lookup_native_gate_shape for exactly which pin
            # each model's own Q/_Q maps to.
            #
            # Unlike LTspice's ideal digital primitive, this real QSpice
            # part is physically powered (confirmed by QSpice's own
            # documentation and by the exported file having real Vdd/Vss
            # pins) -- so it needs an actual supply connection, which
            # _get_or_create_logic_supply_net adds once and every native
            # gate shares.
            #
            # One symbol now covers BOTH outputs when the original gate
            # had a wired complementary tap (Q and _Q), instead of the
            # separate small "auxiliary bubble" component used for that
            # case elsewhere in this function -- the real part already has
            # both outputs built in.
            shape, native_q_pin, native_nq_pin = native_lookup
            native_pins = {
                name: _parse_xy(xy_str) for name, xy_str, _no_connect in shape["pins"]
            }
            native_no_connect = {
                name: no_connect for name, _xy_str, no_connect in shape["pins"]
            }
            function = _QSPICE_NATIVE_GATE_FUNCTION[model][0]

            # At the gate's own original position, or the nearest
            # collision-free point to it -- see _resolve_collision_free_origin.
            ox, oy = _resolve_collision_free_origin(px, py)
            comp_tag = QschTag()
            comp_tag.tokens = ["component", f"({ox},{oy})", "0", "0"]
            sym_tag = QschTag("symbol", shape["symbol_name"])
            type_tag, _ = QschTag.parse("«type: ¥»")
            sym_tag.items.append(type_tag)
            desc_tag, _ = QschTag.parse(f'«description: {shape["description"]}»')
            sym_tag.items.append(desc_tag)
            shorted_tag, _ = QschTag.parse("«shorted pins: false»")
            sym_tag.items.append(shorted_tag)
            for raw_item in shape["geometry"]:
                tag, _ = QschTag.parse(f"«{raw_item}»")
                sym_tag.items.append(tag)

            ref_txt, _ = QschTag.parse(
                f'«text (200,{native_pins["Vdd"][1]}) 1 7 0 0x1000000 -1 -1 "¥{ref}"»'
            )
            val_txt, _ = QschTag.parse(f'«text (0,0) 1 0 2 0x1000000 -1 -1 "{function}"»')
            sym_tag.items.append(ref_txt)
            sym_tag.items.append(val_txt)

            for pin_name, local_xy in native_pins.items():
                if native_no_connect[pin_name]:
                    # Preserve QSpice's own "this output isn't present on
                    # this variant" marker exactly, rather than silently
                    # dropping the pin -- matches the confirmed reference
                    # file's own convention for e.g. AND2Q's unused ¬Q.
                    pin_tag, _ = QschTag.parse(
                        f'«pin ({local_xy[0]},{local_xy[1]}) (0,0) 1 0 0 0x1000000 -1 "{pin_name}" "¥"»'
                    )
                else:
                    pin_tag, _ = QschTag.parse(
                        f'«pin ({local_xy[0]},{local_xy[1]}) (0,0) 1 0 0 0x1000000 -1 "{pin_name}"»'
                    )
                sym_tag.items.append(pin_tag)

            comp_tag.items.append(sym_tag)
            qsch_editor.schematic.items.append(comp_tag)

            supply_net, supply_plus, supply_minus = _get_or_create_logic_supply_net(
                qsch_editor, vhigh, ox, oy, log
            )

            def _wire_direct_to_shared_point(pin_name: str, dest_abs: Tuple[int, int], net_name: str) -> None:
                # Used for Vdd/Vss only: connects straight to the shared
                # supply source's own already-labeled terminal with a
                # plain wire, instead of adding another same-named net
                # label next to this gate. QSpice resolves the net at
                # that shared endpoint from the label already sitting
                # there (checked before it ever looks at this wire's own
                # name), so no new label is needed here at all -- that's
                # what keeps 8 gates sharing one supply down to a single
                # "N_QSpiceLogicVddXV" label instead of 8 copies of it.
                local_xy = native_pins[pin_name]
                pin_abs = (ox + local_xy[0], oy + local_xy[1])
                wire_tag, _ = QschTag.parse(
                    f'«wire ({pin_abs[0]},{pin_abs[1]}) ({dest_abs[0]},{dest_abs[1]}) "{net_name}"»'
                )
                qsch_editor.schematic.items.append(wire_tag)

            def _wire_native_pin(
                pin_name: str, net_name: str, old_abs: Optional[Tuple[int, int]] = None
            ) -> None:
                # Short local stub + net label, same as every other
                # cross-schematic signal tap in this circuit -- see
                # _connect_synth_pin_by_label for why this never needs a
                # long reach-back wire even for an unlabeled/auto-generated
                # net.
                local_xy = native_pins[pin_name]
                _connect_synth_pin_by_label(
                    qsch_editor, ox, oy, local_xy, net_name, old_abs, _known_net_names
                )

            _wire_direct_to_shared_point("Vdd", supply_plus, supply_net)
            _wire_direct_to_shared_point("Vss", supply_minus, "0")
            for i, (node, _pin_name, local_xy) in enumerate(input_pin_locals):
                _wire_native_pin(f"b{i}", node, (px + local_xy[0], py + local_xy[1]))
            if q_net and not native_no_connect[native_q_pin]:
                q_old_abs = (px + q_local[0], py + q_local[1]) if q_local is not None else None
                _wire_native_pin(native_q_pin, q_net, q_old_abs)
            if nq_net and not native_no_connect[native_nq_pin]:
                nq_old_abs = (px + nq_local[0], py + nq_local[1]) if nq_local is not None else None
                _wire_native_pin(native_nq_pin, nq_net, nq_old_abs)

            qsch_editor.canvas_updated = True
            replaced += 1
            out_desc_bits = [f"Q={q_net}" if q_net else None, f"¬Q={nq_net}" if nq_net else None]
            out_desc = ", ".join(b for b in out_desc_bits if b)
            log(
                f"  Synthesized {ref} ({model}, inputs={','.join(input_nets)}) -> "
                f"QSpice native {shape['symbol_name']} gate ¥{ref} ({out_desc}) "
                f"[Vdd={vhigh:g}V via {supply_net}]"
            )
            continue

        # Whether the gate's real artwork can be reused doesn't depend on
        # which output is being wired -- it's the same yes/no for the whole
        # gate. Checking it once here (rather than per-output, inside the
        # loop below) is what fixes a real, general bug: the original
        # LTspice gate is ONE component with up to two output pins (Q and
        # _Q) on the SAME body, but wiring Q and _Q as two separate loop
        # iterations built two ENTIRE separate gate icons, one full-size
        # body per output, stacked on top of each other -- confirmed
        # directly from the user's own QSpice screenshot showing two
        # overlapping AND-gate curves at every gate with a wired
        # complementary tap. One component per gate, with both outputs as
        # real pins on that one body, matches the original 1:1 and applies
        # to any gate in any circuit, not just this one.
        use_native_shape = geometry_items and len(input_pin_locals) == len(input_nets)

        if use_native_shape:
            ox, oy = _resolve_collision_free_origin(px, py)
            comp_tag = QschTag()
            comp_tag.tokens = ["component", f"({ox},{oy})", "0", "0"]
            sym_tag = QschTag("symbol", "X")
            type_tag, _ = QschTag.parse("«type: X»")
            sym_tag.items.append(type_tag)

            desc_tag, _ = QschTag.parse(
                f'«description: {model} gate, synthesized from an LTspice ideal digital '
                f'primitive as a QSpice subcircuit wrapping a behavioral source»'
            )
            sym_tag.items.append(desc_tag)
            shorted_tag, _ = QschTag.parse("«shorted pins: false»")
            sym_tag.items.append(shorted_tag)
            for item in geometry_items:
                sym_tag.items.append(item)

            port_names = [pin_name for _, pin_name, _ in input_pin_locals]
            local_terms = [f"(V({pn})>{vt:g})" for pn in port_names]
            local_bool_expr = _digital_bool_expr(model, local_terms)

            out_ports: List[str] = []
            body_lines: List[str] = []
            pin_specs: List[Tuple[str, Tuple[int, int]]] = list(
                (pin_name, local_xy) for _, pin_name, local_xy in input_pin_locals
            )
            if q_net:
                out_ports.append("Q")
                body_lines.append(
                    f"B1 Q 0 V={vlow:g}+{(vhigh-vlow):g}*({local_bool_expr})"
                )
                pin_specs.append(("Q", q_local if q_local is not None else (200, 100)))
            if nq_net:
                out_ports.append("NQ")
                body_lines.append(
                    f"B2 NQ 0 V={vlow:g}+{(vhigh-vlow):g}*(1-({local_bool_expr}))"
                )
                pin_specs.append(("NQ", nq_local if nq_local is not None else (200, -100)))

            subckt_name = f"GATE_{ref}_{model}_{len(port_names)}"
            subckt_ports = " ".join(port_names + out_ports)
            lib_text = (
                f"|.subckt {subckt_name} {subckt_ports}\\n"
                + "\\n".join(body_lines)
                + "\\n.ends"
            )
            lib_tag = QschTag("library file:", lib_text)
            sym_tag.items.append(lib_tag)

            bref = f"X{ref}"
            ref_txt, _ = QschTag.parse(f'«text (130,220) 1 7 0 0x1000000 -1 -1 "{bref}"»')
            val_txt, _ = QschTag.parse(
                f'«text (-130,-350) 0 7 0 0x1000000 -1 -1 "{subckt_name}"»'
            )
            sym_tag.items.append(ref_txt)
            sym_tag.items.append(val_txt)

            # Real pin per input plus every wired output, all at their
            # ORIGINAL artwork positions, same order as subckt_ports (the
            # order QschEditor reads pins back in to build the instance's
            # node list on reload).
            for pin_name, local_xy in pin_specs:
                lx, ly = local_xy
                pin_tag, _ = QschTag.parse(
                    f'«pin ({lx},{ly}) (0,0) 1 0 0 0x1000000 -1 "{pin_name}"»'
                )
                sym_tag.items.append(pin_tag)

            comp_tag.items.append(sym_tag)
            qsch_editor.schematic.items.append(comp_tag)

            # Short local stub + net label for each pin, exactly like
            # every other cross-schematic signal tap in this circuit --
            # never a long wire physically reaching back to the pin's
            # original artwork position. See _connect_synth_pin_by_label.
            for net_name, _, local_xy in input_pin_locals:
                orig_abs = (px + local_xy[0], py + local_xy[1])
                _connect_synth_pin_by_label(
                    qsch_editor, ox, oy, local_xy, net_name, orig_abs, _known_net_names
                )
            if q_net:
                q_xy = pin_specs[len(input_pin_locals)][1]
                orig_q_abs = (px + q_xy[0], py + q_xy[1])
                _connect_synth_pin_by_label(
                    qsch_editor, ox, oy, q_xy, q_net, orig_q_abs, _known_net_names
                )
            if nq_net:
                nq_xy = pin_specs[-1][1]
                orig_nq_abs = (px + nq_xy[0], py + nq_xy[1])
                _connect_synth_pin_by_label(
                    qsch_editor, ox, oy, nq_xy, nq_net, orig_nq_abs, _known_net_names
                )

            qsch_editor.canvas_updated = True
            replaced += 1
            out_desc_bits = [f"Q={q_net}" if q_net else None, f"¬Q={nq_net}" if nq_net else None]
            out_desc = ", ".join(b for b in out_desc_bits if b)
            log(
                f"  Synthesized {ref} ({model}, inputs={','.join(input_nets)}) -> "
                f"{out_desc} [Vlow={vlow:g} Vhigh={vhigh:g}]"
            )
            continue

        outputs = []
        if q_net:
            outputs.append((f"X{ref}", q_net, f"{vlow:g}+{(vhigh-vlow):g}*({bool_expr})", q_local))
        if nq_net:
            outputs.append((f"X{ref}N", nq_net, f"{vlow:g}+{(vhigh-vlow):g}*(1-({bool_expr}))", nq_local))

        for i, (bref, outnet, expr, out_local) in enumerate(outputs):
            # Fallback path only (use_native_shape is always False here --
            # handled and continue'd above otherwise): both outputs are
            # small auxiliary behavioral-source bubbles, positioned near
            # each other since neither gets the real gate artwork.
            ox, oy = _resolve_collision_free_origin(px if i == 0 else px + 900, py if i == 0 else py + 500)

            # --- Fallback: plain 2-pin behavioral source ---
            # Used for the complementary (_Q) tap, and for a primary output
            # that has no kept artwork or incomplete pin-position data to
            # build the native-shaped version above safely.
            comp_tag = QschTag()
            comp_tag.tokens = ["component", f"({ox},{oy})", "0", "0"]
            sym_tag = QschTag("symbol", "B")

            type_tag, _ = QschTag.parse("«type: B»")
            sym_tag.items.append(type_tag)

            if i == 0:
                desc_tag, _ = QschTag.parse(
                    f'«description: {model} gate, synthesized from an LTspice ideal digital '
                    f'primitive as a QSpice-compatible behavioral source»'
                )
                sym_tag.items.append(desc_tag)
                shorted_tag, _ = QschTag.parse("«shorted pins: false»")
                sym_tag.items.append(shorted_tag)
                for item in geometry_items:
                    sym_tag.items.append(item)
            else:
                # Complementary (_Q) tap on a gate whose primary output is
                # already drawn above: shown as a small auxiliary source
                # bubble (QSpice's own standard behavioral-source shape)
                # rather than a second overlapping gate icon.
                desc_tag, _ = QschTag.parse(
                    "«description: Complementary output tap, synthesized as a QSpice-compatible behavioral source»"
                )
                sym_tag.items.append(desc_tag)
                for raw_item in (
                    '«shorted pins: false»',
                    '«line (0,-130) (0,-200) 0 0 0x1000000 -1 -1»',
                    '«line (0,200) (0,130) 0 0 0x1000000 -1 -1»',
                    '«rect (-25,77) (25,73) 0 0 0 0x1000000 0x3000000 -1 0 -1»',
                    '«rect (-2,50) (2,100) 0 0 0 0x1000000 0x3000000 -1 0 -1»',
                    '«rect (-25,-73) (25,-77) 0 0 0 0x1000000 0x3000000 -1 0 -1»',
                    '«ellipse (-130,130) (130,-130) 0 0 0 0x1000000 0x1000000 -1 -1»',
                ):
                    tag, _ = QschTag.parse(raw_item)
                    sym_tag.items.append(tag)

            # Ref/value text and the 2 real pins are identical regardless of
            # which decoration was drawn above -- fixed, proven-safe offsets.
            ref_txt, _ = QschTag.parse(f'«text (130,220) 1 7 0 0x1000000 -1 -1 "{bref}"»')
            # Value text placed well below the symbol (not beside it) and at
            # the smallest size code, specifically because these are full
            # boolean expressions -- much longer than a typical component's
            # value -- and were measurably overlapping neighboring
            # components when shown at normal size right next to the symbol.
            val_txt, _ = QschTag.parse(f'«text (-130,-350) 0 7 0 0x1000000 -1 -1 "V={expr}"»')
            plus_pin, _ = QschTag.parse('«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»')
            minus_pin, _ = QschTag.parse('«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»')
            sym_tag.items.append(ref_txt)
            sym_tag.items.append(val_txt)
            sym_tag.items.append(plus_pin)
            sym_tag.items.append(minus_pin)
            plus_xy, minus_xy = (0, 200), (0, -200)

            comp_tag.items.append(sym_tag)
            qsch_editor.schematic.items.append(comp_tag)

            # Real LTspice/QSpice net labels are never placed directly on a
            # pin -- every FLAG sits at the far end of a short WIRE stub
            # drawn out from the pin (confirmed against this exact circuit's
            # own .asc), so both pins get a genuine local stub. The "+" pin
            # uses the shared label-based connection helper (short stub, no
            # long reach-back wire); the "-" pin is always local ground, so
            # it just gets its own short stub directly.
            orig_plus_abs = (px + plus_xy[0], py + plus_xy[1])
            _connect_synth_pin_by_label(
                qsch_editor, ox, oy, plus_xy, outnet, orig_plus_abs, _known_net_names, stub_len=60
            )
            minus_stub = (ox + minus_xy[0], oy + minus_xy[1] - 60)
            minus_wire, _ = QschTag.parse(
                f'«wire ({ox+minus_xy[0]},{oy+minus_xy[1]}) ({minus_stub[0]},{minus_stub[1]}) "0"»'
            )
            qsch_editor.schematic.items.append(minus_wire)
            minus_net, _ = QschTag.parse(f'«net ({minus_stub[0]},{minus_stub[1]}) 1 13 0 "0"»')
            qsch_editor.schematic.items.append(minus_net)

        qsch_editor.canvas_updated = True
        replaced += 1
        out_desc = ", ".join(f"{n}={t}" for n, t, _, _ in outputs)
        log(
            f"  Synthesized {ref} ({model}, inputs={','.join(input_nets)}) -> "
            f"{out_desc} [Vlow={vlow:g} Vhigh={vhigh:g}]"
        )

    return replaced


def process_models(
    asc_file: str,
    qsch_file: str,
    search_roots: List[str],
    choose_model_path: Callable[[str, List[str]], Optional[str]],
    log: Callable[[str], None] = print,
    fix_annotations: bool = True,
    replace_zero_ohm: bool = True,
    apply_uic_workaround: bool = False,
    choose_device_model: Optional[
        Callable[[str, List[str], List[tuple]], Optional[dict]]
    ] = None,
    review_choices: Optional[
        Callable[[str, Dict[str, list], dict, Callable[[str], Any]], dict]
    ] = None,
    confirm_before_generating: Optional[Callable[[dict, dict], bool]] = None,
) -> ProcessingResult:
    """review_choices and confirm_before_generating are both optional and
    default to None, which reproduces this function's original behavior
    exactly (ask once per model, inject immediately) for any caller that
    doesn't pass them -- e.g. the GUI. Passing them (as the CLI does) only
    changes WHEN each already-decided choice gets written to the schematic
    relative to the interactive prompts; it does not change what gets
    detected, what gets asked, what candidates are offered, or what ends
    up in the final file for a given set of answers.

    review_choices(category_label, items_by_model, choices, redo_one) is
    called once per category (library imports, then device models) right
    after every model in that category has been asked once. It gets the
    category's full item list, the choices collected so far, and a
    `redo_one(model) -> new_choice` callback for re-asking a single model;
    it returns the (possibly revised) choices dict to use.

    confirm_before_generating(resolved_paths, device_choices) is called
    once, after both categories are fully reviewed, with the final
    combined choices; returning False skips every library/model injection
    (the automatic fixups below -- annotation cleanup, zero-ohm
    replacement, inductor damping -- and the final save still happen, same
    as if every remaining choice had been individually skipped).
    """
    _patch_spicelib_qsch_colon_parsing(log=log)
    _clear_model_candidate_cache()
    asc_map = parse_asc_ref_to_symbol(asc_file)
    x_refs = parse_qsch_x_type_refs(qsch_file)
    missing_by_model: Dict[str, List[str]] = defaultdict(list)
    qspice_local_count = 0

    all_roots = list(search_roots)
    for r in get_default_search_roots(asc_file):
        if r not in all_roots:
            all_roots.append(r)

    for ref in x_refs:
        symtype = asc_map.get(ref)
        if symtype is None:
            continue
        bare = bare_model_name(symtype)
        if is_qspice_local_library_component(bare):
            log(f"  [QSpice Native Library] {ref} ({bare}) is natively supported by QSpice; no external .lib required.")
            qspice_local_count += 1
            continue
        if (
            bare.lower() in BUILTIN_PRIMITIVES
            or bare.lower() in GENERIC_OPAMP_SYMBOLS
        ):
            continue
        missing_by_model[bare].append(ref)

    if not missing_by_model:
        log("No unresolved subcircuit components found via ASC map.")

    log("=" * 60)
    log("COMPONENTS NEEDING VALUE FIX + MODEL IMPORT")
    log("=" * 60)
    for model, refs in missing_by_model.items():
        log(f"  {model}  ({len(refs)} component(s): {', '.join(sorted(refs))})")
    log("")
    resolved_paths: Dict[str, str] = {}
    for model in sorted(missing_by_model.keys()):
        refs = missing_by_model[model]
        log(f"--- {model} (used by {len(refs)} component(s)) ---")
        candidates = find_model_candidates(model, all_roots)
        chosen = choose_model_path(model, candidates)
        if chosen:
            chosen = _clean_path(chosen)
            resolved_paths[model] = chosen
            log(f"  Selected: {chosen}")
        else:
            log(
                f"  SKIPPED -- {model} value will still be fixed, "
                f"but no .lib will be imported for it."
            )

    def _redo_lib_choice(model: str) -> Optional[str]:
        chosen = choose_model_path(model, find_model_candidates(model, all_roots))
        return _clean_path(chosen) if chosen else None
    # Reviewed together with device models below, in one combined pass --
    # see the single review_choices call after device models are gathered.

    qsch_editor = QschEditor(qsch_file)

    log("")
    log("=" * 60)
    log("SYNTHESIZING QSPICE-COMPATIBLE LOGIC FOR LTSPICE DIGITAL PRIMITIVES")
    log("=" * 60)
    digital_primitives_synthesized = synthesize_ltspice_digital_primitives(
        qsch_editor, asc_file, log=log
    )
    if digital_primitives_synthesized:
        force_save_qsch(qsch_editor, qsch_file)
        qsch_editor = QschEditor(qsch_file)

    fixed_count = 0
    for model, refs in missing_by_model.items():
        for ref in refs:
            try:
                qsch_editor.set_component_value(ref, model)
                fixed_count += 1
            except Exception as e:
                log(f"  WARNING: could not set value for {ref}: {e}")
    # Library injection for resolved_paths is deferred to the single "apply
    # everything" block below (after device-model choices are collected and
    # reviewed too), so a caller using review_choices/confirm_before_generating
    # gets one final combined confirmation before ANYTHING is actually
    # written -- see that block for the actual _inject_lib_instruction calls.

    device_model_refs = parse_qsch_primitive_device_refs(qsch_editor, log=log)
    existing_models = _existing_model_definitions(qsch_editor)
    unresolved_device_models = {
        name: refs
        for name, refs in device_model_refs.items()
        if name not in existing_models and name not in resolved_paths and not is_qspice_local_library_component(name)
    }
    device_models_injected = 0
    device_models_skipped: List[str] = []
    device_choices: Dict[str, dict] = {}
    reviewable_device_models: Dict[str, list] = {}
    log("")
    log("=" * 60)
    log("UNRESOLVED COMPONENT MODELS (Diodes / BJTs / MOSFETs / Subcircuits)")
    log("=" * 60)
    if not unresolved_device_models:
        log("  None found -- every component model is already satisfied.")
    else:
        for model in sorted(unresolved_device_models.keys()):
            refs = unresolved_device_models[model]
            ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
            log(f"  {model}  ({len(refs)} component(s): {ref_desc})")
        log("")
        if choose_device_model is None:
            log("  No device-model chooser was provided; skipping this step.")
            device_models_skipped = sorted(unresolved_device_models.keys())
        else:
            for model in sorted(unresolved_device_models.keys()):
                refs = unresolved_device_models[model]
                log(f"--- {model} (used by {len(refs)} component(s)) ---")
                if model.lower() in LTSPICE_GENERIC_DIGITAL_PRIMITIVES:
                    # These gates are meant to be resolved automatically by
                    # synthesize_ltspice_digital_primitives earlier in this
                    # same run -- if one lands here instead, that step
                    # didn't run or didn't cover it (most commonly: no real
                    # LTspice-resolved .net file was available for pin
                    # connectivity, so it was skipped entirely rather than
                    # risk wrong wiring). Asking the person to hand-pick a
                    # QSpice gate here wouldn't actually fix anything --
                    # this fallback only sets the component's displayed
                    # value text, it can't turn a still-unsupported "type:
                    # A" component into a real, wired, simulating gate, so
                    # the real fix is upstream: get that gate covered by
                    # synthesis instead, not paper over it here. Not a real
                    # choice, so it's never offered for review/redo either.
                    log(
                        f"  '{model}' ({', '.join(r for r, _ in refs)}) is an LTspice ideal "
                        f"digital-gate placeholder that QSpice's own engine can't simulate at "
                        f"all as-is. It's meant to be replaced automatically earlier in this "
                        f"run, and wasn't -- almost always because no real LTspice-resolved "
                        f".net file was available for its actual pin connectivity (see the "
                        f"'SYNTHESIZING...' section above for why). Re-run this conversion "
                        f"with a .net file for this circuit sitting next to the .asc (open the "
                        f"circuit in LTspice and run it once, or use Tools > Create Netlist, "
                        f"to generate one) rather than picking a model by hand here -- a manual "
                        f"pick can't make this component simulate correctly on its own."
                    )
                    device_models_skipped.append(model)
                    continue
                reviewable_device_models[model] = refs
                candidates = find_model_candidates(model, all_roots)
                device_choices[model] = choose_device_model(model, candidates, refs)

    def _redo_device_choice(model: str) -> Optional[dict]:
        refs = reviewable_device_models[model]
        return choose_device_model(
            model, find_model_candidates(model, all_roots), refs
        )

    # One combined review pass for both library imports and device models,
    # instead of two separate back-to-back review screens. The two stayed
    # structurally different underneath on purpose -- a library import
    # resolves to a plain file path (injected as a ".lib \"path\"" line)
    # while a device model resolves to a dict describing an extracted/
    # edited model card (injected as the card itself) -- but there was no
    # real reason to also split the *review UX* into two passes the user
    # has to step through one after another; that was reported as
    # confusing/inaccurate (easy to lose track of which screen a given
    # model belongs to) without changing what's actually correct here.
    # Keys are merged as-is; on the rare case a bare model name is used as
    # both a missing subcircuit AND a missing device model in the same
    # circuit, the device-model entry gets a suffixed key so neither is
    # silently dropped from the combined list.
    if review_choices is not None and (missing_by_model or reviewable_device_models):
        combined_items: Dict[str, list] = dict(missing_by_model)
        combined_choices: dict = dict(resolved_paths)
        key_to_model: Dict[str, Tuple[str, str]] = {
            model: ("library", model) for model in missing_by_model
        }
        for model, refs in reviewable_device_models.items():
            key = model if model not in combined_items else f"{model} [device model]"
            combined_items[key] = refs
            combined_choices[key] = device_choices.get(model)
            key_to_model[key] = ("device", model)

        def _redo_combined(key: str):
            kind, model = key_to_model[key]
            return _redo_lib_choice(model) if kind == "library" else _redo_device_choice(model)

        combined_choices = review_choices(
            "library imports and device models",
            combined_items,
            combined_choices,
            _redo_combined,
        )

        resolved_paths = {}
        device_choices = {}
        for key, choice in combined_choices.items():
            kind, model = key_to_model[key]
            if kind == "library":
                # resolved_paths only ever holds models with a real chosen
                # path (a skipped library import is simply absent as a key,
                # never present with None) -- the unguarded injection loop
                # further down assumes that invariant, so it's preserved
                # here rather than letting a redo-to-nothing add a None
                # entry the way review_choices' generic dict merge would.
                if choice:
                    resolved_paths[model] = choice
            else:
                device_choices[model] = choice

    # ---- Apply everything: library imports (resolved_paths) and device
    # models (device_choices) together, after both categories have been
    # asked and reviewed. A caller that didn't pass confirm_before_generating
    # gets proceed=True unconditionally, i.e. applies immediately exactly
    # like this function always did.
    proceed = True
    if confirm_before_generating is not None:
        proceed = confirm_before_generating(resolved_paths, device_choices)

    if not proceed:
        # A "No" answer here means abort entirely: don't inject anything,
        # don't run any of the automatic fixups below, and don't touch the
        # file any further -- previously this branch skipped injection
        # but the fixups and final save still ran regardless, silently
        # writing to disk even after the user had declined. The one thing
        # this can't undo is digital-gate synthesis: that already ran and
        # saved earlier in this same function, before this confirmation
        # point even exists, so it's not part of what "No" is declining.
        log("")
        log("Generation cancelled by user -- nothing further was written.")
        return ProcessingResult(
            fixed_count=fixed_count,
            injected_count=0,
            skipped_models=sorted(missing_by_model.keys()),
            resolved_paths=resolved_paths,
            reclassified_count=0,
            replaced_zero_ohm_count=0,
            device_models_injected=0,
            device_models_skipped=sorted(
                set(device_models_skipped) | set(device_choices.keys())
            ),
            inductors_damped_count=0,
            qspice_local_library_count=qspice_local_count,
            digital_primitives_synthesized=digital_primitives_synthesized,
            cancelled=True,
        )

    injected_count = 0
    for model, path in resolved_paths.items():
        path = _clean_path(path)
        log(f'Injecting: .lib "{path}"')
        _inject_lib_instruction(qsch_editor, path, kind="lib", log=log)
        injected_count += 1
    skipped_libs = sorted(set(missing_by_model.keys()) - set(resolved_paths.keys()))

    for model, choice in device_choices.items():
        refs = unresolved_device_models[model]
        if not choice:
            log(f"  SKIPPED -- no definition assigned for {model}.")
            device_models_skipped.append(model)
            continue
        text = choice.get("inline") if isinstance(choice, dict) else choice
        path_file = choice.get("path") if isinstance(choice, dict) else None
        if text:
            n = validate_and_inject_device_model(
                qsch_editor, model, text, refs, log=log
            )
            if n:
                device_models_injected += 1
            else:
                device_models_skipped.append(model)
        elif path_file:
            _inject_lib_instruction(qsch_editor, path_file, kind="lib", log=log)
            device_models_injected += 1
        else:
            device_models_skipped.append(model)

    reclassified_count = 0
    if fix_annotations:
        log("")
        log("Checking for plain text annotations that should be comments...")
        try:
            reclassified_count = fix_misclassified_comments(
                qsch_editor, log=log
            )
        except Exception as e:
            log(f"  WARNING: annotation cleanup failed: {e}")
            reclassified_count = 0
        if reclassified_count == 0:
            log("  None found -- nothing to fix.")

    replaced_zero_ohm_count = 0
    if replace_zero_ohm:
        log("")
        log("Replacing literal 0-ohm resistors with wires...")
        try:
            replaced_zero_ohm_count = replace_zero_ohm_resistors_with_wires(
                qsch_editor, log=log
            )
        except Exception as e:
            log(f"  WARNING: zero-ohm replacement failed: {e}")
            replaced_zero_ohm_count = 0
        if replaced_zero_ohm_count == 0:
            log("  None found -- nothing to replace.")

    inductors_damped_count = 0
    log("")
    log("Ensuring standalone inductors have default series damping (Rser=1m)...")
    try:
        inductors_damped_count = ensure_inductor_damping(qsch_editor, log=log)
    except Exception as e:
        log(f"  WARNING: inductor damping check failed: {e}")
        inductors_damped_count = 0
    if inductors_damped_count == 0:
        log("  None needed damping adjustments.")

    tran_uic_rewritten_count = 0
    if apply_uic_workaround:
        log("")
        log("Replacing '.tran ... startup' (ignored by QSpice) with 'uic'...")
        try:
            tran_uic_rewritten_count = convert_startup_to_uic(qsch_editor, log=log)
        except Exception as e:
            log(f"  WARNING: uic workaround failed: {e}")
            tran_uic_rewritten_count = 0
        if tran_uic_rewritten_count == 0:
            log("  Nothing to do -- no '.tran ... startup' directive found.")

    log("")
    log("Normalizing .lib/.include instructions...")
    normalized_count = _normalize_lib_tags(qsch_editor, log=log)
    if normalized_count == 0:
        log("  None needed normalizing.")

    force_save_qsch(qsch_editor, qsch_file)
    log("")
    log(
        f"Done. {fixed_count} component value(s) corrected, "
        f"{injected_count} .lib instruction(s) injected, "
        f"{device_models_injected} device model/subcircuit definition(s) injected, "
        f"{qspice_local_count} component(s) mapped to QSpice native local library, "
        f"{digital_primitives_synthesized} LTspice digital primitive(s) synthesized as QSpice-compatible behavioral sources, "
        f"{reclassified_count} annotation(s) reclassified as comments, "
        f"{replaced_zero_ohm_count} zero-ohm resistor(s) replaced with wires, "
        f"{inductors_damped_count} inductor(s) auto-damped, "
        f"{tran_uic_rewritten_count} '.tran' directive(s) switched from startup to uic "
        f"in {qsch_file}"
    )
    skipped = sorted(set(skipped_libs))
    if skipped:
        log(f"Still no .lib imported for: {', '.join(skipped)}")
    if device_models_skipped:
        log(
            f"Still no model defined for: {', '.join(sorted(set(device_models_skipped)))}"
        )
    return ProcessingResult(
        fixed_count=fixed_count,
        injected_count=injected_count,
        skipped_models=skipped,
        resolved_paths=resolved_paths,
        reclassified_count=reclassified_count,
        replaced_zero_ohm_count=replaced_zero_ohm_count,
        device_models_injected=device_models_injected,
        device_models_skipped=sorted(set(device_models_skipped)),
        inductors_damped_count=inductors_damped_count,
        qspice_local_library_count=qspice_local_count,
        digital_primitives_synthesized=digital_primitives_synthesized,
        tran_uic_rewritten_count=tran_uic_rewritten_count,
    )


def choose_model_path_cli(model: str, candidates: List[str]) -> Optional[str]:
    if candidates:
        print(f"  Found {len(candidates)} candidate match(es):")
        for i, cand in enumerate(candidates, 1):
            print(f"    [{i}] {cand}")
        answer = input(
            f"  Select candidate [1-{len(candidates)}], enter custom path, or press Enter to skip: "
        ).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        elif answer:
            return answer
        return None

    manual = input(
        f"  Enter path to .lib/.cir file containing '{model}' (or press Enter to skip): "
    ).strip()
    return manual or None


def choose_model_path_gui(
    model: str, candidates: List[str], root
) -> Optional[str]:
    import tkinter as tk
    from tkinter import filedialog, ttk

    dialog = tk.Toplevel(root)
    dialog.title(f"Select Model File: {model}")
    dialog.geometry("620x320")
    dialog.transient(root)
    dialog.grab_set()

    result: Dict[str, Optional[str]] = {"path": None}

    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)

    ttk.Label(
        frame,
        text=f'Select library file for subcircuit "{model}"',
        font=("", 11, "bold"),
    ).pack(anchor="w", pady=(0, 8))

    if candidates:
        ttk.Label(
            frame,
            text=f"Autosearch found {len(candidates)} matching candidate file(s):",
        ).pack(anchor="w")

        combo_var = tk.StringVar(value=candidates[0])
        combo = ttk.Combobox(
            frame, textvariable=combo_var, values=candidates, state="readonly"
        )
        combo.pack(fill="x", pady=(4, 12))
    else:
        ttk.Label(
            frame,
            text="Autosearch found no matching candidate files automatically.",
        ).pack(anchor="w", pady=(0, 12))
        combo_var = tk.StringVar(value="")

    custom_box = ttk.LabelFrame(
        frame, text="Or select/edit path manually", padding=8
    )
    custom_box.pack(fill="x", pady=(0, 16))

    file_var = tk.StringVar()
    file_entry = ttk.Entry(custom_box, textvariable=file_var)
    file_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_manual():
        chosen = filedialog.askopenfilename(
            parent=dialog,
            title=f"Select file for {model}",
            filetypes=[
                ("SPICE library files", "*.lib *.cir *.sub *.txt *.mod *.bjt *.dio *.mos"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            file_var.set(chosen)

    ttk.Button(custom_box, text="Browse...", command=browse_manual).pack(
        side="right"
    )

    def confirm():
        chosen_path = file_var.get().strip() or combo_var.get().strip()
        if chosen_path:
            result["path"] = chosen_path
        dialog.destroy()

    def skip():
        dialog.destroy()

    btn_row = ttk.Frame(frame)
    btn_row.pack(fill="x")
    ttk.Button(btn_row, text="Confirm", command=confirm).pack(side="left")
    ttk.Button(btn_row, text="Skip", command=skip).pack(side="right")

    dialog.wait_window()
    return result["path"]


def choose_device_model_cli(
    model: str, candidates: List[str], refs: List[tuple]
) -> Optional[dict]:
    ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
    print(f"  Used by: {ref_desc}")
    if candidates:
        print(f"  Found {len(candidates)} candidate match(es):")
        for i, cand in enumerate(candidates, 1):
            print(f"    [{i}] {cand}")
        answer = input(
            f"  Select candidate [1-{len(candidates)}] or press Enter for manual/skip: "
        ).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            chosen = candidates[int(answer) - 1]
            extracted = _extract_model_definition(chosen, model, log=print)
            if extracted:
                classification = classify_spice_model(extracted)
                print(f"  Extracted definition [{classification['reason']}]:")
                print("    " + extracted.replace("\n", "\n    "))
                
                if classification["action"] == "INCLUDE_FILE":
                    print("  Recommendation: Include file path (.lib) directly.")
                    confirm = input("  Include path as .lib directive? [Y/n]: ").strip().lower()
                    if confirm in ("", "y"):
                        return {"path": chosen}
                    return {"inline": extracted}
                else:
                    confirm = (
                        input("  Inject primitive definition inline? [Y/n]: ")
                        .strip()
                        .lower()
                    )
                    if confirm in ("", "y"):
                        return {"inline": extracted}
                    return {"path": chosen}
            else:
                return {"path": chosen}

    print(
        f"  Type or paste the .model / .subckt line(s) for '{model}' directly.\n"
        f"  (leave blank to skip)"
    )
    lines = []
    while True:
        line = input("    ")
        if not line.strip():
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return {"inline": text} if text else None


def _describe_cli_choice(choice) -> str:
    """One-line human-readable summary of a chooser's return value, for the
    CLI review screen -- handles both a plain path string (library-import
    choices) and a {"path"/"inline": ...} dict (device-model choices)."""
    if not choice:
        return "(skipped)"
    if isinstance(choice, dict):
        if choice.get("path"):
            return f'.lib "{choice["path"]}"'
        inline = choice.get("inline")
        if inline:
            first_line = inline.splitlines()[0] if inline else ""
            return f"inline definition ({first_line})" if first_line else "inline definition"
        return "(skipped)"
    return str(choice)


def review_choices_cli(
    category_label: str,
    items_by_model: Dict[str, list],
    choices: dict,
    redo_one: Callable[[str], Any],
) -> dict:
    """CLI implementation of process_models' review_choices hook: shows
    every choice made so far for one category (library imports, or device
    models), and lets the user pick any of them by number to redo before
    moving on -- so picking the wrong candidate for an earlier model
    doesn't mean starting the whole conversion over.
    """
    if not items_by_model:
        return choices
    choices = dict(choices)
    models = sorted(items_by_model.keys())
    while True:
        print()
        print(f"--- Review: {category_label} ---")
        for i, model in enumerate(models, 1):
            print(f"  [{i}] {model}: {_describe_cli_choice(choices.get(model))}")
        answer = input(
            "Press Enter to confirm these and continue, or enter a number to redo one: "
        ).strip()
        if not answer:
            return choices
        if answer.isdigit() and 1 <= int(answer) <= len(models):
            model = models[int(answer) - 1]
            print(f"Re-choosing {model}:")
            choices[model] = redo_one(model)
        else:
            print("  Not a valid choice; try again.")


def confirm_and_generate_cli(resolved_paths: dict, device_choices: dict) -> bool:
    """CLI implementation of process_models' confirm_before_generating
    hook: shows every library and device-model choice together one last
    time, and only writes anything to the schematic once the user
    explicitly confirms."""
    print()
    print("=" * 60)
    print("PHASE 2: CONFIRM AND GENERATE SCHEMATIC")
    print("=" * 60)
    if not resolved_paths and not device_choices:
        print("  No library imports or device models to inject.")
    else:
        if resolved_paths:
            print("  Library imports:")
            for model, path in sorted(resolved_paths.items()):
                print(f"    {model} -> {path}")
        if device_choices:
            print("  Device models:")
            for model, choice in sorted(device_choices.items()):
                print(f"    {model} -> {_describe_cli_choice(choice)}")
    try:
        answer = (
            input("Generate the final QSpice schematic with these choices? [Y/n]: ")
            .strip()
            .lower()
        )
    except Exception:
        # No real console attached to ask from (e.g. launched via a file
        # association) -- the whole point of this step is to let the user
        # back out if something looks wrong in the review above, so the
        # safe default when it genuinely can't be asked is "don't
        # generate," not "assume yes." Silently defaulting to yes here
        # would just be an unconfirmed auto-generate wearing a
        # confirmation dialog's clothes.
        print(
            "  NOTE: no console available to confirm -- treating as declined. "
            "Nothing was written."
        )
        return False
    return answer in ("", "y", "yes")


def choose_device_model_gui(
    model: str, candidates: List[str], refs: List[tuple], root
) -> Optional[dict]:
    import tkinter as tk
    from tkinter import filedialog, ttk

    dialog = tk.Toplevel(root)
    dialog.title(f"Resolve model definition: {model}")
    dialog.geometry("680x560")
    dialog.transient(root)
    dialog.grab_set()

    result: dict = {}

    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)

    ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
    ttk.Label(
        frame,
        text=f'Model definition lookup for "{model}"',
        font=("", 11, "bold"),
    ).pack(anchor="w")
    ttk.Label(frame, text=f"Used by: {ref_desc}", wraplength=640).pack(
        anchor="w", pady=(2, 10)
    )

    file_box = ttk.LabelFrame(
        frame,
        text="Candidate Library Files",
        padding=8,
    )
    file_box.pack(fill="x", pady=(0, 10))

    file_var = tk.StringVar()

    if candidates:
        combo = ttk.Combobox(
            file_box, textvariable=file_var, values=candidates, state="readonly"
        )
        combo.pack(fill="x", pady=(0, 4))
        combo.current(0)
    else:
        file_entry = ttk.Entry(file_box, textvariable=file_var)
        file_entry.pack(fill="x", pady=(0, 4), side="left", expand=True)

    status_label = ttk.Label(
        frame, text="", foreground="#333333", wraplength=640
    )
    status_label.pack(anchor="w", pady=(0, 4))

    inline_box = ttk.LabelFrame(
        frame, text="Live Extracted / Editable SPICE Definition", padding=8
    )
    inline_box.pack(fill="both", expand=True, pady=(0, 10))
    text_widget = tk.Text(inline_box, height=8, wrap="word")
    text_widget.pack(fill="both", expand=True)

    def _update_extracted_view(path: str) -> None:
        path = path.strip()
        text_widget.delete("1.0", "end")
        if not path or not os.path.isfile(path):
            status_label.config(text="No valid file selected.")
            return
        extracted = _extract_model_definition(path, model)
        if extracted:
            classification = classify_spice_model(extracted)
            text_widget.insert("1.0", extracted)
            status_label.config(
                text=f"Extracted definition for '{model}' from {os.path.basename(path)} "
                     f"[{classification['action']}: {classification['reason']}]"
            )
        else:
            status_label.config(
                text=f"No explicit definition for '{model}' found in {os.path.basename(path)} -- paste manually below."
            )

    def browse_file() -> None:
        chosen = filedialog.askopenfilename(
            parent=dialog,
            title=f"Select file defining '{model}'",
            filetypes=[
                ("SPICE library files", "*.lib *.cir *.sub *.mod *.txt *.bjt *.dio *.mos"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            file_var.set(chosen)
            _update_extracted_view(chosen)

    btn_sub = ttk.Frame(file_box)
    btn_sub.pack(fill="x")
    ttk.Button(btn_sub, text="Browse Manual...", command=browse_file).pack(
        side="left"
    )
    ttk.Button(
        btn_sub,
        text="Reload Selected",
        command=lambda: _update_extracted_view(file_var.get()),
    ).pack(side="left", padx=(6, 0))

    if candidates:
        file_var.trace_add("write", lambda *_: _update_extracted_view(file_var.get()))
        _update_extracted_view(candidates[0])

    def confirm() -> None:
        content = text_widget.get("1.0", "end").strip()
        if content:
            classification = classify_spice_model(content)
            if classification["action"] == "INCLUDE_FILE" and file_var.get().strip():
                result["path"] = file_var.get().strip()
            else:
                result["inline"] = content
        elif file_var.get().strip():
            result["path"] = file_var.get().strip()
        dialog.destroy()

    def skip() -> None:
        dialog.destroy()

    button_row = ttk.Frame(frame)
    button_row.pack(fill="x", pady=(4, 0))
    ttk.Button(button_row, text="Confirm Definition", command=confirm).pack(
        side="left"
    )
    ttk.Button(button_row, text="Skip", command=skip).pack(side="right")

    dialog.wait_window()
    return result or None


def review_choices_gui(
    category_label: str,
    items_by_model: Dict[str, list],
    choices: dict,
    redo_one: Callable[[str], Any],
    root,
) -> dict:
    """GUI implementation of process_models' review_choices hook: a modal
    list of every choice made so far for one category (library imports, or
    device models), with a button to redo any selected entry -- so picking
    the wrong file for an earlier model doesn't mean starting over.
    """
    import tkinter as tk
    from tkinter import ttk

    if not items_by_model:
        return choices
    choices = dict(choices)
    models = sorted(items_by_model.keys())

    dialog = tk.Toplevel(root)
    dialog.title(f"Review: {category_label}")
    dialog.geometry("640x360")
    dialog.transient(root)
    dialog.grab_set()

    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(
        frame,
        text=f"Review your {category_label} choices before generating:",
        font=("", 10, "bold"),
    ).pack(anchor="w", pady=(0, 8))

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill="both", expand=True)
    listbox = tk.Listbox(list_frame, height=10)
    listbox.pack(side="left", fill="both", expand=True)
    scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
    scroll.pack(side="right", fill="y")
    listbox.configure(yscrollcommand=scroll.set)

    def _refresh():
        listbox.delete(0, tk.END)
        for model in models:
            listbox.insert(tk.END, f"{model}: {_describe_cli_choice(choices.get(model))}")

    _refresh()

    def _redo_selected():
        sel = listbox.curselection()
        if not sel:
            return
        model = models[sel[0]]
        # redo_one(model) returns None both when the user explicitly
        # skips/cancels the file-picker dialog it opens AND when it's
        # legitimately re-picking "no file". Since this button exists
        # specifically so a wrong pick can be backed out of, None here is
        # treated as "cancelled -- keep whatever was already chosen",
        # not "clear the existing choice". Previously this always
        # overwrote choices[model] unconditionally, so cancelling out of a
        # redo silently downgraded an already-correct choice to nothing.
        new_choice = redo_one(model)
        if new_choice is not None:
            choices[model] = new_choice
        _refresh()

    listbox.bind("<Double-Button-1>", lambda _e: _redo_selected())

    btn_row = ttk.Frame(frame)
    btn_row.pack(fill="x", pady=(8, 0))
    ttk.Button(btn_row, text="Redo selected", command=_redo_selected).pack(side="left")
    ttk.Label(frame, text="(double-click an item to redo it)").pack(anchor="w")
    ttk.Button(btn_row, text="Continue", command=dialog.destroy).pack(side="right")

    dialog.wait_window()
    return choices


def confirm_and_generate_gui(resolved_paths: dict, device_choices: dict, root) -> bool:
    """GUI implementation of process_models' confirm_before_generating
    hook: shows every library and device-model choice together one last
    time, and only writes anything to the schematic once the user
    explicitly confirms."""
    from tkinter import messagebox

    lines = []
    if not resolved_paths and not device_choices:
        lines.append("No library imports or device models to inject.")
    else:
        if resolved_paths:
            lines.append("Library imports:")
            for model, path in sorted(resolved_paths.items()):
                lines.append(f"  {model} -> {path}")
        if device_choices:
            lines.append("Device models:")
            for model, choice in sorted(device_choices.items()):
                lines.append(f"  {model} -> {_describe_cli_choice(choice)}")
    lines.append("")
    lines.append("Generate the final QSpice schematic with these choices?")
    return messagebox.askyesno(
        "Confirm and generate", "\n".join(lines), parent=root
    )


_logger = logging.getLogger("spicelib_vendor.AscToQschFixed")


class _MissingSymbolStub:
    symbol_type = "CELL"

    def is_subcircuit(self):
        return False

    def get_library(self):
        return None


def _patch_asc_editor_symbol_lookup():
    original = AscEditor._get_symbol

    def patched(self, symbol):
        try:
            return original(self, symbol)
        except FileNotFoundError:
            print(
                f"  NOTE: no .asy found for symbol type '{symbol}' during initial "
                f"parse -- will retry with full search path list below, and use a "
                f"placeholder if still unresolved."
            )
            return _MissingSymbolStub()
        except Exception as exc:
            # A .asy file with a matching name was found here, but spicelib
            # couldn't parse it at all (e.g. NotImplementedError on an
            # unsupported drawing primitive) -- this is exactly the "symbol
            # that can't even be opened in LTspice" case: a genuinely
            # malformed/nonstandard file. Only FileNotFoundError was being
            # caught here, so any other parse failure crashed the whole
            # conversion during AscEditor's own constructor, before the
            # main symbol-resolution loop (which has its own matching
            # fallback further down) ever got a chance to run. Treating it
            # the same as "not found" restores the original behavior this
            # tool is meant to have -- still convert with a placeholder,
            # then let the user supply a library for it during model
            # fixup -- for any symbol this can't fully parse, not just ones
            # it can't locate.
            print(
                f"  NOTE: found a .asy for symbol type '{symbol}' but could not "
                f"parse it ({exc}) -- treating as unresolved and using a "
                f"placeholder if still unresolved after the full search below."
            )
            return _MissingSymbolStub()

    AscEditor._get_symbol = patched


def _default_search_paths(asc_file, extra_paths):
    # Step 2 (model/.lib fix-up) already searches the broad root list from
    # get_default_search_roots() -- which recursively covers Documents,
    # Desktop, and Downloads, not just LTspice's own install paths -- but
    # step 1 (.asy symbol resolution, here) used a much narrower list of
    # fixed LTspice-only paths. That mismatch was a real, confirmed bug: a
    # real circuit's AD826A.asy (living in a manufacturer-supplied symbol
    # dropped in ~/Documents, not any LTspice-standard folder) silently
    # failed to resolve in step 1, producing a blank/pinless placeholder
    # box in the converted schematic -- while a hypothetical step-2-only
    # search would have found it. Folding get_default_search_roots() in
    # here keeps both steps consistent and searching the same real-world
    # locations, recursively (os.walk under each root, see
    # find_file_in_directory), instead of maintaining two different guesses
    # about where a user's custom symbols might live.
    return extra_paths + get_default_search_roots(asc_file) + [
        os.path.expanduser("~/AppData/Local/LTspice/lib/sym"),
        os.path.expanduser("~/Documents/LtspiceXVII/lib/sym"),
        os.path.expanduser("~/Library/Application Support/LTspice/lib/sym"),
    ]


def _build_placeholder_symbol(reference, symbol_type, value):
    symbol = QschTag("symbol", "?")
    symbol.items.append(QschTag("type:", "?"))
    symbol.items.append(
        QschTag("description:", f"UNRESOLVED SYMBOL: {symbol_type}")
    )
    symbol.items.append(QschTag("shorted pins:", "false"))

    box, _ = QschTag.parse(
        "«rect (0,0) (300,-300) 0 2 0 0xff0000 0x1000000 -1 0 -1»"
    )
    symbol.items.append(box)

    ref_text, _ = QschTag.parse(
        f'«text (20,-150) 1 7 0 0x1000000 -1 -1 "{reference}"»'
    )
    symbol.items.append(ref_text)

    val_text, _ = QschTag.parse(
        f'«text (20,-100) 1 7 0 0x1000000 -1 -1 "{value}"»'
    )
    symbol.items.append(val_text)

    warn_text, _ = QschTag.parse(
        f'«text (20,-50) 1 7 0 0xff0000 -1 -1 "NO SYMBOL: {symbol_type}"»'
    )
    symbol.items.append(warn_text)

    return symbol


_EXTRA_BUILTIN_SYMBOL_TEMPLATES: Dict[str, List[str]] = {
    "ind": [
        "«type: L»",
        "«description: Inductor»",
        "«shorted pins: false»",
        "«line (0,200) (0,180) 0 0 0x1000000 -1 -1»",
        "«line (0,-200) (0,-180) 0 0 0x1000000 -1 -1»",
        "«coil (-80,180) (80,-180) 0 0 0 0x1000000 -1 -1»",
        '«text (100,150) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (100,-150) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»',
    ],
    "current": [
        "«type: I»",
        "«description: Independent Current Source»",
        "«shorted pins: false»",
        "«line (0,-130) (0,-200) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,130) 0 0 0x1000000 -1 -1»",
        "«ellipse (-130,130) (130,-130) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«line (0,80) (0,-80) 0 0 0x1000000 -1 -1»",
        "«triangle (0,90) (-35,40) (35,40) 0 0 0x1000000 0x2000000 -1 -1»",
        '«text (100,150) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (100,-150) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»',
    ],
    "zener": [
        "«type: D»",
        "«description: Zener Diode»",
        "«library file: Diode.txt»",
        "«shorted pins: false»",
        "«line (100,80) (-100,80) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,80) 0 0 0x1000000 -1 -1»",
        "«line (0,-200) (0,-70) 0 0 0x1000000 -1 -1»",
        "«triangle (0,80) (100,-70) (-100,-70) 0 0 0x1000000 0x2000000 -1 -1»",
        '«text (100,0) 1 110 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (-100,0) 1 109 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "A"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "K"»',
    ],
    "schottky": [
        "«type: D»",
        "«description: Schottky Diode»",
        "«library file: Diode.txt»",
        "«shorted pins: false»",
        "«line (100,80) (-100,80) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,80) 0 0 0x1000000 -1 -1»",
        "«line (0,-200) (0,-70) 0 0 0x1000000 -1 -1»",
        "«triangle (0,80) (100,-70) (-100,-70) 0 0 0x1000000 0x2000000 -1 -1»",
        '«text (100,0) 1 110 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (-100,0) 1 109 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "A"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "K"»',
    ],
    "polcap": [
        "«type: C»",
        "«description: Polarized Capacitor»",
        "«shorted pins: false»",
        "«line (0,200) (0,40) 0 0 0x1000000 -1 -1»",
        "«line (0,-40) (0,-200) 0 0 0x1000000 -1 -1»",
        "«rect (-130,-40) (130,-30) 0 0 0 0x1000000 0x3000000 -1 0 -1»",
        "«rect (-130,30) (130,40) 0 0 0 0x1000000 0x3000000 -1 0 -1»",
        '«text (130,0) 1 110 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (-130,0) 1 109 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»',
    ],
    "bv": [
        "«type: B»",
        "«description: Behavioral Voltage Source»",
        "«shorted pins: false»",
        "«line (0,-130) (0,-200) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,130) 0 0 0x1000000 -1 -1»",
        "«ellipse (-130,130) (130,-130) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«line (-60,90) (60,90) 0 0 0x1000000 -1 -1»",
        "«line (-60,-90) (60,-90) 0 0 0x1000000 -1 -1»",
        "«line (0,-30) (0,30) 0 0 0x1000000 -1 -1»",
        "«line (-30,60) (30,60) 0 0 0x1000000 -1 -1»",
        '«text (100,150) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (100,-150) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»',
    ],
    "bi": [
        "«type: B»",
        "«description: Behavioral Current Source»",
        "«shorted pins: false»",
        "«line (0,-130) (0,-200) 0 0 0x1000000 -1 -1»",
        "«line (0,200) (0,130) 0 0 0x1000000 -1 -1»",
        "«ellipse (-130,130) (130,-130) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«line (0,80) (0,-80) 0 0 0x1000000 -1 -1»",
        "«triangle (0,90) (-35,40) (35,40) 0 0 0x1000000 0x2000000 -1 -1»",
        '«text (100,150) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (100,-150) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,200) (0,0) 1 0 0 0x0 -1 "+"»',
        '«pin (0,-200) (0,0) 1 0 0 0x0 -1 "-"»',
    ],
    "nmos": [
        "«type: MN»",
        "«description: N-Channel MOSFET transistor»",
        "«shorted pins: false»",
        "«line (300,-300) (300,-600) 0 0 0x1000000 -1 -1»",
        "«line (100,-500) (300,-500) 0 0 0x1000000 -1 -1»",
        "«line (250,-300) (300,-300) 0 0 0x1000000 -1 -1»",
        "«line (100,-300) (250,-275) 0 0 0x1000000 -1 -1»",
        "«line (100,-300) (250,-325) 0 0 0x1000000 -1 -1»",
        "«line (250,-275) (250,-325) 0 0 0x1000000 -1 -1»",
        "«line (100,-50) (100,-150) 0 0 0x1000000 -1 -1»",
        "«line (100,-250) (100,-350) 0 0 0x1000000 -1 -1»",
        "«line (100,-450) (100,-550) 0 0 0x1000000 -1 -1»",
        "«line (0,-500) (50,-500) 0 0 0x1000000 -1 -1»",
        "«line (50,-100) (50,-500) 0 0 0x1000000 -1 -1»",
        "«line (300,-100) (100,-100) 0 0 0x1000000 -1 -1»",
        "«line (300,0) (300,-100) 0 0 0x1000000 -1 -1»",
        '«text (350,-200) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (350,-450) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (300,-0) (0,0) 1 0 0 0x1000000 -1 "D"»',
        '«pin (0,-500) (0,0) 1 0 0 0x1000000 -1 "G"»',
        '«pin (300,-600) (0,0) 1 0 0 0x1000000 -1 "S"»',
    ],
    "sw": [
        "«type: S»",
        "«description: Voltage controlled switch»",
        "«shorted pins: false»",
        "«line (-300,-200) (-200,-200) 0 0 0x1000000 -1 -1»",
        "«line (-200,-200) (-150,-225) 0 0 0x1000000 -1 -1»",
        "«line (-300,-500) (-200,-500) 0 0 0x1000000 -1 -1»",
        "«line (-200,-500) (-150,-475) 0 0 0x1000000 -1 -1»",
        "«line (0,-600) (0,-450) 0 0 0x1000000 -1 -1»",
        "«line (0,-100) (0,-225) 0 0 0x1000000 -1 -1»",
        "«line (0,-225) (125,-375) 0 0 0x1000000 -1 -1»",
        "«line (-300,-450) (-250,-450) 0 0 0x1000000 -1 -1»",
        "«line (-275,-475) (-275,-425) 0 0 0x1000000 -1 -1»",
        "«line (-300,-250) (-250,-250) 0 0 0x1000000 -1 -1»",
        "«ellipse (-200,-150) (200,-550) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«ellipse (-25,-475) (25,-425) 0 0 0 0x1000000 0x1000000 -1 -1»",
        "«ellipse (100,-350) (150,-400) 0 0 0 0x1000000 0x1000000 -1 -1»",
        '«text (150,-100) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (150,-600) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,-100) (0,0) 1 0 0 0x1000000 -1 "A"»',
        '«pin (0,-600) (0,0) 1 0 0 0x1000000 -1 "B"»',
        '«pin (-300,-500) (0,0) 1 0 0 0x1000000 -1 "NC+"»',
        '«pin (-300,-200) (0,0) 1 0 0 0x1000000 -1 "NC-"»',
    ],
    "e": [
        "«type: E»",
        "«description: Voltage dependent voltage source»",
        "«shorted pins: false»",
        "«line (-300,-200) (-200,-200) 0 0 0x1000000 -1 -1»",
        "«line (-200,-200) (-150,-225) 0 0 0x1000000 -1 -1»",
        "«line (-300,-500) (-200,-500) 0 0 0x1000000 -1 -1»",
        "«line (-200,-500) (-150,-475) 0 0 0x1000000 -1 -1»",
        "«line (0,-100) (0,-150) 0 0 0x1000000 -1 -1»",
        "«line (0,-600) (0,-550) 0 0 0x1000000 -1 -1»",
        "«line (-300,-450) (-250,-450) 0 0 0x1000000 -1 -1»",
        "«line (-300,-250) (-250,-250) 0 0 0x1000000 -1 -1»",
        "«line (-275,-225) (-275,-275) 0 0 0x1000000 -1 -1»",
        "«line (-25,-450) (25,-450) 0 0 0x1000000 -1 -1»",
        "«line (-25,-250) (25,-250) 0 0 0x1000000 -1 -1»",
        "«line (0,-225) (0,-275) 0 0 0x1000000 -1 -1»",
        "«ellipse (-200,-150) (200,-550) 0 0 0 0x1000000 0x1000000 -1 -1»",
        '«text (150,-100) 1 7 0 0x1000000 -1 -1 "{ref:}"»',
        '«text (150,-600) 1 7 0 0x1000000 -1 -1 "{val:}"»',
        '«pin (0,-100) (0,0) 1 0 0 0x1000000 -1 "+"»',
        '«pin (0,-600) (0,0) 1 0 0 0x1000000 -1 "-"»',
        '«pin (-300,-200) (0,0) 1 0 0 0x1000000 -1 "P"»',
        '«pin (-300,-500) (0,0) 1 0 0 0x1000000 -1 "N"»',
    ],
}


def _parse_builtin_symbol_templates_from_xml(xml_root) -> Dict[str, List[str]]:
    templates: Dict[str, List[str]] = {}
    if xml_root is None:
        return templates
    try:
        for sym in xml_root.findall("component_symbols/symbol"):
            lt_name_el = sym.find("LT_name")
            if lt_name_el is None or not lt_name_el.text:
                continue
            lt_name = lt_name_el.text.strip().lower()
            items = [
                item_el.text.strip()
                for item_el in sym.findall("items/item")
                if item_el.text and item_el.text.strip()
            ]
            if items:
                templates[lt_name] = items
    except Exception:
        pass
    return templates


_BUILTIN_SYMBOL_NAME_OVERRIDES: Dict[str, str] = {
    "ind": "L",
    "res": "R",
    "cap": "C",
    "voltage": "V",
    "current": "I",
    "zener": "D",
    "schottky": "D",
    "polcap": "C",
    "nmos": "MN",
    "pmos": "MP",
    "sw": "S",
    "e": "E",
    "bv": "B",
    "bi": "B",
}


def _build_native_symbol(
    reference: str, value: str, template_items: List[str], symbol_key: str = ""
) -> QschTag:
    symbol_name = _BUILTIN_SYMBOL_NAME_OVERRIDES.get(
        symbol_key, symbol_key.upper() if symbol_key else "?"
    )
    symbol = QschTag("symbol", symbol_name)
    for raw_item in template_items:
        text = raw_item.replace("{ref:}", str(reference)).replace(
            "{val:}", str(value)
        )
        tag, _ = QschTag.parse(text)
        symbol.items.append(tag)
    return symbol


_IND_PIN_A_NATIVE_OFFSET = (16, 96)
_IND_PIN_B_NATIVE_OFFSET = (16, 16)


def _ind_absolute_pin_positions(anchor_x, anchor_y, rotation_value, scale_x, scale_y):
    mirror = rotation_value >= 360
    ang = rotation_value % 360
    rad = math.radians(ang)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def transform(native_dx, native_dy):
        rx = native_dx * cos_a - native_dy * sin_a
        ry = native_dx * sin_a + native_dy * cos_a
        if mirror:
            rx = -rx
        return anchor_x + rx * scale_x, anchor_y + ry * scale_y

    pin_a = transform(*_IND_PIN_A_NATIVE_OFFSET)
    pin_b = transform(*_IND_PIN_B_NATIVE_OFFSET)
    return (round(pin_a[0]), round(pin_a[1])), (round(pin_b[0]), round(pin_b[1]))


def _build_ind_symbol_absolute(reference, value, anchor_x, anchor_y, rotation_value, scale_x, scale_y):
    (ax, ay), (bx, by) = _ind_absolute_pin_positions(
        anchor_x, anchor_y, rotation_value, scale_x, scale_y
    )

    vertical = abs(ax - bx) <= abs(ay - by)

    total_span = math.hypot(ax - bx, ay - by)
    lead = 20.0 if total_span >= 2 * 20.0 + 40.0 else max(0.0, (total_span - 40.0) / 2.0)
    half_width = 80

    items: List[str] = []

    if vertical:
        if ay >= by:
            top, bottom = (ax, ay), (bx, by)
        else:
            top, bottom = (bx, by), (ax, ay)
        cx = top[0]
        coil_top_y = top[1] - lead
        coil_bot_y = bottom[1] + lead
        mid_y = (top[1] + bottom[1]) / 2.0
        items.append(f'«line ({cx},{top[1]}) ({cx},{coil_top_y:.0f}) 0 0 0x1000000 -1 -1»')
        items.append(f'«line ({cx},{bottom[1]}) ({cx},{coil_bot_y:.0f}) 0 0 0x1000000 -1 -1»')
        items.append(
            f'«coil ({cx-half_width},{coil_top_y:.0f}) ({cx+half_width},{coil_bot_y:.0f}) 0 0 0 0x1000000 -1 -1»'
        )
        ref_pos = (cx + 100, round(mid_y + total_span * 0.25))
        val_pos = (cx + 100, round(mid_y - total_span * 0.25))
        pin_top, pin_bottom = top, bottom
    else:
        if ax >= bx:
            right, left = (ax, ay), (bx, by)
        else:
            right, left = (bx, by), (ax, ay)
        cy = right[1]
        coil_right_x = right[0] - lead
        coil_left_x = left[0] + lead
        mid_x = (right[0] + left[0]) / 2.0
        items.append(f'«line ({right[0]},{cy}) ({coil_right_x:.0f},{cy}) 0 0 0x1000000 -1 -1»')
        items.append(f'«line ({left[0]},{cy}) ({coil_left_x:.0f},{cy}) 0 0 0x1000000 -1 -1»')
        items.append(
            f'«coil ({coil_left_x:.0f},{cy+half_width}) ({coil_right_x:.0f},{cy-half_width}) 0 0 0 0x1000000 -1 -1»'
        )
        ref_pos = (round(mid_x), cy + 100)
        val_pos = (round(mid_x), cy - 100)
        pin_top, pin_bottom = right, left

    items.append(f'«text ({ref_pos[0]},{ref_pos[1]}) 1 7 0 0x1000000 -1 -1 "{reference}"»')
    items.append(f'«text ({val_pos[0]},{val_pos[1]}) 1 7 0 0x1000000 -1 -1 "{value}"»')
    items.append(f'«pin ({pin_top[0]},{pin_top[1]}) (0,0) 1 0 0 0x0 -1 "+"»')
    items.append(f'«pin ({pin_bottom[0]},{pin_bottom[1]}) (0,0) 1 0 0 0x0 -1 "-"»')

    symbol = QschTag("symbol", "L")
    type_tag, _ = QschTag.parse("«type: L»")
    desc_tag, _ = QschTag.parse("«description: Inductor»")
    shorted_tag, _ = QschTag.parse("«shorted pins: false»")
    symbol.items.append(type_tag)
    symbol.items.append(desc_tag)
    symbol.items.append(shorted_tag)
    for raw_item in items:
        tag, _ = QschTag.parse(raw_item)
        symbol.items.append(tag)
    return symbol


_RSER_TOKEN_RE = re.compile(r"\bRser\s*=\s*\S+", re.IGNORECASE)


def _extract_rser_token(spiceline_text: str) -> Optional[str]:
    """Pulls just the "Rser=<value>" token out of a raw SpiceLine string,
    ignoring everything else in it (Lser, Rpar, Cpar, tolerance, ...).

    Scoped to Rser only, deliberately: confirmed via QSpice's own author
    (Mike Engelhardt, on the Qorvo support forum) that QSpice's capacitor
    model does not implement Lser at all -- appending it the way Rser is
    appended here would either be silently ignored or produce an "unknown
    instance parameter" warning, so it's better left out entirely than
    carried over as text that won't do anything. Rser is confirmed
    supported on both the capacitor and inductor models (the inductor
    model defaults to Rser=1m on its own), so that one parameter is safe
    to carry through generically for every component type.
    """
    if not spiceline_text:
        return None
    m = _RSER_TOKEN_RE.search(spiceline_text)
    return m.group(0) if m else None


def _resolve_component_display_value(comp) -> str:
    """Picks the text written onto a converted component, and -- critically
    -- appends a real "Rser=<value>" onto it when the source specified one.

    LTspice stores a passive part's parasitic/extra SPICE parameters (ESR,
    ESL, parallel R/C, tolerance, anything beyond the bare nominal value)
    in a SEPARATE "SpiceLine" attribute, not in "Value" itself -- e.g. a
    1000uF cap's real ESR lives in SYMATTR SpiceLine "Rser=30m Lser=50n",
    alongside SYMATTR Value "1000u". Earlier versions of this function only
    read Value/Value2/SpiceModel, so that Rser was silently dropped for
    every component type on conversion -- confirmed on a real circuit
    (source SpiceLine "Rser=0.8m" on inductor L21 never made it into the
    converted .qsch at all). Appending the Rser piece here, after the
    existing Value/Value2/SpiceModel precedence (unchanged), fixes that
    generally for every component type, not just inductors. Only Rser is
    pulled out -- see _extract_rser_token for why the rest of SpiceLine is
    deliberately left out.
    """
    raw_spicemodel = comp.attributes.get("SpiceModel")
    raw_value = comp.attributes.get("Value")
    raw_value2 = comp.attributes.get("Value2")
    fallback_name = bare_model_name(comp.symbol or "")

    if raw_spicemodel and bare_model_name(str(raw_spicemodel)):
        base = str(raw_spicemodel)
    elif raw_value2 and (
        not raw_value
        or str(raw_value).lower() in GENERIC_OPAMP_SYMBOLS
        or (fallback_name and str(raw_value).lower() == fallback_name.lower())
    ):
        base = str(raw_value2)
    elif raw_value and str(raw_value).lower() not in GENERIC_OPAMP_SYMBOLS:
        base = str(raw_value)
    elif fallback_name:
        base = fallback_name
    else:
        base = "<val>"

    for key in ("SpiceLine", "SpiceLine2"):
        extra = comp.attributes.get(key)
        rser_token = _extract_rser_token(str(extra)) if extra else None
        if rser_token:
            return f"{base} {rser_token}".strip()
    return base


def _restore_wire_net_names(qsch_editor, asc_editor, log: Callable[[str], None] = print) -> int:
    """Gives every converted wire its correct net name, derived from what
    the wire is actually connected to.

    Fixes a real, confirmed bug in spicelib's QschEditor.copy_from(): it
    builds every wire from a fixed template string containing "0" as the
    placeholder net name, sets the two endpoint positions from the source
    wire, and then -- on the very next line -- has the call that would set
    the real net name COMMENTED OUT in the library source:

        wire_tag, _ = QschTag.parse('«wire (0,0) (0,0) "0"»')
        wire_tag.set_attr(QSCH_WIRE_POS1, (wire.V1.X, wire.V1.Y))
        wire_tag.set_attr(QSCH_WIRE_POS2, (wire.V2.X, wire.V2.Y))
        # wire_tag.set_attr(QSCH_WIRE_NET, wire.net)      <-- disabled

    The result is that EVERY wire in a converted schematic is stored as
    being on net "0" -- ground. Measured on a real 265-component circuit:
    all 1375 wires came out named "0", exactly one distinct name across the
    entire file, where a genuine QSpice-authored schematic carries the real
    net name on each wire (verified against a QSpice-written reference
    file: «wire (400,600) (800,600) "VIN"»). That is the true source of the
    large pile of "conflicting net labels: 0, <real name>" errors this
    conversion has always produced (~146 on that circuit).

    Re-enabling spicelib's own commented-out line would not help: LTspice
    .asc files don't store a net name on a wire at all (net names come from
    separate FLAG statements at coordinates), so every source wire's `net`
    attribute is empty -- confirmed directly on a real .asc, all 1375 wires
    blank. The name has to be DERIVED from connectivity instead: union-find
    over wire endpoints (the same _build_qsch_wire_graph the rest of this
    tool already relies on), then every wire in a connected group takes the
    name of whatever net label is reachable within that group.

    Wires in a group with no reachable label keep whatever they had; those
    are genuinely unlabeled nodes in the original circuit, and QSpice
    auto-numbers them the same way it would for an unlabeled wire drawn by
    hand.
    """
    try:
        find, group_names, _wired_points = _build_qsch_wire_graph(qsch_editor)
    except Exception:
        return 0

    fixed = 0
    unlabeled = 0
    for wire in qsch_editor.schematic.get_items("wire"):
        try:
            p1 = tuple(wire.get_attr(1))
        except Exception:
            continue
        name = group_names.get(find(p1))
        if not name:
            unlabeled += 1
            continue
        try:
            if wire.get_attr(3) != name:
                wire.set_attr(3, name)
                fixed += 1
        except Exception:
            pass
    if fixed or unlabeled:
        log(
            f"  Restored real net names onto {fixed} wire(s) "
            f"({unlabeled} left unlabeled -- no net label reachable, QSpice auto-numbers those). "
            f"spicelib's copy_from() leaves every wire named \"0\" (ground), which shorts the schematic."
        )
    return fixed


def _merge_aliased_nets(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Merges nets that carry more than one label into a single canonical
    name throughout the schematic, matching what LTspice's own netlister
    does and QSpice does not do automatically.

    It's completely legal in LTspice to give one electrical node two (or
    more) different FLAG names -- e.g. wiring a "ShuntA_1" flag directly to
    a "L_Out_A_2" flag with no component between them. LTspice's own
    netlister silently collapses that into one node under a single chosen
    name and rewrites every reference to the other name (including inside
    "V(othername)" behavioral-source expressions) to match -- confirmed
    directly against a real circuit's own LTspice-generated .net file: a
    node flagged both "ShuntA_1" and "L_Out_A_2" comes out entirely as
    "L_Out_A_2", with zero occurrences of "ShuntA_1" anywhere in the file,
    including inside other components' own V(...) expressions that
    originally referenced it by that name.

    QSpice does not do this collapsing -- each flag stays its own separate
    net tag, so only ONE of the two physically-identical nodes actually
    ends up with real components attached in QSpice's eyes; the other
    reads as a genuine floating node with nothing on it. Confirmed as the
    exact cause of a real QSpice run's "floating nodes" / "Singular
    matrix" errors on a real circuit, naming precisely the non-canonical
    half of each aliased pair.

    The canonical name chosen per group matches LTspice's own observed
    choice on a real circuit: "0" (ground) if present in the group,
    otherwise the alphabetically-first name (case-insensitive) -- checked
    against 3 real aliased groups in that circuit's own LTspice-generated
    netlist, all 3 matching this rule.
    """
    try:
        find, _group_names, _wired_points = _build_qsch_wire_graph(qsch_editor)
    except Exception:
        return 0

    names_by_root: Dict[Tuple[int, int], Set[str]] = defaultdict(set)
    for net in qsch_editor.schematic.get_items("net"):
        try:
            pos = tuple(net.get_attr(1))
            name = net.get_attr(5)
        except Exception:
            continue
        if not name:
            continue
        names_by_root[find(pos)].add(name)

    alias_map: Dict[str, str] = {}
    for names in names_by_root.values():
        if len(names) < 2:
            continue
        canonical = "0" if "0" in names else sorted(names, key=str.lower)[0]
        for name in names:
            if name != canonical:
                alias_map[name] = canonical

    if not alias_map:
        return 0

    for old, new in alias_map.items():
        log(
            f"  Merging aliased net '{old}' into '{new}' (this circuit's own "
            f"LTspice design wires these to the same node under two different "
            f"flag names; QSpice needs one name to avoid a floating node)."
        )

    def _walk(tag):
        yield tag
        for item in getattr(tag, "items", None) or []:
            yield from _walk(item)

    v_ref_patterns = {
        old: re.compile(r'\bV\(' + re.escape(old) + r'\)') for old in alias_map
    }

    net_fixed = wire_fixed = text_fixed = 0
    for tag in _walk(qsch_editor.schematic):
        tokens = getattr(tag, "tokens", None)
        if not tokens:
            continue
        kind = tokens[0]
        if kind == "net":
            try:
                name = tag.get_attr(5)
            except Exception:
                continue
            if name in alias_map:
                tag.set_attr(5, alias_map[name])
                net_fixed += 1
        elif kind == "wire":
            try:
                name = tag.get_attr(3)
            except Exception:
                continue
            if name in alias_map:
                tag.set_attr(3, alias_map[name])
                wire_fixed += 1
        elif kind == "text":
            try:
                raw = tag.get_attr(len(tokens) - 1)
            except Exception:
                continue
            if not isinstance(raw, str):
                continue
            new_raw = raw
            for old, pattern in v_ref_patterns.items():
                new_raw = pattern.sub(f"V({alias_map[old]})", new_raw)
            if new_raw != raw:
                tag.set_attr(len(tokens) - 1, new_raw)
                text_fixed += 1

    qsch_editor.canvas_updated = True
    log(
        f"  Rewrote {net_fixed} net label(s), {wire_fixed} wire name(s), and "
        f"{text_fixed} expression(s) to use the merged canonical net names."
    )
    return len(alias_map)


def _fix_text_comment_flags(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Restores the correct "is this a comment" flag on every converted
    text box, fixing a real bug in spicelib's QschEditor.copy_from(): it
    writes EVERY source text box -- both real "!"-prefixed SPICE
    directives (.param, .tran, K-coupling, ...) and plain ";"-prefixed or
    unmarked comment/design-note text -- into the output with the same
    hardcoded "not a comment" flag, discarding the real distinction
    LTspice's own .asc format makes between the two.

    Confirmed causing a real failure: a design-note text box (e.g. "a)
    Resonant Frequency:") from a real converted circuit was parsed
    downstream as if it were an actual device/directive line, producing
    "Fatal error: Unknown device type: 'A'" -- QSpice reading "A)" as an
    attempt to instantiate a digital-gate-type device from a line that
    was never meant to be simulated at all.

    copy_from() deep-copies the source's Text objects (with the correct
    .type) into qsch_editor.directives BEFORE the separate, lossy step
    that builds the actual QSCH text tags from them -- so the correct
    classification still exists in memory at the point this runs; this
    matches each written tag back to its original Text object by exact
    content and corrects the flag from there. Must run right after
    copy_from(), before anything else adds new text tags.
    """
    by_text: Dict[str, List[bool]] = defaultdict(list)
    for directive in getattr(qsch_editor, "directives", []):
        is_comment = getattr(directive, "type", None) == TextTypeEnum.COMMENT
        by_text[getattr(directive, "text", "") or ""].append(is_comment)

    fixed = 0
    for text_tag in qsch_editor.schematic.get_items("text"):
        try:
            raw = text_tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(raw, str) or not raw.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            continue
        content = raw[len(QSCH_TEXT_INSTR_QUALIFIER):]
        queue = by_text.get(content)
        if not queue:
            continue
        is_comment = queue.pop(0)
        try:
            current = text_tag.get_attr(QSCH_TEXT_COMMENT)
        except Exception:
            continue
        desired = 1 if is_comment else 0
        if current != desired:
            try:
                text_tag.set_attr(QSCH_TEXT_COMMENT, desired)
                fixed += 1
            except Exception:
                pass

    if fixed:
        qsch_editor.canvas_updated = True
        log(
            f"  Corrected comment/directive classification on {fixed} text "
            "box(es) (spicelib's copy_from() marks every text box as an "
            "active directive regardless of source, which can make plain "
            "design-note text get parsed downstream as if it were a real "
            "device line)."
        )
    return fixed


def convert_asc_to_qsch(asc_file, qsch_file, search_paths=None, log=None):
    if search_paths is None:
        search_paths = []

    def emit(msg=""):
        if log is None:
            print(msg)
        else:
            log(msg)

    all_search_paths = _default_search_paths(asc_file, search_paths)

    existing_paths = [p for p in all_search_paths if p and os.path.isdir(p)]
    if existing_paths:
        AscEditor.set_custom_library_paths(*existing_paths)

    _patch_asc_editor_symbol_lookup()

    asc_raw_lines = _read_text_file_robust(asc_file) or []
    coupled_inductors = find_coupled_inductors("".join(asc_raw_lines))

    asc_editor = AscEditor(asc_file)

    try:
        asc_to_qsch_mod = __import__(
            "spicelib_vendor.scripts.asc_to_qsch", fromlist=["dummy"]
        )
        parent_dir = os.path.dirname(os.path.realpath(asc_to_qsch_mod.__file__))
        xml_file = os.path.join(parent_dir, "asc_to_qsch_data.xml")
        conversion_data = ET.parse(xml_file)
        root = conversion_data.getroot()

        offset = root.find("offset")
        offset_x = float(offset.get("x"))
        offset_y = float(offset.get("y"))
        scale = root.find("scaling")
        scale_x = float(scale.get("x"))
        scale_y = float(scale.get("y"))
        builtin_symbol_templates = _parse_builtin_symbol_templates_from_xml(root)
    except Exception:
        offset_x, offset_y = 0.0, 0.0
        scale_x, scale_y = 1.0, 1.0
        builtin_symbol_templates = {}

    for _name, _items in _EXTRA_BUILTIN_SYMBOL_TEMPLATES.items():
        builtin_symbol_templates.setdefault(_name, _items)

    asc_editor.scale(
        offset_x=offset_x, offset_y=offset_y, scale_x=scale_x, scale_y=scale_y
    )

    asy_reader_cache = {}
    unusable_asy_files: Set[str] = set()

    total = 0
    converted = 0
    placeholders = 0
    resolution_log = []

    for comp in asc_editor.components.values():
        total += 1
        symbol_lower = (comp.symbol or "").strip().lower()
        ref_upper = (comp.reference or "").strip().upper()

        is_inductor = symbol_lower in ("ind", "ind2") or ref_upper.startswith("L")

        if is_inductor:
            raw_rotation_value = int(comp.rotation)
            value = _resolve_component_display_value(comp)

            attr_combined = " ".join(str(v) for v in comp.attributes.values() if v) + " " + value
            has_rser = "RSER" in attr_combined.upper()

            if ref_upper not in coupled_inductors and not has_rser:
                value = f"{value} Rser=1m".strip()
                damped_note = " (auto-damped Rser=1m)"
            else:
                damped_note = ""

            symbol_tag = _build_ind_symbol_absolute(
                comp.reference,
                value,
                comp.position.X,
                comp.position.Y,
                raw_rotation_value,
                scale_x,
                scale_y,
            )
            comp.attributes["symbol"] = symbol_tag
            comp.position = Point(0, 0)
            comp.rotation = 0
            converted += 1
            resolution_log.append(
                (comp.reference, comp.symbol, f"built-in QSCH primitive (absolute, rotation-safe){damped_note}", value)
            )
            continue

        asy_reader = asy_reader_cache.get(comp.symbol, None)
        resolved_from = "cache" if asy_reader is not None else None

        if asy_reader is None and comp.symbol not in unusable_asy_files:
            for sym_root in all_search_paths:
                if not sym_root or not os.path.exists(sym_root):
                    continue
                symbol_asy_file = find_file_in_directory(
                    sym_root, comp.symbol + ".asy"
                )
                if symbol_asy_file is not None:
                    try:
                        asy_reader = AsyReader(symbol_asy_file)
                        asy_reader_cache[comp.symbol] = asy_reader
                        resolved_from = symbol_asy_file
                    except Exception as exc:
                        # A file with the right name was found, but spicelib
                        # couldn't parse or convert it -- most often a
                        # genuinely malformed/nonstandard .asy that LTspice
                        # itself can't open either (unsupported primitive,
                        # bad encoding, ...). This used to crash the whole
                        # conversion outright, since nothing downstream of
                        # this loop caught it. Falling through to the same
                        # placeholder path used for "no .asy found at all"
                        # instead: the component still gets a real reference
                        # designator, value, and reference box in the
                        # converted schematic, and (like any other
                        # unresolved model) still gets offered to the user
                        # for a manual .lib pick in the model-fixup step --
                        # this is the "still converts, then ask for a
                        # library" behavior this tool is meant to have for
                        # any symbol it can't fully resolve on its own.
                        emit(
                            f"  NOTE: found '{symbol_asy_file}' for symbol "
                            f"'{comp.symbol}' but could not parse/convert it "
                            f"({exc}) -- using a placeholder instead."
                        )
                        unusable_asy_files.add(comp.symbol)
                        asy_reader = None
                    break

        if comp.rotation == 90:
            comp.rotation = 270
        elif comp.rotation == 270:
            comp.rotation = 90
        elif comp.rotation == 90 + 360:
            comp.rotation = 270 + 360
        elif comp.rotation == 270 + 360:
            comp.rotation = 90 + 360

        value = _resolve_component_display_value(comp)

        if asy_reader is not None:
            try:
                symbol_tag = asy_reader.to_qsch(comp.reference, value)
            except Exception as exc:
                # Same fallback as a parse failure above -- a symbol that
                # parsed fine but fails during geometry conversion (e.g. an
                # unsupported drawing primitive) still shouldn't take the
                # whole conversion down with it.
                emit(
                    f"  NOTE: found a symbol for '{comp.symbol}' but could "
                    f"not convert it ({exc}) -- using a placeholder instead."
                )
                unusable_asy_files.add(comp.symbol)
                asy_reader_cache.pop(comp.symbol, None)
                asy_reader = None
            else:
                comp.attributes["symbol"] = symbol_tag
                converted += 1
        if asy_reader is None:
            symbol_key = (comp.symbol or "").strip().lower()
            builtin_items = builtin_symbol_templates.get(symbol_key)
            if builtin_items:
                symbol_tag = _build_native_symbol(
                    comp.reference, value, builtin_items, symbol_key
                )
                comp.attributes["symbol"] = symbol_tag
                resolved_from = "built-in QSCH primitive (no .asy needed)"
                converted += 1
            elif is_qspice_local_library_component(symbol_key):
                symbol_tag = _build_placeholder_symbol(
                    comp.reference, comp.symbol, value
                )
                comp.attributes["symbol"] = symbol_tag
                resolved_from = "QSpice local library component"
                converted += 1
            else:
                symbol_tag = _build_placeholder_symbol(
                    comp.reference, comp.symbol, value
                )
                comp.attributes["symbol"] = symbol_tag
                placeholders += 1

        resolution_log.append(
            (comp.reference, comp.symbol, resolved_from, value)
        )

    qsch_editor = QschEditor(qsch_file, create_blank=True)
    qsch_editor.copy_from(asc_editor)
    _fix_text_comment_flags(qsch_editor, log=emit)
    _restore_wire_net_names(qsch_editor, asc_editor, log=emit)
    _merge_aliased_nets(qsch_editor, log=emit)

    force_save_qsch(qsch_editor, qsch_file)

    if not os.path.exists(qsch_file):
        raise FileNotFoundError(f"Failed to create output file: {qsch_file}")

    emit("")
    emit("--- Per-component resolution log ---")
    for ref, symtype, resolved_from, value in resolution_log:
        status = resolved_from if resolved_from else "*** NOT FOUND ***"
        emit(f"  {ref:8s} type={symtype:20s} value={value:15s} -> {status}")

    emit("")
    emit(
        f"Summary: {converted}/{total} components resolved with a real symbol, "
        f"{placeholders} placeholder(s)."
    )


# ----------------------- GUI -----------------------


def _normalize_paths(paths):
    cleaned = []
    seen = set()
    for p in paths:
        if not p:
            continue
        p = os.path.normpath(str(p).replace("\xa0", " ").strip().strip('"').strip("'"))
        if p and p not in seen:
            seen.add(p)
            cleaned.append(p)
    return cleaned


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("QSpice Combined Tool: Convert + Fix")
    root.geometry("900x700")

    def _do_quit():
        # root.destroy() alone doesn't reliably end the process -- confirmed
        # directly this session: closing the window left the compiled .exe
        # still running as a background process (seen via tasklist showing
        # two live newtool.exe instances at once), most likely a mainloop
        # not fully unwinding under PyInstaller's windowed (console=False)
        # bootloader. os._exit() guarantees the process actually ends.
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", _do_quit)

    asc_var = tk.StringVar()
    qsch_var = tk.StringVar()
    use_default_output_var = tk.BooleanVar(value=True)
    auto_search_var = tk.BooleanVar(value=True)
    fix_annotations_var = tk.BooleanVar(value=True)
    replace_zero_ohm_var = tk.BooleanVar(value=True)
    device_model_fix_var = tk.BooleanVar(value=True)
    uic_workaround_var = tk.BooleanVar(value=False)

    def _on_qsch_manual_edit(*_args):
        if asc_var.get():
            base, _ = os.path.splitext(asc_var.get())
            default_qsch = os.path.normpath(base + ".qsch")
            current_qsch = os.path.normpath(qsch_var.get()) if qsch_var.get() else ""
            if current_qsch != default_qsch:
                use_default_output_var.set(False)

    qsch_var.trace_add("write", _on_qsch_manual_edit)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)
    main.rowconfigure(3, weight=1)

    files = ttk.LabelFrame(main, text="Files", padding=10)
    files.grid(row=0, column=0, sticky="ew")
    files.columnconfigure(0, weight=1)

    def browse_asc():
        path = filedialog.askopenfilename(
            parent=root,
            title="Select LTspice .asc file",
            filetypes=[("LTspice schematic", "*.asc"), ("All files", "*.*")],
        )
        if path:
            norm_asc = os.path.normpath(path)
            asc_var.set(norm_asc)
            # Picking a NEW .asc file always re-syncs the output name to
            # match it, even if the output field had been manually edited
            # earlier -- that manual edit was for whatever circuit was
            # previously selected, and shouldn't silently keep overriding
            # the output name for every DIFFERENT circuit picked after it.
            # (Editing the output field again, for THIS circuit, still
            # disables the auto-sync as before, via _on_qsch_manual_edit.)
            use_default_output_var.set(True)
            base, _ = os.path.splitext(norm_asc)
            qsch_var.set(os.path.normpath(base + ".qsch"))

    def browse_qsch():
        path = filedialog.asksaveasfilename(
            parent=root,
            title="Select output .qsch file",
            defaultextension=".qsch",
            filetypes=[("QSpice schematic", "*.qsch"), ("All files", "*.*")],
        )
        if path:
            qsch_var.set(os.path.normpath(path))
            use_default_output_var.set(False)

    ttk.Label(files, text="Input .asc").grid(row=0, column=0, sticky="w")
    ttk.Entry(files, textvariable=asc_var).grid(
        row=1, column=0, sticky="ew", padx=(0, 8)
    )
    ttk.Button(files, text="Browse...", command=browse_asc).grid(
        row=1, column=1, sticky="ew"
    )

    ttk.Label(files, text="Output .qsch (final, fixed result)").grid(
        row=2, column=0, sticky="w", pady=(10, 0)
    )
    ttk.Entry(files, textvariable=qsch_var).grid(
        row=3, column=0, sticky="ew", padx=(0, 8)
    )
    ttk.Button(files, text="Browse...", command=browse_qsch).grid(
        row=3, column=1, sticky="ew"
    )

    ttk.Checkbutton(
        files,
        text="Use same base name for output when input is chosen",
        variable=use_default_output_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    fix_frame = ttk.LabelFrame(
        main, text="Model fix-up options (for step 2)", padding=10
    )
    fix_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    ttk.Checkbutton(
        fix_frame,
        text="Auto-search system folders (Downloads/Desktop/Documents/LTspice)",
        variable=auto_search_var,
    ).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame,
        text="Fix plain text annotations / hidden notes as comments (recommended)",
        variable=fix_annotations_var,
    ).grid(row=1, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame,
        text="Replace 0-ohm resistors with wires (recommended)",
        variable=replace_zero_ohm_var,
    ).grid(row=2, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame,
        text="Detect & prompt for missing component model/subcircuit definitions (recommended)",
        variable=device_model_fix_var,
    ).grid(row=3, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame,
        text="Replace '.tran ... startup' (QSpice ignores it) with 'uic'",
        variable=uic_workaround_var,
    ).grid(row=4, column=0, sticky="w")

    log_frame = ttk.LabelFrame(main, text="Log", padding=10)
    log_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, wrap="word", height=18)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(
        log_frame, orient="vertical", command=log_text.yview
    )
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    log_lines: List[str] = []

    def log(msg=""):
        text = str(msg)
        log_text.insert(tk.END, text + "\n")
        log_text.see(tk.END)
        root.update_idletasks()
        log_lines.append(text)

    def write_log_file(qsch_file: str):
        # Same location and name as the converted .qsch, just a .log
        # extension -- matches the convention LTspice/QSpice's own .log
        # files already use next to a schematic. Written unconditionally
        # at the end of a run (success, cancelled, or errored) via the
        # caller's try/finally, so a failed run's log is captured too.
        if not log_lines:
            return
        log_path = os.path.splitext(qsch_file)[0] + ".log"
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(log_lines) + "\n")
        except Exception as exc:
            log(f"  NOTE: could not write log file {log_path}: {exc}")

    def run_all():
        asc_file = os.path.normpath(asc_var.get().strip())
        qsch_file = os.path.normpath(qsch_var.get().strip())

        if not asc_file or not os.path.isfile(asc_file):
            messagebox.showerror(
                "Missing file", "Please select a valid .asc file.", parent=root
            )
            return
        if not qsch_file:
            messagebox.showerror(
                "Missing file", "Please select an output .qsch file.", parent=root
            )
            return

        if os.path.exists(qsch_file):
            confirm = messagebox.askyesno(
                "File Already Exists",
                f"The output file already exists:\n\n{qsch_file}\n\nDo you want to overwrite it?",
                parent=root,
            )
            if not confirm:
                return

        log_text.delete("1.0", tk.END)
        log_lines.clear()
        log("=" * 60)
        log("Converting .asc -> .qsch")
        log("=" * 60)

        try:
            convert_asc_to_qsch(
                asc_file,
                qsch_file,
                log=log,
            )
        except Exception as exc:
            log(f"ERROR during conversion: {exc}")
            messagebox.showerror("Conversion failed", str(exc), parent=root)
            write_log_file(qsch_file)
            return

        log("")
        log("=" * 60)
        log("PHASE 1: Library and model selection")
        log("=" * 60)

        search_roots = (
            get_default_search_roots(asc_file) if auto_search_var.get() else []
        )

        def chooser(model, candidates):
            return choose_model_path_gui(model, candidates, root)

        def device_chooser(model, candidates, refs):
            return choose_device_model_gui(model, candidates, refs, root)

        def review_wrapper(category_label, items_by_model, choices, redo_one):
            return review_choices_gui(category_label, items_by_model, choices, redo_one, root)

        def confirm_wrapper(resolved_paths, device_choices):
            return confirm_and_generate_gui(resolved_paths, device_choices, root)

        try:
            fixup_result = process_models(
                asc_file=asc_file,
                qsch_file=qsch_file,
                search_roots=search_roots,
                choose_model_path=chooser,
                log=log,
                fix_annotations=fix_annotations_var.get(),
                replace_zero_ohm=replace_zero_ohm_var.get(),
                apply_uic_workaround=uic_workaround_var.get(),
                choose_device_model=(
                    device_chooser if device_model_fix_var.get() else None
                ),
                review_choices=review_wrapper,
                confirm_before_generating=confirm_wrapper,
            )
        except Exception as exc:
            log(f"ERROR during model fix-up: {exc}")
            messagebox.showerror("Model fix-up failed", str(exc), parent=root)
            write_log_file(qsch_file)
            return

        log("")
        log("=" * 60)
        if fixup_result.cancelled:
            log("CANCELLED -- no library/model choices were applied.")
            log("=" * 60)
            messagebox.showinfo(
                "Cancelled",
                "Generation cancelled -- no library or device model changes "
                f"were saved.\n\n{qsch_file}\nstill only has the initial "
                "conversion from before your choices were reviewed.",
                parent=root,
            )
        else:
            log("ALL DONE.")
            log("=" * 60)
            messagebox.showinfo(
                "Done", f"Finished!\nSaved to:\n{qsch_file}", parent=root
            )
            if messagebox.askyesno(
                "View file?",
                f"Open the converted file now?\n\n{qsch_file}",
                parent=root,
            ):
                try:
                    os.startfile(qsch_file)
                except Exception as exc:
                    log(f"  NOTE: could not open {qsch_file} automatically: {exc}")
                    messagebox.showerror(
                        "Could not open file", str(exc), parent=root
                    )

        write_log_file(qsch_file)

    action_row = ttk.Frame(main)
    action_row.grid(row=2, column=0, sticky="w", pady=(10, 0))
    ttk.Button(action_row, text="Convert and generate", command=run_all).pack(
        side="left"
    )
    ttk.Button(action_row, text="Quit", command=_do_quit).pack(
        side="left", padx=(8, 0)
    )

    root.mainloop()


class _TeeStdout:
    """Duplicates every write to the real stdout into an in-memory buffer,
    so run_cli_combined can export a full transcript (everything printed --
    phase headers, per-component resolution log, prompts, chosen answers)
    to a .log file alongside the converted .qsch, matching what the GUI's
    own log_text/write_log_file already capture. Wrapping sys.stdout
    itself (rather than threading a custom logger through every print()
    and input() call site in this file) keeps this additive: no existing
    call site needed to change."""

    def __init__(self, original):
        self._original = original
        self.text = ""

    def write(self, s):
        self._original.write(s)
        self.text += s

    def flush(self):
        self._original.flush()

    def isatty(self):
        return getattr(self._original, "isatty", lambda: False)()


def run_cli_combined(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert an LTspice .asc into a fixed, model-complete QSpice .qsch in one pass."
    )
    parser.add_argument("asc_file")
    parser.add_argument("qsch_file", nargs="?", default=None)
    parser.add_argument(
        "-a",
        "--add",
        action="append",
        dest="search_path",
        default=[],
        help="Add a path for searching .asy symbol files.",
    )
    parser.add_argument(
        "--auto-search",
        action="append",
        default=[],
        help="Extra folder(s) to search for missing .model/.subckt files.",
    )
    parser.add_argument("--no-annotation-fix", action="store_true")
    parser.add_argument("--no-zero-ohm-replacement", action="store_true")
    parser.add_argument("--no-device-model-fix", action="store_true")
    parser.add_argument(
        "--apply-uic-workaround",
        action="store_true",
        help="Replace '.tran ... startup' (silently ignored by QSpice) with 'uic'. Off by "
        "default -- circuit-dependent whether it's actually an improvement over doing "
        "nothing (see HANDOFF.md section 6.6).",
    )
    args = parser.parse_args(argv)

    asc_file = os.path.normpath(args.asc_file)
    qsch_file = os.path.normpath(
        args.qsch_file or (os.path.splitext(asc_file)[0] + ".qsch")
    )

    tee = _TeeStdout(sys.stdout)
    sys.stdout = tee
    try:
        return _run_cli_combined_body(asc_file, qsch_file, args)
    finally:
        sys.stdout = tee._original
        # Same location and name as the converted .qsch, just a .log
        # extension -- matches the convention LTspice/QSpice's own .log
        # files already use next to a schematic. Written unconditionally
        # here (success, cancelled, or errored -- an uncaught exception
        # still runs this finally block before propagating) so a failed
        # run's transcript is captured too.
        if tee.text:
            log_path = os.path.splitext(qsch_file)[0] + ".log"
            try:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(tee.text)
            except Exception as exc:
                print(f"  NOTE: could not write log file {log_path}: {exc}")


def _run_cli_combined_body(asc_file: str, qsch_file: str, args) -> int:
    print(f"Converting {asc_file} -> {qsch_file}")
    convert_asc_to_qsch(
        asc_file,
        qsch_file,
        search_paths=_normalize_paths(args.search_path),
    )

    print()
    print("PHASE 1: LIBRARY AND MODEL SELECTION")
    print(f"Resolving missing libraries/models for {qsch_file}")
    search_roots = get_default_search_roots(asc_file) + args.auto_search

    device_chooser = (
        None if args.no_device_model_fix else choose_device_model_cli
    )

    result = process_models(
        asc_file=asc_file,
        qsch_file=qsch_file,
        search_roots=search_roots,
        choose_model_path=choose_model_path_cli,
        log=print,
        fix_annotations=not args.no_annotation_fix,
        replace_zero_ohm=not args.no_zero_ohm_replacement,
        apply_uic_workaround=args.apply_uic_workaround,
        choose_device_model=device_chooser,
        review_choices=review_choices_cli,
        confirm_before_generating=confirm_and_generate_cli,
    )

    if not result.cancelled:
        # This exe is built windowed (console=False, see newtool.spec) so
        # the GUI never shows a black terminal window -- but that also
        # means a CLI-mode run launched without an attached console (e.g.
        # from a shortcut) has no real stdin at all. input() then fails
        # immediately instead of waiting for a keypress, which previously
        # crashed the whole run right at the finish line. Exactly which
        # exception that failure surfaces as isn't consistent across every
        # "no real console" situation (confirmed EOFError in the one real
        # crash report; a broader catch here covers e.g. AttributeError if
        # sys.stdin itself ends up None rather than a closed/empty stream).
        # Guarded broadly so "no console available," in any form, just
        # silently skips the prompt instead of crashing -- the file is
        # already saved either way, this step is purely a convenience.
        try:
            answer = input("\nOpen the converted file now? [y/N]: ").strip().lower()
        except Exception:
            answer = ""
        if answer == "y":
            try:
                os.startfile(qsch_file)
            except Exception as exc:
                print(f"  NOTE: could not open {qsch_file} automatically: {exc}")

    return 0


def main():
    if len(sys.argv) == 1 or sys.argv[1] in ("--gui", "-g"):
        run_gui()
        return 0
    return run_cli_combined(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())