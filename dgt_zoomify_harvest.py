# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""
dgt_zoomify_harvest.py  (v5 — all nine series)
==============================================

Download every Zoomify `ImageProperties.xml` from the DGT "Museu Virtual".

Nine series, three enumeration methods (all worked out by inspection):

  method "cota"  — viewer ?e=1 lists sheets directly as ?cota=<CODE>; CODE is
                   the folder name. (100K_nova, 200K, Carta_Agricola)
  method "code"  — cartograma ?e=1 lists sheets as image-map ?c=<CODE>; CODE is
                   the folder name. (Madeira, Carta_Lx_1K, Carta_GLx_10K, Acores)
  method "folha" — cartograma ?e=1 lists ?folha=<F>; each folha must be fetched
                   from <rslt>?folha=<F> WITH the cartograma as Referer, and the
                   real cota is read from the embedded tile path. This endpoint
                   is flaky (intermittent HTTP 500) so it is retried with
                   backoff and low concurrency. (Carta_SCN_50K, Carta_100K)

In every case the `ImageProperties.xml` files themselves are small, static and
fast — only the folha *enumeration* endpoint misbehaves.

Usage
-----
    uv run dgt_zoomify_harvest.py --probe            # enumerate, count, no download
    uv run dgt_zoomify_harvest.py --out ./dgt_museu  # full harvest (all 9)
    uv run dgt_zoomify_harvest.py --out ./dgt_museu --only Madeira --only Acores
    uv run dgt_zoomify_harvest.py --out ./dgt_museu --skip-folha   # the easy 7 only
    uv run dgt_zoomify_harvest.py --insecure ...     # if :8443 TLS chain trips

Re-runs skip files already on disk, so folha series can be resumed until the
flaky endpoint has coughed up every sheet.

Output
------
    ./dgt_museu/<series>/<cota>.ImageProperties.xml
    ./dgt_museu/manifest.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx

ROOT = "https://www.dgterritorio.gov.pt:8443/museu"

# name, method, enum page (?e=1), tile base dir, and (folha only) rslt endpoint
SERIES: list[dict] = [
    {"name": "100K_nova", "method": "cota",
     "enum": f"{ROOT}/mv_2011/cart_100k_nova.asp", "base": "mv_2011/100K_nova"},
    {"name": "200K", "method": "cota",
     "enum": f"{ROOT}/mv_2011/cart_200k.asp", "base": "mv_2011/200K"},
    {"name": "Carta_Agricola", "method": "cota",
     "enum": f"{ROOT}/MV_2011/Cart_Carta_Agricola.asp", "base": "MV_2011/Carta_Agricola"},
    {"name": "Madeira", "method": "code",
     "enum": f"{ROOT}/cart_madeira.asp", "base": "cartas/Madeira"},
    {"name": "Carta_Lx_1K", "method": "code",
     "enum": f"{ROOT}/cart_lx1k_ff.asp", "base": "cartas/Carta_Lx_1K"},
    {"name": "Carta_GLx_10K", "method": "code",
     "enum": f"{ROOT}/cart_glx10k.asp", "base": "cartas/Carta_GLx_10K"},
    {"name": "Acores", "method": "code",
     "enum": f"{ROOT}/mv_2011/cart_acores.asp", "base": "mv_2011/A\u00e7ores"},
    {"name": "Carta_SCN_50K", "method": "folha",
     "enum": f"{ROOT}/cart_50k.asp", "rslt": f"{ROOT}/Cart_50K_rslt.asp",
     "base": "cartas/Carta_SCN_50K"},
    {"name": "Carta_100K", "method": "folha",
     "enum": f"{ROOT}/cart_100k.asp", "rslt": f"{ROOT}/Cart_100K_rslt.asp",
     "base": "cartas/Carta_100K"},
]

UA = "dgt-zoomify-harvest/5.0 (map metadata harvest)"
RE = {
    "cota": re.compile(r"cota=([^\"'&<>\s]+)", re.I),
    "code": re.compile(r"[?&]c=([^\"'&<>\s]+)", re.I),
    "folha": re.compile(r"folha=([^\"'&<>\s]+)", re.I),
}
ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']?([^"'\s/>]+)["']?""")


def decode(content: bytes, headers: httpx.Headers) -> str:
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
    for cand in (m.group(1) if m else "", "utf-8", "cp1252", "latin-1"):
        if not cand:
            continue
        try:
            return content.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def uniq(matches) -> list[str]:
    seen: dict[str, None] = {}
    for raw in matches:
        v = unquote(raw).strip()
        if v:
            seen.setdefault(v, None)
    return list(seen)


