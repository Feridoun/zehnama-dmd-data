#!/usr/bin/env python3
"""Convert an NHS BSA TRUD dm+d release into the Zehnama JSON pack format.

The TRUD dm+d distribution is a ZIP of XML files. The principal files are:

    f_vmp2_*.xml      Virtual Medicinal Products (the dispensable abstractions)
    f_vtm_*.xml       Virtual Therapeutic Moieties (the drug-substance roots)
    f_lookup2_*.xml   Code-to-description lookups (forms, routes, units)
    f_amp_*.xml       Actual Medicinal Products (brand-level — optional here)
    f_bnf_*.xml       BNF mappings (optional, may live in supplementary pack)

We extract a minimal denormalised view sufficient for prescription quick-entry,
allergy checking, and interaction lookups. The output matches the schema
consumed by `lib/services/dmd_service.dart` in the Zehnama application.

Usage:
    python convert_trud_dmd.py <trud-zip-or-folder> --version 2026.05.17 \
        [--out dist] [--include-amp] [--limit 0]

Outputs (in --out, default `dist`):
    dmd_medications.json          The pack itself.
    dmd_medications.json.sha256   sha256sum-format sidecar.

The script is intentionally dependency-free (standard library only) so it can
run in CI without `pip install`, and so we can audit every transformation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Lookup tables (extracted from f_lookup2_*.xml)
# ---------------------------------------------------------------------------


@dataclass
class Lookups:
    forms: Dict[str, str] = field(default_factory=dict)
    routes: Dict[str, str] = field(default_factory=dict)
    units: Dict[str, str] = field(default_factory=dict)


LOOKUP_GROUPS = {
    "FORM": "forms",
    "ROUTE": "routes",
    "UNIT_OF_MEASURE": "units",
}


def parse_lookups(path: Path) -> Lookups:
    out = Lookups()
    tree = ET.parse(str(path))
    root = tree.getroot()
    for group in root:
        tag = group.tag.upper()
        attr = LOOKUP_GROUPS.get(tag)
        if not attr:
            continue
        table: Dict[str, str] = getattr(out, attr)
        for info in group.findall("./INFO"):
            cd = (info.findtext("CD") or "").strip()
            desc = (info.findtext("DESC") or "").strip()
            if cd and desc:
                table[cd] = desc
    return out


# ---------------------------------------------------------------------------
# VTM (Virtual Therapeutic Moiety) — root drug substance
# ---------------------------------------------------------------------------


@dataclass
class Vtm:
    code: str
    name: str


def parse_vtms(path: Path) -> Dict[str, Vtm]:
    vtms: Dict[str, Vtm] = {}
    tree = ET.parse(str(path))
    root = tree.getroot()
    for el in root.iter("VTM"):
        vtmid = (el.findtext("VTMID") or "").strip()
        name = (el.findtext("NM") or "").strip()
        if vtmid and name:
            vtms[vtmid] = Vtm(code=f"VTM{vtmid}", name=name)
    return vtms


# ---------------------------------------------------------------------------
# VMP (Virtual Medicinal Product) — the dispensable entity
# ---------------------------------------------------------------------------


@dataclass
class Vmp:
    vpid: str
    name: str
    vtmid: Optional[str]
    form_codes: List[str] = field(default_factory=list)
    route_codes: List[str] = field(default_factory=list)
    strength: Optional[str] = None


def _format_strength(
    numerator_value: Optional[str],
    numerator_unit: Optional[str],
    denominator_value: Optional[str],
    denominator_unit: Optional[str],
    units: Dict[str, str],
) -> Optional[str]:
    def fmt(value: Optional[str], unit_code: Optional[str]) -> Optional[str]:
        if not value:
            return None
        # Trim trailing zeros from decimals like "500.000" -> "500"
        v = value
        if "." in v:
            v = v.rstrip("0").rstrip(".")
        unit = units.get(unit_code or "", "")
        return f"{v}{unit}" if unit else v

    num = fmt(numerator_value, numerator_unit)
    den = fmt(denominator_value, denominator_unit)
    if num and den:
        return f"{num}/{den}"
    return num or den


def parse_vmps(path: Path, units: Dict[str, str]) -> List[Vmp]:
    vmps: List[Vmp] = []
    vmp_by_id: Dict[str, Vmp] = {}
    tree = ET.parse(str(path))
    root = tree.getroot()

    # 1) VMPs themselves.
    for el in root.iter("VMP"):
        vpid = (el.findtext("VPID") or "").strip()
        name = (el.findtext("NM") or "").strip()
        vtmid = (el.findtext("VTMID") or "").strip() or None
        if not vpid or not name:
            continue
        vmp = Vmp(vpid=vpid, name=name, vtmid=vtmid)
        vmps.append(vmp)
        vmp_by_id[vpid] = vmp

    # 2) Form links.
    for el in root.iter("DFORM_IND"):
        pass  # legacy hook; ignore
    for el in root.iter("DFORM"):  # newer tag
        vpid = (el.findtext("VPID") or "").strip()
        form_cd = (el.findtext("FORMCD") or "").strip()
        v = vmp_by_id.get(vpid)
        if v and form_cd:
            v.form_codes.append(form_cd)

    # 3) Route links.
    for el in root.iter("DROUTE"):
        vpid = (el.findtext("VPID") or "").strip()
        route_cd = (el.findtext("ROUTECD") or "").strip()
        v = vmp_by_id.get(vpid)
        if v and route_cd:
            v.route_codes.append(route_cd)

    # 4) Strengths (VPI = Virtual Product Ingredient).
    #    A VMP may have multiple ingredients; we concatenate as "500mg + 125mg".
    strengths_by_vpid: Dict[str, List[str]] = {}
    for el in root.iter("VPI"):
        vpid = (el.findtext("VPID") or "").strip()
        if not vpid or vpid not in vmp_by_id:
            continue
        s = _format_strength(
            el.findtext("STRNT_NMRTR_VAL"),
            el.findtext("STRNT_NMRTR_UOMCD"),
            el.findtext("STRNT_DNMTR_VAL"),
            el.findtext("STRNT_DNMTR_UOMCD"),
            units,
        )
        if s:
            strengths_by_vpid.setdefault(vpid, []).append(s)

    for vpid, parts in strengths_by_vpid.items():
        vmp_by_id[vpid].strength = " + ".join(parts)

    return vmps


# ---------------------------------------------------------------------------
# Optional: AMP (Actual Medicinal Product) brand names
# ---------------------------------------------------------------------------


def parse_amp_brands(path: Path) -> Dict[str, str]:
    """Return {vpid: first_brand_name} from f_amp_*.xml."""
    brands: Dict[str, str] = {}
    tree = ET.parse(str(path))
    root = tree.getroot()
    for el in root.iter("AMP"):
        vpid = (el.findtext("VPID") or "").strip()
        name = (el.findtext("NM") or "").strip()
        if vpid and name and vpid not in brands:
            brands[vpid] = name
    return brands


# ---------------------------------------------------------------------------
# File discovery (TRUD names files like f_vmp2_3030124.xml — date suffix varies)
# ---------------------------------------------------------------------------


def find_one(folder: Path, pattern: str) -> Optional[Path]:
    rx = re.compile(pattern, re.IGNORECASE)
    candidates = sorted(p for p in folder.rglob("*.xml") if rx.search(p.name))
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_pack(
    folder: Path,
    version: str,
    include_amp: bool,
    limit: int,
) -> dict:
    lookup_xml = find_one(folder, r"^f_lookup2_")
    vtm_xml = find_one(folder, r"^f_vtm_")
    vmp_xml = find_one(folder, r"^f_vmp2?_")
    amp_xml = find_one(folder, r"^f_amp_") if include_amp else None

    if not vmp_xml:
        raise SystemExit(
            "ERROR: Could not find f_vmp2_*.xml in the TRUD release. "
            "Did you pass the correct ZIP / folder?"
        )
    if not vtm_xml:
        raise SystemExit("ERROR: Could not find f_vtm_*.xml.")

    lookups = parse_lookups(lookup_xml) if lookup_xml else Lookups()
    vtms = parse_vtms(vtm_xml)
    vmps = parse_vmps(vmp_xml, lookups.units)
    brands = parse_amp_brands(amp_xml) if amp_xml else {}

    meds: List[dict] = []
    for vmp in vmps:
        vtm = vtms.get(vmp.vtmid or "")
        form = lookups.forms.get(vmp.form_codes[0], "") if vmp.form_codes else ""
        route = (
            lookups.routes.get(vmp.route_codes[0], "") if vmp.route_codes else ""
        )
        entry = {
            "vtmCode": vtm.code if vtm else "",
            "vmpCode": f"VMP{vmp.vpid}",
            "name": vtm.name if vtm else vmp.name,
        }
        brand = brands.get(vmp.vpid)
        if brand and brand.lower() != entry["name"].lower():
            entry["brandName"] = brand
        if form:
            entry["form"] = form.lower()
        if vmp.strength:
            entry["strength"] = vmp.strength
        if route:
            entry["route"] = route.lower()
        # bnfCode / atcCode are not in the core dm+d release; left blank here
        # and may be enriched from supplementary BNF/ATC mappings if available.
        meds.append(entry)

    meds.sort(key=lambda m: (m.get("name", ""), m.get("strength", "")))
    if limit > 0:
        meds = meds[:limit]

    return {
        "version": version,
        "lastUpdated": date.today().isoformat(),
        "description": "NHS dm+d auto-generated pack for Zehnama",
        "source": "NHS Business Services Authority (TRUD)",
        "medications": meds,
    }


def write_outputs(pack: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dmd_medications.json"
    digest_path = out_dir / "dmd_medications.json.sha256"

    payload = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
    json_path.write_text(payload, encoding="utf-8")

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    digest_path.write_text(f"{digest}  {json_path.name}\n", encoding="utf-8")

    print(f"  wrote {json_path}  ({len(payload):,} bytes, {len(pack['medications']):,} meds)")
    print(f"  wrote {digest_path}  ({digest})")


def resolve_source(source: Path) -> Path:
    """Accept either a TRUD ZIP or an already-extracted folder."""
    if source.is_dir():
        return source
    if source.suffix.lower() == ".zip" and source.is_file():
        tmp = Path(tempfile.mkdtemp(prefix="trud_dmd_"))
        print(f"  extracting {source} -> {tmp}")
        with zipfile.ZipFile(source) as zf:
            zf.extractall(tmp)
        return tmp
    raise SystemExit(f"ERROR: source must be a TRUD .zip or folder: {source}")


def main(argv: Iterable[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path, help="Path to TRUD dm+d .zip or extracted folder")
    p.add_argument("--version", required=True, help="Pack version tag, e.g. 2026.05.17")
    p.add_argument("--out", type=Path, default=Path("dist"), help="Output directory")
    p.add_argument("--include-amp", action="store_true", help="Enrich with AMP brand names")
    p.add_argument("--limit", type=int, default=0, help="Truncate to N entries (0 = all)")
    args = p.parse_args(list(argv))

    if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", args.version):
        print(f"WARNING: version '{args.version}' is not YYYY.MM.DD — the app"
              f" compares versions numerically; non-conforming tags may be"
              f" mis-ordered.", file=sys.stderr)

    folder = resolve_source(args.source)
    print(f"  parsing TRUD release from {folder}")
    pack = build_pack(folder, args.version, args.include_amp, args.limit)
    write_outputs(pack, args.out)
    print("  done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
