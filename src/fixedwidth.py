"""Column layout of DSSAT header-driven tables.

Many DSSAT tables (weather records, soil layers, the soil and weather station
lines) are read by ``PARSE_HEADERS`` (dssat-csm-os ``Utilities/READS.for:997``):

* a header line starts with ``@`` and lists column names separated by spaces;
* the first column spans from character 1 to the end of its name;
* every later column spans from two characters after the previous name ends
  (the single space right after a name is a separator) to the end of its own
  name;
* the value inside a span is read list-directed (``READ(LINE(C1:C2),*)``), so
  it only has to lie inside the span. We right-align numbers so the output
  looks like the files DSSAT itself writes.

Columns are numbered from 1 here, as in the Fortran source.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str    # header name without trailing dots
    start: int   # first character of the span, 1-based, inclusive
    end: int     # last character of the span, 1-based, inclusive
    token_start: int  # first character of the name in the header line

    @property
    def width(self) -> int:
        return self.end - self.start + 1


def parse_header(header: str) -> list[Column]:
    """Replicate ``PARSE_HEADERS``: spans of each column in ``header``."""
    line = header.rstrip()
    if "!" in line[1:]:
        line = line[: line.index("!", 1)]
    cols: list[Column] = []
    n = len(line)
    i = 1  # 0-based index; character 1 (the '@') belongs to the first column
    start = 1
    tok_start = None
    in_space = True
    while i < n:
        ch = line[i]
        if ch == " ":
            if not in_space:
                cols.append(_column(line, start, i, tok_start))
                start = i + 2  # 1-based column after the separating space
                in_space = True
                tok_start = None
        else:
            if in_space:
                tok_start = i + 1
            in_space = False
        i += 1
    if not in_space:
        cols.append(_column(line, start, n, tok_start))
    return cols


def _column(line: str, start: int, end: int, tok_start: int | None) -> Column:
    if start == 1:
        text = line[1:end]  # Fortran: HEADER(1) = ADJUSTL(CHAR(2:COL(1,2)))
    else:
        text = line[start - 1 : end]
    name = text.strip().rstrip(". ")
    return Column(name=name, start=start, end=end, token_start=tok_start or start)


def format_value(value, width: int, fmt: str | None = None, left: bool = False) -> str:
    """Format one value to exactly ``width`` characters, or raise."""
    if value is None or (isinstance(value, float) and value != value):
        s = "-99"
    elif isinstance(value, str):
        s = value
    elif fmt is not None:
        s = format(value, fmt)
        # Too wide with the requested decimals: drop decimals until it fits
        # (12.345 in a 5-wide '.3f' column becomes '12.35').
        if len(s) > width and fmt.endswith("f"):
            for nd in range(int(fmt[1:-1] or 0) - 1, -1, -1):
                s = format(value, f".{nd}f")
                if len(s) <= width:
                    break
    else:
        s = str(value)
    if s in ("-99.0", "-99.00", "-99.000"):
        s = "-99"
    if len(s) > width:
        raise ValueError(f"value {s!r} does not fit in {width} characters")
    return s.ljust(width) if left else s.rjust(width)


def format_row(cols: list[Column], values: dict, fmts: dict | None = None,
               left: set | frozenset = frozenset()) -> str:
    """Build one data line for a header-driven table.

    ``values`` maps column name to value; missing columns are written as -99.
    ``fmts`` maps column name to a format spec such as ``'.1f'``.
    """
    fmts = fmts or {}
    out = []
    pos = 1
    for c in cols:
        if c.start > pos:
            out.append(" " * (c.start - pos))
        v = values.get(c.name, -99)
        out.append(format_value(v, c.width, fmts.get(c.name), left=c.name in left))
        pos = c.end + 1
    return "".join(out).rstrip()
