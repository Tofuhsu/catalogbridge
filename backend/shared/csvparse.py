"""Structural CSV validation and canonical manifest building.

Structural problems (encoding, empty file, missing/duplicate headers, ragged
rows, too many rows) reject the whole file before anything is dispatched.
Row-level business problems (bad price, missing SKU) are NOT handled here; they
become INVALID results later so the user can fix them.
"""

import csv
import hashlib
import io
import re

from shared import schema

BOM = b"\xef\xbb\xbf"
_NUMERIC = re.compile(r"^[\s\-+]?\d+([.,]\d+)?\s*$")
_MAX_LISTED = 10


class StructuralError(Exception):
    """The file cannot become a job. `reasons` is shown to the user verbatim."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def parse_csv(data: bytes) -> dict:
    """Parse bytes into a canonical manifest dict or raise StructuralError.

    rowNumber is the 1-based index of the data record (header excluded). It is
    stable for a given file version, which is what makes it usable in the
    business dedup key.
    """
    if len(data) == 0:
        raise StructuralError(["File is empty."])
    if len(data) > schema.MAX_FILE_BYTES:
        raise StructuralError([f"File is larger than {schema.MAX_FILE_BYTES} bytes."])

    had_bom = data.startswith(BOM)
    if had_bom:
        data = data[len(BOM):]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise StructuralError([f"File is not valid UTF-8 (byte offset {e.start})."])
    if "\x00" in text:
        raise StructuralError(["File contains NUL bytes; it does not look like a CSV text file."])

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as e:
        raise StructuralError([f"CSV syntax error: {e}."])

    # Drop fully blank records (e.g. trailing newline lines) but keep numbering
    # based on the remaining records only.
    records = [r for r in records if any(cell.strip() for cell in r)]
    if not records:
        raise StructuralError(["File has no header row."])

    header = [h.strip() for h in records[0]]
    reasons = []
    if len(header) > schema.MAX_COLUMNS:
        reasons.append(f"File has {len(header)} columns; the limit is {schema.MAX_COLUMNS}.")
    long_names = [h[:20] + "..." for h in header if len(h) > schema.MAX_HEADER_LEN]
    if long_names:
        reasons.append(f"Header name(s) longer than {schema.MAX_HEADER_LEN} characters: " + ", ".join(long_names[:5]) + ".")
    empty_cols = [str(i + 1) for i, h in enumerate(header) if h == ""]
    if empty_cols:
        reasons.append("Header has empty column name(s) at position " + ", ".join(empty_cols) + ".")
    seen = {}
    dups = []
    for h in header:
        key = h.casefold()
        if h and key in seen and h not in dups:
            dups.append(h)
        seen[key] = True
    if dups:
        reasons.append("Duplicate header name(s): " + ", ".join(dups) + ".")
    non_empty = [h for h in header if h]
    if non_empty and all(_NUMERIC.match(h) for h in non_empty):
        reasons.append("First row looks like data, not a header row.")

    rows = records[1:]
    if not rows:
        reasons.append("File has a header but no data rows.")
    if len(rows) > schema.MAX_DATA_ROWS:
        reasons.append(f"File has {len(rows)} data rows; the limit is {schema.MAX_DATA_ROWS}.")

    ragged = [i + 1 for i, r in enumerate(rows) if len(r) != len(header)]
    if ragged:
        listed = ", ".join(str(n) for n in ragged[:_MAX_LISTED])
        more = f" (and {len(ragged) - _MAX_LISTED} more)" if len(ragged) > _MAX_LISTED else ""
        reasons.append(
            f"{len(ragged)} data row(s) do not have {len(header)} columns: rows {listed}{more}."
        )
    if reasons:
        raise StructuralError(reasons)

    return {
        "headers": header,
        "rows": [{"rowNumber": i + 1, "values": r} for i, r in enumerate(rows)],
        "rowCount": len(rows),
        "hadBom": had_bom,
        "sourceSignature": header_signature(header),
    }


def header_signature(headers) -> str:
    """Order-insensitive fingerprint of a header set (for mapping templates)."""
    norm = sorted(h.strip().casefold() for h in headers)
    return hashlib.sha256("\x1f".join(norm).encode()).hexdigest()[:16]
