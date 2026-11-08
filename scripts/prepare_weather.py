"""Prepare daily weather from your own gridded files into a weather input.

    python scripts/prepare_weather.py --inspect D:/era5land/t2m_max_2015.nc   # what a file holds
    python scripts/prepare_weather.py                                       # prepare > weather of configs/xdssat.yml
    python scripts/prepare_weather.py --config configs/<study>.yml --overwrite

Say in the experiment file (section prepare > weather, explained in
configs/rice-java.yml) where each variable comes from: SRAD, TMAX, TMIN and RAIN
are needed, DEWP and WIND are optional, and each can come from its own source
(rain from CHIRPS, temperature from ERA5-Land or NASA POWER ...). The files must
hold values on a regular latitude-longitude grid; sub-daily files (ERA5-Land hourly)
need  daily: max | min | mean | sum | accumulated  per variable, and utc_offset_hours
for local days. Point sources instead (points:): DSSAT .WTH station files, or CSV
tables such as NASA POWER point downloads; every grid cell takes its nearest point.
Use --inspect on a file to read off the names, units, grid and time step to put there.

The script cuts each variable to the area, converts it to DSSAT units from the
file's own units attribute (or units: in the experiment file), puts every
variable on one grid (regrid, see src/weatherprep.py), and writes the weather
input <cache>/inputs/weather/<name>/ with its manifest. Years already written are
skipped unless --overwrite. At the end it checks the result against the
requirements. Then set inputs > weather to the name, and prepare the soil for
that weather grid (scripts/prepare_soils.py --weather <name>).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import inputs  # noqa: E402
import weatherprep  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--inspect", nargs="+", type=Path, metavar="FILE", help="describe these files and stop")
    ap.add_argument("--overwrite", action="store_true", help="rewrite years that already exist")
    a = ap.parse_args()
    if a.inspect:
        for f in a.inspect:
            point = f.suffix.lower() in (".csv", ".txt", ".wth")
            print((weatherprep.inspect_table(f) if point else weatherprep.inspect(f)) + "\n")
        return
    exp = config.use(config.load(a.config))
    t0 = time.time()
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    manifest = weatherprep.prepare(exp, overwrite=a.overwrite, log=log)
    name = exp.data["prepare"]["weather"]["name"]
    log(f"wrote {manifest.parent} in {(time.time() - t0) / 60:.1f} min")
    # check the new dataset as the run would use it
    over = dict(exp.data["inputs"], weather=name)
    w = inputs.open_dataset("weather", name, exp.path("cache"))
    lat, lon, valid = w.grid()
    log(f"grid {len(lat)} x {len(lon)} cells of {w.step:g} degree, {int(valid.sum())} with data; "
        f"variables {', '.join(w.variables)}; last common day {w.last_common_date():%Y-%m-%d}")
    problems = [p for p in inputs.check(config.load(a.config, overrides={"inputs": over}))
                if "inputs > weather" in p.split("\n")[0] or p.startswith("The weather input")]
    if problems:
        print("\n" + "\n\n".join(problems))
        sys.exit(1)
    used = exp.data["inputs"]["weather"]
    print(f"\nThe weather input '{name}' meets the requirements."
          + ("" if used == name else f" To use it, set  inputs > weather: {name}  (now '{used}'), and prepare "
             f"the soil for its grid: python scripts/prepare_soils.py --weather {name}"))


if __name__ == "__main__":
    main()
