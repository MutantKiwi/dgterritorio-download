# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "pillow>=10.0"]
# ///
"""
dgt_stitch.py
=============

Reconstruct full-resolution map images from the DGT Zoomify pyramids whose
`ImageProperties.xml` you harvested with dgt_zoomify_harvest.py.

For each sheet it reads WIDTH/HEIGHT/TILESIZE/NUMTILES from the manifest (or the
saved XML), works out the Zoomify tile pyramid, downloads the tiles for the
chosen zoom level, and stitches + crops them into one image.

Zoomify addressing (verified: sum of tiles across levels == NUMTILES):
  * level 0 is the smallest (whole image in <=1 tile); the top level is full res
  * tiles are numbered from level 0 upward, row-major; tile_index // 256 selects
    the TileGroup folder, so a tile is:
        {sheet}/TileGroup{index//256}/{level}-{col}-{row}.jpg

Usage
-----
    # full resolution, everything in the manifest:
    uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images

    # lighter: cap the long edge (~picks the nearest pyramid level <= N px):
    uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images --max-dim 4096

    # a subset / a quick test:
    uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images --series Madeira --limit 3

    --format png|jpg   (default jpg, quality 90)
    --insecure         if the :8443 TLS chain trips
    --concurrency 24   parallel tile downloads per sheet

Re-runs skip sheets whose output image already exists.

Note on georeferencing: these images have no embedded geo. Turning a sheet into
a GeoTIFF needs its corner coordinates (from the sheet-index / cartograma
geometry) + a warp — a separate step. Ask and that can be built next.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # these scans are large; we trust our own sizes


def pyramid(W: int, H: int, ts: int = 256) -> list[dict]:
    """Levels from 0 (smallest) to top (full res)."""
    dims = [(W, H)]
    w, h = W, H
    while w > ts or h > ts:
        w = (w + 1) // 2
        h = (h + 1) // 2
        dims.append((w, h))
    dims.reverse()
    out = []
    for (w, h) in dims:
        out.append({"w": w, "h": h, "cols": (w + ts - 1) // ts, "rows": (h + ts - 1) // ts})
    return out


def level_base_indices(levels: list[dict]) -> list[int]:
    base, acc = [], 0
    for lv in levels:
        base.append(acc)
        acc += lv["cols"] * lv["rows"]
    return base


def choose_level(levels: list[dict], max_dim: int | None) -> int:
    top = len(levels) - 1
    if not max_dim:
        return top
    for i in range(top, -1, -1):
        if max(levels[i]["w"], levels[i]["h"]) <= max_dim:
            return i
    return 0


@dataclass
class Sheet:
    series: str
    cota: str
    tilebase: str          # .../<cota>/   (ends with /)
    width: int
    height: int
    tilesize: int
    numtiles: int


def load_sheets(museu: Path) -> list[Sheet]:
    man = museu / "manifest.csv"
    sheets: list[Sheet] = []
    if not man.exists():
        raise SystemExit(f"manifest.csv not found in {museu}")
    with man.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("status") not in ("ok", "cached"):
                continue
            try:
                w = int(r["width"]); h = int(r["height"]); ts = int(r["tilesize"] or 256)
            except (ValueError, KeyError):
                continue
            url = r["url"]
            tilebase = url[: url.rfind("/") + 1]  # strip ImageProperties.xml
            sheets.append(Sheet(r["series"], r["cota"], tilebase, w, h, ts,
                                int(r.get("numtiles") or 0)))
    return sheets


async def fetch_tile(client, sem, url) -> bytes | None:
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.content:
                    return r.content
                if r.status_code in (500, 502, 503, 504):
                    await asyncio.sleep(0.4 * (attempt + 1)); continue
                return None
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.4 * (attempt + 1))
    return None


async def stitch_sheet(client, s: Sheet, out: Path, level_pref, max_dim,
                       fmt: str, quality: int, conc: int) -> tuple[str, str]:
    levels = pyramid(s.width, s.height, s.tilesize)
    total = sum(lv["cols"] * lv["rows"] for lv in levels)
    if s.numtiles and total != s.numtiles:
        return ("warn", f"tile-count mismatch (calc {total} vs NUMTILES {s.numtiles}); skipped")

    L = (len(levels) - 1) if level_pref == "top" else choose_level(levels, max_dim)
    lv = levels[L]
    base = level_base_indices(levels)[L]
    ts = s.tilesize
    dest = out / s.series / f"{s.cota.replace('/', '_')}.{fmt}"
    if dest.exists() and dest.stat().st_size > 0:
        return ("cached", str(dest))

    sem = asyncio.Semaphore(conc)
    coords = [(c, rrow) for rrow in range(lv["rows"]) for c in range(lv["cols"])]

    async def get(col, row):
        idx = base + row * lv["cols"] + col
        url = f"{s.tilebase}TileGroup{idx // 256}/{L}-{col}-{row}.jpg"
        return (col, row, await fetch_tile(client, sem, url))

    results = await asyncio.gather(*(get(c, r) for (c, r) in coords))
    missing = [(c, r) for (c, r, b) in results if not b]
    canvas = Image.new("RGB", (lv["cols"] * ts, lv["rows"] * ts), (255, 255, 255))
    for (c, r, b) in results:
        if not b:
            continue
        try:
            canvas.paste(Image.open(io.BytesIO(b)).convert("RGB"), (c * ts, r * ts))
        except Exception:  # noqa: BLE001
            missing.append((c, r))
    canvas = canvas.crop((0, 0, lv["w"], lv["h"]))

    dest.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jpg":
        canvas.save(dest, "JPEG", quality=quality, optimize=True)
    else:
        canvas.save(dest, fmt.upper())
    note = f"L{L} {lv['w']}x{lv['h']}"
    if missing:
        note += f"  ({len(missing)} tiles missing)"
    return ("ok", f"{dest}  [{note}]")


async def run(args) -> int:
    museu = Path(args.museu)
    out = Path(args.out)
    sheets = load_sheets(museu)
    if args.series:
        want = set(args.series)
        sheets = [s for s in sheets if s.series in want]
    if args.limit:
        sheets = sheets[: args.limit]
    if not sheets:
        print("No matching sheets in manifest."); return 1

    print(f"{len(sheets)} sheets to process -> {out}")
    ok = cached = warn = err = 0
    async with httpx.AsyncClient(
        headers={"User-Agent": "dgt-stitch/1.0"}, follow_redirects=True,
        verify=not args.insecure, timeout=httpx.Timeout(60.0),
        limits=httpx.Limits(max_connections=args.concurrency + 4),
    ) as client:
        for i, s in enumerate(sheets, 1):
            try:
                status, msg = await stitch_sheet(
                    client, s, out, "top" if not args.max_dim else "fit",
                    args.max_dim, args.format, args.quality, args.concurrency)
            except Exception as exc:  # noqa: BLE001
                status, msg = "error", f"{type(exc).__name__}: {exc}"
            ok += status == "ok"; cached += status == "cached"
            warn += status == "warn"; err += status == "error"
            tag = {"ok": "OK ", "cached": "== ", "warn": "?? ", "error": "!! "}[status]
            print(f"[{i}/{len(sheets)}] {tag}{s.series}/{s.cota}: {msg}")
    print(f"\nDone. ok={ok} cached={cached} warn={warn} error={err}  -> {out}")
    return 0 if not err else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--museu", default="./dgt_museu", help="Dir with manifest.csv + XML tree.")
    ap.add_argument("--out", default="./dgt_images")
    ap.add_argument("--series", action="append", help="Limit to series name(s).")
    ap.add_argument("--limit", type=int, help="Only the first N sheets (after filtering).")
    ap.add_argument("--max-dim", type=int,
                    help="Cap the long edge: pick the pyramid level <= this many px.")
    ap.add_argument("--format", choices=["jpg", "png"], default="jpg")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality (default 90).")
    ap.add_argument("--concurrency", type=int, default=24, help="Parallel tile downloads.")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr); return 130


if __name__ == "__main__":
    raise SystemExit(main())
