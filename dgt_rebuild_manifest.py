# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
dgt_rebuild_manifest.py
=======================

Rebuild a COMPLETE manifest.csv by scanning every *.ImageProperties.xml already
saved under the museu tree — independent of how/when each series was harvested.

Each XML was saved as  <out>/<series>/<cota>.ImageProperties.xml  and the tile
URL is reconstructed from a series->base map (the only thing not in the file).

Usage
-----
    uv run dgt_rebuild_manifest.py --museu ./dgt_museu
    # writes ./dgt_museu/manifest.csv (backs up any existing one)
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
from pathlib import Path
from urllib.parse import quote

ROOT = "https://www.dgterritorio.gov.pt:8443/museu"

# series folder name -> tile base path under /museu/
BASE = {
    "100K_nova": "mv_2011/100K_nova",
    "200K": "mv_2011/200K",
    "Carta_Agricola": "MV_2011/Carta_Agricola",
    "Madeira": "cartas/Madeira",
    "Carta_Lx_1K": "cartas/Carta_Lx_1K",
    "Carta_GLx_10K": "cartas/Carta_GLx_10K",
    "Acores": "mv_2011/A\u00e7ores",
    "Carta_SCN_50K": "cartas/Carta_SCN_50K",
    "Carta_100K": "cartas/Carta_100K",
}
ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']?([^"'\s/>]+)["']?""")


def enc_base(base: str) -> str:
    return "/".join(quote(seg) for seg in base.split("/"))


def xml_url(base: str, cota: str) -> str:
    return f"{ROOT}/{enc_base(base)}/{quote(cota, safe='()-_.')}/ImageProperties.xml"


def attrs(text: str) -> dict[str, str]:
    m = re.search(r"<IMAGE_PROPERTIES\b([^>]*)>", text, re.I)
    return {k.upper(): v for k, v in ATTR_RE.findall(m.group(1))} if m else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--museu", default="./dgt_museu")
    args = ap.parse_args()
    museu = Path(args.museu)

    rows = []
    unknown = set()
    for xml in sorted(museu.rglob("*.ImageProperties.xml")):
        series = xml.parent.name
        cota = xml.name[: -len(".ImageProperties.xml")]
        base = BASE.get(series)
        if base is None:
            unknown.add(series)
            base = f"cartas/{series}"  # best-effort fallback
        a = attrs(xml.read_text(encoding="utf-8", errors="replace"))
        if not a:
            rows.append([series, "", cota, xml_url(base, cota), "error", "", "", "",
                         "", "", "", str(xml), "unparseable"])
            continue
        rows.append([series, "", cota, xml_url(base, cota), "ok",
                     a.get("WIDTH", ""), a.get("HEIGHT", ""), a.get("TILESIZE", ""),
                     a.get("NUMTILES", ""), a.get("NUMIMAGES", ""), a.get("VERSION", ""),
                     str(xml), ""])

    man = museu / "manifest.csv"
    if man.exists():
        shutil.copy2(man, man.with_suffix(".csv.bak"))
    with man.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["series", "folha", "cota", "url", "status", "width", "height",
                    "tilesize", "numtiles", "numimages", "version", "xml_path", "error"])
        w.writerows(rows)

    by: dict[str, int] = {}
    for r in rows:
        by[r[0]] = by.get(r[0], 0) + 1
    print(f"Wrote {man} with {len(rows)} sheets across {len(by)} series:")
    for k in sorted(by):
        print(f"    {k:16} {by[k]}")
    if unknown:
        print(f"\nNote: unknown series folders (guessed base cartas/<name>): {sorted(unknown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