def parse_xml(text: str) -> dict[str, str]:
    text = text.strip()
    try:
        return {k.upper(): v for k, v in ET.fromstring(text).attrib.items()}
    except ET.ParseError:
        m = re.search(r"<IMAGE_PROPERTIES\b([^>]*)>", text, re.I)
        return {k.upper(): v for k, v in ATTR_RE.findall(m.group(1))} if m else {}


def enc_base(base: str) -> str:
    return "/".join(quote(seg) for seg in base.split("/"))


def xml_url(base: str, cota: str) -> str:
    return f"{ROOT}/{enc_base(base)}/{quote(cota, safe='()-_.')}/ImageProperties.xml"


def local_path(out: Path, series: str, cota: str) -> Path:
    return out / series / f"{cota.replace('/', '_')}.ImageProperties.xml"


def parse_url(url: str) -> tuple[str, str, str] | None:
    """From a full .../museu/<base>/<cota>/ImageProperties.xml URL return
    (series_name, base, cota). series_name is the last base segment."""
    from urllib.parse import urlsplit
    parts = [unquote(p) for p in urlsplit(url).path.split("/") if p]
    if len(parts) < 4 or not parts[-1].lower().startswith("imageproperties"):
        return None
    try:
        mi = parts.index("museu")
    except ValueError:
        mi = -1
    base_parts = parts[mi + 1:-2]
    cota = parts[-2]
    if not base_parts:
        return None
    return base_parts[-1], "/".join(base_parts), cota


