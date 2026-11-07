# xdssat documentation site

A Quarto website with Observable JS (Observable Plot) charts and maps. `getting-started.md` and
`feasibility-review.md` in this folder are project documents; the first is also rendered as a page.

| Section | Files |
|---|---|
| Home: xdssat as a bridge between DSSAT and Python | `index.qmd` |
| xdssat | `xdssat/index.qmd`, `xdssat/gridding.qmd`, `xdssat/implementation.qmd`, `getting-started.md` |
| Case study: rice in Java | `java/index.qmd` (story), `java/data.qmd`, `java/science.qmd`; their data in `java/data/` |
| DSSAT training (6 modules, adapted from the 2018 International Training Program on DSSAT) | `learn/index.qmd` (from the case study to DSSAT itself), `learn/module1.qmd` ... `module6.qmd`; their data in `learn/data/moduleN/` |
| Notebooks | `notebooks/index.qmd`, `notebooks/module1.ipynb` ... `module6.ipynb`; helpers `notebooks/learnlib.py` and `notebooks/glue.py` (GLUE port); the course's data files in `notebooks/course-data/` |
| About (navbar, right) | `faq.qmd`, `acknowledgements.qmd` (not an official DSSAT product; how to cite DSSAT and xdssat), `rebuild.qmd` |

| Folder | Holds |
|---|---|
| `assets/java/` | the case study's two static maps (`scripts/export_data.py`) and backup SVG and PNG of its charts, one folder per page |
| `assets/xdssat/` | backup SVG and PNG of the charts of the xdssat pages and the home page |
| `assets/logo/` | the logo (light and dark, with small copies for the navbar), `favicon.ico` (16-64 px), `favicon-32.png`, `apple-touch-icon.png` |
| `assets/training/` | backup SVG and PNG of every chart of the training pages, one folder per page |
| `assets/js/dl.js` | download buttons and chart helpers |
| `styles/` | `light.scss` and `dark.scss` (theme layers on cosmo and darkly, logo colours), `custom.css` |

No numbers are typed into the pages: they are read in the browser from small CSV and JSON files.
`scripts/export_data.py` writes `java/data/` and the two maps in `assets/java/` from the xdssat cache (`XDSSAT_CACHE`); each
training notebook writes its own `learn/data/<module>/` when it is run. The site never runs the DSSAT engine at
render time: Quarto does not execute `.ipynb` files by default (the stored outputs are shown) and
`freeze: auto` keeps anything executed.

Equations are written as `\( ... \)` (inline) and `\[ ... \]` (display). `$...$` math is switched off in
`_quarto.yml` (`from: markdown-tex_math_dollars+tex_math_single_backslash`): the dollar signs of Observable's
inline `${...}` expressions and of prices would otherwise pair up into math.

## Rebuild

From the repository root:

```bash
conda run --no-capture-output -n climate python docs/scripts/export_data.py   # refresh java/data/ and assets/java/
quarto render docs                                                           # write docs/_site/
python -m http.server 8765 --directory docs/_site                            # look at it
conda run --no-capture-output -n climate python docs/scripts/save_charts.py   # with the server running: chart backups to assets/
```

On Windows, stop any server serving `_site/` before `quarto render`, or the render fails on a file lock
(os error 32). To refresh a training module, run its notebook top to bottom from `docs/notebooks/` (needs the
engine, `XDSSAT_WORK`, `dssat-csm-data` next to this repository and `xlrd`), save it, then render. Run
`scripts/compare_districts.py --run <run folder>` first for a new run, so its district comparison exists.
`export_data.py --run <folder>` picks which run the pages call current. Details: `rebuild.qmd`.

## Publishing

`.github/workflows/quarto-publish.yml` renders this folder with Quarto 1.8.27 and deploys `_site/` to GitHub
Pages on every push to `main` that touches `docs/` (or by hand from the Actions tab). It runs nothing else: the
data files and notebook outputs it shows must be committed, so refresh them locally (see Rebuild) and commit
before pushing. One-time setup in the repository: Settings, Pages, Source: "GitHub Actions".

## OneDrive

The render output `_site/` and Quarto's cache `.quarto/` are gitignored (root `.gitignore`), but they sit
inside OneDrive and are rewritten on every render, so every render churns OneDrive sync. Render when needed,
or point `output-dir` in `_quarto.yml` at a folder outside OneDrive.

If `quarto render` stops with `os error 32` on `site_libs` or `_site`, OneDrive (or a local server) is
holding the folder it just wrote; wait a little and run it again.

## Download buttons and chart backups

`assets/js/dl.js` exports `withDownload(chart, name)`, which wraps any Plot chart with "Download SVG" and
"Download PNG" buttons; `chartImages()`, which returns every chart of a page as SVG and PNG (used by
`scripts/save_charts.py`, which writes them to `assets/training/`, `assets/java/` and `assets/xdssat/`); `cellFeatures(rows)`, which turns 0.1° cell centres into GeoJSON squares for maps; and
`adminMarks()` for the province and district lines.

## Analytics

Google Analytics 4 is on: the measurement ID is in `website: google-analytics:` in `_quarto.yml`
(Quarto adds the tag to every page). `cookie-consent: true` next to it is commented out.
