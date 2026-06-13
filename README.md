# DGT Museu Virtual — Map Harvester

Ferramentas para descarregar todas as folhas de mapas históricos do *Museu Virtual* da DGT (https://www.dgterritorio.gov.pt:8443/museu/) e reconstruir cada uma como uma
imagem em resolução total.

O site disponibiliza cada folha como uma pirâmide de mosaicos **Zoomify** (um ficheiro `ImageProperties.xml`
mais pastas `TileGroupN/` com mosaicos JPEG). Estes scripts enumeram cada folha,
espelham o seu ficheiro `ImageProperties. xml` e, em seguida, descarregam e unem os mosaicos numa
única imagem por folha.

Tools to download every historical map sheet from the DGT *Museu Virtual*
(`https://www.dgterritorio.gov.pt:8443/museu/`) and reconstruct each one as a
full-resolution image.

The site serves each sheet as a **Zoomify** tile pyramid (an `ImageProperties.xml`
plus `TileGroupN/` folders of JPEG tiles). These scripts enumerate every sheet,
mirror its `ImageProperties.xml`, then download and stitch the tiles back into a
single image per sheet.

## Scripts

| Script | Purpose |
|--------|---------|
| `dgt_zoomify_harvest.py` | Enumerate all 9 series and download every `ImageProperties.xml` |
| `dgt_rebuild_manifest.py` | Rebuild a complete `manifest.csv` from the files already on disk |
| `dgt_stitch.py` | Download tile pyramids and stitch each sheet into a full image |

All three are self-contained [`uv`](https://docs.astral.sh/uv/) scripts (PEP 723
inline dependencies) — `uv` fetches what they need on first run. No manual installs.

## The nine series

| Series (folder) | Method | Sheets |
|-----------------|--------|--------|
| `100K_nova` | cota | 53 |
| `200K` | cota | 8 |
| `Carta_Agricola` | cota | 35 |
| `Madeira` | code | 29 |
| `Carta_Lx_1K` | code | 65 |
| `Carta_GLx_10K` | code | 19 |
| `Acores` | code | 4 |
| `Carta_SCN_50K` | folha | 175 (171 published) |
| `Carta_100K` | folha | 40 (37 published) |

Three enumeration mechanisms, worked out by inspecting the site:

* **cota** — the viewer page lists sheets directly as `?cota=<CODE>`; the code is
  the tile-folder name.
* **code** — the cartograma image-map lists sheets as `?c=<CODE>`; the code is the
  tile-folder name.
* **folha** — the cartograma lists `?folha=<F>`; each folha must be fetched from a
  `…_rslt.asp?folha=<F>` page **with the cartograma as HTTP Referer**, and the real
  cota is read from the embedded tile path. This editions endpoint is flaky
  (intermittent HTTP 500) and is referer-gated, so a plain HTTP client gets blanks
  — the two folha series are enumerated in the browser instead (see below).

The static `ImageProperties.xml` and tile files are fast and unthrottled; only the
dynamic `.asp` folha enumeration misbehaves.

---

## Workflow

### 1. Harvest the metadata (the 7 reliable series)

```bash
uv run dgt_zoomify_harvest.py --probe            # enumerate + count, no download
uv run dgt_zoomify_harvest.py --out ./dgt_museu  # download all ImageProperties.xml
```

This pulls the seven non-folha series (~213 sheets) straight from a plain HTTP
client — reliable, zero errors expected. To do only those and skip the flaky
folha pair:

```bash
uv run dgt_zoomify_harvest.py --out ./dgt_museu --skip-folha
```

Re-runs skip files already on disk, so it's safe to run repeatedly.

### 2. Harvest the two folha series (50K + old 100K) in the browser

These need a genuine browser Referer, so enumerate them in the browser console.
On each cartograma page —

* 50K:  `https://www.dgterritorio.gov.pt:8443/museu/cart_50k.asp?e=1`
* 100K: `https://www.dgterritorio.gov.pt:8443/museu/cart_100k.asp?e=1`

— run the enumeration snippet (gentle, one folha at a time, retries on 500). It
crawls every `folha=`, follows each to its `…_rslt.asp` editions page with the
referer set, reads the real cota, and downloads a `<series>_urls.txt` list of full
`ImageProperties.xml` URLs.

Then feed those URL lists to the harvester:

```bash
uv run dgt_zoomify_harvest.py --out ./dgt_museu --urls-only \
  --urls-file Carta_SCN_50K_urls.txt --urls-file Carta_100K_urls.txt
```

`--urls-only` skips the built-in enumeration and just downloads from the lists, so
it won't re-touch the flaky endpoint or re-scan the seven you already have.

> A few sheets in 50K/100K are dead references — the editions database lists them
> but DGT never published the tiles (they 404). Those are expected and can be
> dropped from the URL lists; everything else downloads cleanly.

### 3. Rebuild the manifest (important)

The harvester rewrites `manifest.csv` on each run, so a `--urls-only` pass can
leave it holding only the last batch. Rebuild a complete manifest from the XML
files actually on disk — independent of how each series was fetched:

```bash
uv run dgt_rebuild_manifest.py --museu ./dgt_museu
```

This scans every `*.ImageProperties.xml`, reconstructs each tile URL, and writes a
full `manifest.csv` (backing up the old one to `manifest.csv.bak`). It prints all
nine series with their counts (~421 total). **Run this whenever the manifest looks
short — the files on disk are the source of truth.**

### 4. Stitch the sheets into images

```bash
# test a few first:
uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images --series Madeira --limit 3

# then everything (full resolution):
uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images
```

For each sheet it reads `WIDTH/HEIGHT/TILESIZE/NUMTILES` from the manifest,
rebuilds the Zoomify pyramid, downloads the tiles, and stitches + crops them into
one image at `dgt_images/<series>/<cota>.jpg`. Re-runs skip sheets already done.

Lighter / faster browse set — cap the long edge (picks the nearest pyramid level,
far fewer tiles):

```bash
uv run dgt_stitch.py --museu ./dgt_museu --out ./dgt_images --max-dim 4096
```

> Keep the resolution consistent across series — if you stitch some at full res and
> some with `--max-dim`, you'll get a mixed-resolution set. Stick to one.

Each sheet self-validates before downloading: it compares the computed pyramid
tile count against the manifest's `NUMTILES`, and skips with a `??` warning rather
than producing a garbled image if they disagree. The per-sheet log flags
`(N tiles missing)` if any tile failed — re-pull those by deleting that single
`.jpg` and re-running.

---

## Output layout

```
dgt_museu/
  manifest.csv                         # width/height/tilesize/numtiles per sheet
  <series>/<cota>.ImageProperties.xml  # mirrored Zoomify metadata
dgt_images/
  <series>/<cota>.jpg                  # stitched full-resolution sheets
```

## Common options

| Flag | Scripts | Purpose |
|------|---------|---------|
| `--out` | all | Output directory |
| `--probe` | harvest | Enumerate + count, download nothing |
| `--only <name>` | harvest | Limit to series name(s) |
| `--skip-folha` | harvest | Skip the two flaky folha series |
| `--urls-file` / `--urls-only` | harvest | Download from browser-made URL lists |
| `--insecure` | harvest, stitch | Disable TLS verification (if the `:8443` chain trips) |
| `--series` / `--limit` | stitch | Restrict which sheets to stitch |
| `--max-dim N` | stitch | Cap the long edge to ~N px (lighter output) |
| `--format jpg\|png` | stitch | Output format (default jpg, quality 90) |

## Notes

* The Museu Virtual is a public archive of historical cartography. These tools
  mirror its content for offline use; mind DGT's terms for any redistribution.
* The `:8443` host occasionally has an incomplete TLS chain on some clients — add
  `--insecure` if you hit `CERTIFICATE_VERIFY_FAILED`.
* The flaky folha endpoint can need a couple of passes; re-running only retries the
  gaps, since completed sheets are cached on disk.