def read_url_files(paths: list[str]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if not u or u.startswith("#") or u in seen:
                continue
            seen.add(u)
            parsed = parse_url(u)
            if parsed:
                out.append(parsed)
    return out


@dataclass
class Row:
    series: str
    cota: str
    folha: str = ""
    url: str = ""
    status: str = ""
    width: str = ""
    height: str = ""
    tilesize: str = ""
    numtiles: str = ""
    numimages: str = ""
    version: str = ""
    xml_path: str = ""
    error: str = ""


def fill(r: Row, a: dict[str, str]) -> None:
    r.width = a.get("WIDTH", ""); r.height = a.get("HEIGHT", "")
    r.tilesize = a.get("TILESIZE", ""); r.numtiles = a.get("NUMTILES", "")
    r.numimages = a.get("NUMIMAGES", ""); r.version = a.get("VERSION", "")


async def get_text(client, url, headers=None, tries=1, backoff=0.5):
    last = None
    for attempt in range(tries):
        try:
            r = await client.get(url, headers=headers or {})
            if r.status_code == 200:
                return r, decode(r.content, r.headers)
            last = f"HTTP {r.status_code}"
            if r.status_code in (500, 502, 503, 504) and attempt < tries - 1:
                await asyncio.sleep(backoff * (2 ** attempt))
                continue
            return r, decode(r.content, r.headers)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            if attempt < tries - 1:
                await asyncio.sleep(backoff * (2 ** attempt))
    return None, last or "failed"


async def enumerate_series(client, sem, s: dict) -> list[tuple[str, str]]:
    """Return [(cota, folha)] for a series."""
    method = s["method"]
    if method in ("cota", "code"):
        r, t = await get_text(client, s["enum"] + "?e=1")
        if r is None or r.status_code != 200:
            return []
        return [(c, "") for c in uniq(RE[method].findall(t))]

    # folha: scrape folhas from cartograma, then fetch each rslt WITH referer
    referer = s["enum"] + "?e=1"
    r, t = await get_text(client, referer)
    if r is None or r.status_code != 200:
        return []
    folhas = uniq(RE["folha"].findall(t))
    dirname = re.escape(s["base"].split("/")[-1])
    cota_re = re.compile(dirname + r"/([^\"'/?\s]+)/", re.I)
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    async def one(folha: str):
        url = f"{s['rslt']}?folha={quote(folha, safe='')}"
        async with sem:
            r2, t2 = await get_text(client, url, headers={"Referer": referer},
                                    tries=5, backoff=0.6)
            await asyncio.sleep(0.25)  # be gentle on the flaky endpoint
        if r2 is None or r2.status_code != 200:
            return
        for c in dict.fromkeys(cota_re.findall(t2)):
            c = unquote(c)
            if c not in seen:
                seen.add(c); pairs.append((c, folha))

    # low concurrency on purpose
    fsem = asyncio.Semaphore(3)

    async def guarded(f):
        async with fsem:
            await one(f)

    done = 0
    for chunk_start in range(0, len(folhas), 25):
        chunk = folhas[chunk_start:chunk_start + 25]
        await asyncio.gather(*(guarded(f) for f in chunk))
        done += len(chunk)
        print(f"    {s['name']}: {done}/{len(folhas)} folhas, {len(pairs)} cotas",
              file=sys.stderr)
    return pairs


async def download(client, sem, series, base, cota, folha, out, refresh, delay) -> Row:
    row = Row(series=series, cota=cota, folha=folha, url=xml_url(base, cota))
    dest = local_path(out, series, cota)
    row.xml_path = str(dest)
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        row.status = "cached"; fill(row, parse_xml(dest.read_text(encoding="utf-8", errors="replace")))
        return row
    async with sem:
        r, t = await get_text(client, row.url, tries=3, backoff=0.4)
        if delay:
            await asyncio.sleep(delay)
    if r is None or r.status_code != 200:
        row.status, row.error = "error", (t if isinstance(t, str) else "failed")
        return row
    attrs = parse_xml(t)
    if not attrs:
        row.status, row.error = "error", "no IMAGE_PROPERTIES attrs"
        return row
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(t, encoding="utf-8")
    row.status = "ok"; fill(row, attrs)
    return row


async def run(args) -> int:
    out = Path(args.out)
    only = set(args.only or [])
    chosen = [s for s in SERIES if (not only or s["name"] in only)
              and not (args.skip_folha and s["method"] == "folha")]

    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, follow_redirects=True,
        verify=not args.insecure, timeout=httpx.Timeout(args.timeout),
        limits=httpx.Limits(max_connections=args.concurrency * 2),
    ) as client:
        sem = asyncio.Semaphore(args.concurrency)

        plan: list[tuple[str, str, list[tuple[str, str]]]] = []
        if not args.urls_only:
            print("=== ENUMERATING ===")
            for s in chosen:
                pairs = await enumerate_series(client, sem, s)
                tag = "OK " if pairs else "-- "
                print(f"{tag} {s['name']:16} {len(pairs):>4} sheets  ({s['method']})  base={s['base']}")
                if pairs:
                    plan.append((s["name"], s["base"], pairs))

        if args.urls_file:
            ingested: dict[tuple[str, str], list[tuple[str, str]]] = {}
            for series, base, cota in read_url_files(args.urls_file):
                ingested.setdefault((series, base), []).append((cota, ""))
            for (series, base), pairs in ingested.items():
                print(f"URLS {series:16} {len(pairs):>4} sheets  base={base}")
                plan.append((series, base, pairs))

        total = sum(len(p) for _, _, p in plan)
        print(f"--- {total} sheets across {len(plan)} series ---")
        if args.probe or total == 0:
            return 0 if total else 1

        out.mkdir(parents=True, exist_ok=True)
        tasks = [download(client, sem, name, base, cota, folha, out, args.refresh, args.delay)
                 for name, base, pairs in plan for (cota, folha) in pairs]
        rows: list[Row] = []
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            rows.append(await coro)
            if i % 50 == 0 or i == len(tasks):
                ok = sum(r.status in ("ok", "cached") for r in rows)
                print(f"  {i}/{len(tasks)}  ok={ok}  err={sum(r.status=='error' for r in rows)}",
                      file=sys.stderr)

        man = out / "manifest.csv"
        with man.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["series", "folha", "cota", "url", "status", "width", "height",
                        "tilesize", "numtiles", "numimages", "version", "xml_path", "error"])
            for r in sorted(rows, key=lambda x: (x.series, x.cota)):
                w.writerow([r.series, r.folha, r.cota, r.url, r.status, r.width, r.height,
                            r.tilesize, r.numtiles, r.numimages, r.version, r.xml_path, r.error])

        ok = sum(r.status in ("ok", "cached") for r in rows)
        err = [r for r in rows if r.status == "error"]
        print(f"\nDone. {ok} sheets, {len(err)} errors. Manifest: {man}")
        by = {}
        for r in rows:
            by.setdefault(r.series, [0, 0])
            by[r.series][0 if r.status in ("ok", "cached") else 1] += 1
        for k in sorted(by):
            print(f"    {k:16} ok={by[k][0]:>4}  err={by[k][1]}")
        if err:
            print("Re-run to retry errored sheets (cached ones are skipped).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="./dgt_museu")
    ap.add_argument("--only", action="append", help="Limit to series name(s).")
    ap.add_argument("--skip-folha", action="store_true",
                    help="Skip the two flaky folha series (50K, 100K) — do the easy 7.")
    ap.add_argument("--urls-file", action="append",
                    help="Text file(s) of full ImageProperties.xml URLs (e.g. the 50K/100K "
                         "lists produced in the browser). Repeatable.")
    ap.add_argument("--urls-only", action="store_true",
                    help="Skip built-in enumeration; only download from --urls-file.")
    ap.add_argument("--probe", action="store_true", help="Enumerate + count, no download.")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--insecure", action="store_true", help="Disable TLS verification.")
    ap.add_argument("--refresh", action="store_true", help="Re-download existing files.")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
