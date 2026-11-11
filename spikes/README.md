# Spikes

Small experiments behind numbers quoted in the documentation (engine build, speed of
chunked experiment files, checks on Java cells). Not production code.

All runs: 2026-11-05, Windows 11, 64 logical cores, DSSAT-CSM v4.8.6.4 built from
`../dssat-csm-os` (commit `25c93b9a`), GNU Fortran 13.2.0 (Strawberry MinGW-w64), CMake 3.29.2.

## 1. Build the engine (about 67 s)

Git Bash. Build outside OneDrive.

```bash
B=C:/TEMP/dsn-build
mkdir -p $B/src $B/build
git -C ../../dssat-csm-os archive --format=tar HEAD | tar -x -C $B/src
cd $B/build
cmake -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=RELEASE ../src
mingw32-make -j8          # -> bin/dscsm048.exe
```

`cmake --install` fails on Windows: `CMakeLists.txt` installs
`Utilities/run_dssat` unconditionally, but that file is only generated in the
non-WIN32 branch. Copy the files by hand instead:

```bash
D=C:/TEMP/dsn/dssat
mkdir -p $D && cp -r $B/src/Data/. $D/ && cp $B/build/bin/dscsm048.exe $D/
cp -r ../../dssat-csm-data/Soil $D/
mkdir -p $D/Weather && cp ../../dssat-csm-data/Weather/UFGA82*.WTH $D/Weather/
# Rename DSSATPRO.v48 -> DSSATPRO.V48 and replace "C: \DSSAT48" with "C: \TEMP\dsn\dssat"
```

Keep the install path short. The engine stores paths in fixed-length Fortran
`CHARACTER` buffers (see the review, section 4).

## 2. Single experiment

```bash
mkdir -p C:/TEMP/dsn/run1 && cp ../../dssat-csm-data/Maize/UFGA8201.MZX C:/TEMP/dsn/run1/
cd C:/TEMP/dsn/run1 && ../dssat/dscsm048.exe A UFGA8201.MZX < /dev/null
```

Result: 6 treatments, 1.6 s wall-clock, 27 output files.

For lean output, copy `DSCSM048.CTR` into the run directory, change the
`@CTRNO` line from `00` to `08` (CSV, Summary only), and pass the file as the
third argument. 20 runs of 6 treatments took 19.1 s.

## 3. Many cells in one process

```bash
python spikes/chunk_filex.py ../dssat-csm-data/Maize/UFGA8201.MZX \
    ../dssat-csm-data/Weather/UFGA8201.WTH C:/TEMP/dsn/chunk 500
```

Then run the engine as in step 2.

| cells per FileX | wall-clock (s) | s per cell |
|---:|---:|---:|
| 50 | 9.0 | 0.18 |
| 200 | 41.1 | 0.21 |
| 500 | 122.1 | 0.24 |

Running 8 chunks of 50 cells as parallel processes, each in its own directory,
took 7.5 s in total.
