"""Row-level business validation and normalization (rules.v2: stock optional).

A row that fails these rules becomes an INVALID result. That is a completed
outcome, never a retry: retrying cannot make a negative price valid.
"""

import re
from decimal import Decimal, InvalidOperation

from shared import schema

_PRICE = re.compile(r"^-?\d+(\.\d+)?$")
_INT = re.compile(r"^-?\d+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _err(field, code, message):
    return {"field": field, "code": code, "message": message}


def project_row(values, headers, mapping) -> dict:
    """Pick the mapped source cells for one row. mapping: target -> source header."""
    index = {h: i for i, h in enumerate(headers)}
    out = {}
    for target in schema.TARGET_FIELDS:
        src = mapping.get(target)
        out[target] = values[index[src]] if src is not None and src in index else None
    return out


def validate_row(source: dict, currency: str, duplicate_of=None):
    """Return (normalized_or_None, errors). Never raises for bad user data."""
    errors = []
    norm = {}

    sku = (source.get("sku") or "").strip()
    if not sku:
        errors.append(_err("sku", "REQUIRED", "SKU is required."))
    elif len(sku) > schema.MAX_SKU_LEN:
        errors.append(_err("sku", "TOO_LONG", f"SKU is longer than {schema.MAX_SKU_LEN} characters."))
    elif _CONTROL.search(sku) or "\n" in sku or "\r" in sku:
        errors.append(_err("sku", "BAD_CHARACTERS", "SKU contains control characters or line breaks."))
    norm["sku"] = sku

    title = (source.get("title") or "").strip()
    if not title:
        errors.append(_err("title", "REQUIRED", "Title is required."))
    elif len(title) > schema.MAX_TITLE_LEN:
        errors.append(_err("title", "TOO_LONG", f"Title is longer than {schema.MAX_TITLE_LEN} characters."))
    elif _CONTROL.search(title):
        errors.append(_err("title", "BAD_CHARACTERS", "Title contains control characters."))
    norm["title"] = title

    raw_price = (source.get("price") or "").strip()
    digits = schema.CURRENCIES[currency]
    if not raw_price:
        errors.append(_err("price", "REQUIRED", "Price is required."))
    elif not _PRICE.match(raw_price):
        errors.append(_err("price", "BAD_FORMAT", "Price must be a plain decimal number like 12.50 (no symbols or thousands separators)."))
    else:
        try:
            price = Decimal(raw_price)
        except InvalidOperation:
            price = None
            errors.append(_err("price", "BAD_FORMAT", "Price is not a number."))
        if price is not None:
            q = Decimal(1).scaleb(-digits)
            if price < 0:
                errors.append(_err("price", "NEGATIVE", "Price must not be negative."))
            elif price != price.quantize(q):
                errors.append(_err("price", "TOO_PRECISE", f"{currency} prices allow at most {digits} decimal place(s)."))
            elif price >= Decimal("1e12"):
                errors.append(_err("price", "TOO_LARGE", "Price is unrealistically large."))
            else:
                norm["price"] = str(price.quantize(q))

    raw_stock = (source.get("stock") or "").strip()
    if not raw_stock:
        norm["stock"] = ""  # optional since rules.v2: not tracked by this supplier
    elif not _INT.match(raw_stock):
        errors.append(_err("stock", "BAD_FORMAT", "Stock must be a whole number."))
    else:
        stock = int(raw_stock)
        if stock < 0:
            errors.append(_err("stock", "NEGATIVE", "Stock must not be negative."))
        elif stock > schema.MAX_STOCK:
            errors.append(_err("stock", "TOO_LARGE", f"Stock must be at most {schema.MAX_STOCK}."))
        else:
            norm["stock"] = str(stock)

    if duplicate_of is not None:
        errors.append(_err("sku", "DUPLICATE_SKU", f"SKU already appears in row {duplicate_of}."))

    norm["currency"] = currency
    if errors:
        return None, errors
    return norm, []


def find_duplicate_skus(projected_rows) -> dict:
    """Map rowNumber -> first rowNumber with the same SKU (later occurrences only).

    Comparison is trimmed and case-insensitive. Computed once from the frozen
    manifest + mapping at dispatch time, so it is deterministic across retries.
    """
    first = {}
    dup = {}
    for row_number, src in projected_rows:
        key = (src.get("sku") or "").strip().casefold()
        if not key:
            continue
        if key in first:
            dup[row_number] = first[key]
        else:
            first[key] = row_number
    return dup


def csv_safe_text(value: str) -> str:
    """Neutralize spreadsheet formula injection for text cells.

    Text that starts with = + - @ TAB or CR is prefixed with a single quote so a
    spreadsheet shows it as text instead of evaluating it. Numeric columns are
    produced from validated numbers and are never prefixed.
    """
    if value and value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value
