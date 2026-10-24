import pytest

from shared import csvparse, rules
from shared.csvparse import StructuralError


def test_parses_bom_quotes_commas_newlines():
    data = '﻿SKU,Name,Price,Qty\r\nA1,"Mug, blue",12.5,3\r\nA2,"Two\nlines ""quoted""",0,0\r\n'.encode()
    m = csvparse.parse_csv(data)
    assert m["hadBom"] is True
    assert m["headers"] == ["SKU", "Name", "Price", "Qty"]
    assert m["rows"][0] == {"rowNumber": 1, "values": ["A1", "Mug, blue", "12.5", "3"]}
    assert m["rows"][1]["values"][1] == 'Two\nlines "quoted"'
    assert m["rowCount"] == 2


@pytest.mark.parametrize("data,needle", [
    (b"", "empty"),
    (b"a,b\n", "no data rows"),
    (b"a,a\n1,2\n", "Duplicate header"),
    (b"a,,c\n1,2,3\n", "empty column name"),
    (b"1,2,3\n4,5,6\n", "looks like data"),
    (b"a,b\n1\n", "do not have 2 columns"),
    (b"a,b\n\xff\xfe,1\n", "not valid UTF-8"),
    (b'a,b\n"unterminated,1\n', "CSV syntax error"),
])
def test_structural_rejections(data, needle):
    with pytest.raises(StructuralError) as e:
        csvparse.parse_csv(data)
    assert needle in str(e.value)


def test_row_limit():
    data = ("a,b\n" + "x,1\n" * 501).encode()
    with pytest.raises(StructuralError, match="limit is 500"):
        csvparse.parse_csv(data)
    assert csvparse.parse_csv(("a,b\n" + "x,1\n" * 500).encode())["rowCount"] == 500


def test_case_insensitive_duplicate_header():
    with pytest.raises(StructuralError, match="Duplicate"):
        csvparse.parse_csv(b"SKU,sku\n1,2\n")


def test_header_signature_order_insensitive():
    assert csvparse.header_signature(["A", "b"]) == csvparse.header_signature(["B", "a"])


def ok_src(**kw):
    base = {"sku": "A1", "title": "Mug", "price": "12.5", "stock": "3"}
    base.update(kw)
    return base


def test_valid_row_normalizes_price_decimal():
    norm, errs = rules.validate_row(ok_src(price="12.5"), "USD")
    assert errs == [] and norm["price"] == "12.50" and norm["currency"] == "USD"
    norm, _ = rules.validate_row(ok_src(price="0.10"), "USD")
    assert norm["price"] == "0.10"  # no float drift


@pytest.mark.parametrize("src,code", [
    (ok_src(price="-1"), "NEGATIVE"),
    (ok_src(price="$5"), "BAD_FORMAT"),
    (ok_src(price="1,000"), "BAD_FORMAT"),
    (ok_src(price="1.234"), "TOO_PRECISE"),
    (ok_src(price=""), "REQUIRED"),
    (ok_src(stock="1.5"), "BAD_FORMAT"),
    (ok_src(stock="-2"), "NEGATIVE"),
    (ok_src(sku=" "), "REQUIRED"),
    (ok_src(title=None), "REQUIRED"),
])
def test_invalid_rows(src, code):
    norm, errs = rules.validate_row(src, "USD")
    assert norm is None and code in [e["code"] for e in errs]


def test_jpy_has_no_decimals():
    assert rules.validate_row(ok_src(price="100"), "JPY")[0]["price"] == "100"
    assert rules.validate_row(ok_src(price="100.5"), "JPY")[0] is None


def test_duplicate_skus_flag_later_rows_only():
    rows = [(1, {"sku": "A"}), (2, {"sku": "b"}), (3, {"sku": " a "}), (4, {"sku": ""})]
    assert rules.find_duplicate_skus(rows) == {3: 1}


def test_formula_injection_neutralized():
    assert rules.csv_safe_text("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert rules.csv_safe_text("@cmd") == "'@cmd"
    assert rules.csv_safe_text("Mug") == "Mug"


def test_retry_on_tx_conflict_retries_then_succeeds():
    from botocore.exceptions import ClientError
    from shared.ddbutil import retry_on_tx_conflict
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClientError({"Error": {"Code": "TransactionConflictException", "Message": "x"}}, "UpdateItem")
        return "ok"
    assert retry_on_tx_conflict(fn) == "ok" and calls["n"] == 3


def test_retry_on_tx_conflict_does_not_hide_other_errors():
    from botocore.exceptions import ClientError
    from shared.ddbutil import retry_on_tx_conflict

    def fn():
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")
    with pytest.raises(ClientError):
        retry_on_tx_conflict(fn)


def test_manual_all_in_one_fixture_expectations():
    """Keeps docs/manual-test-checklist.md section I in sync with the real rules."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "manual_test_all_in_one.csv")
    m = csvparse.parse_csv(open(path, "rb").read())
    mapping = {"sku": "商品編號", "title": "Product Name", "price": "售價 (USD)", "stock": "庫存"}
    projected = [(r["rowNumber"], rules.project_row(r["values"], m["headers"], mapping)) for r in m["rows"]]
    dups = rules.find_duplicate_skus(projected)
    outcome = {}
    for n, src in projected:
        norm, errs = rules.validate_row(src, "USD", dups.get(n))
        outcome[n] = "VALID" if norm else ",".join(f"{e['field']}:{e['code']}" for e in errs)
    assert m["hadBom"] and m["rowCount"] == 15
    assert [n for n, v in outcome.items() if v == "VALID"] == [1, 2, 3, 11, 12, 15]
    assert outcome[4] == "price:NEGATIVE" and outcome[5] == "sku:REQUIRED"
    assert outcome[6] == "stock:BAD_FORMAT" and outcome[7] == "sku:DUPLICATE_SKU"
    assert outcome[8] == "price:BAD_FORMAT" and outcome[9] == "price:TOO_PRECISE"
    assert outcome[10] == "title:REQUIRED" and outcome[13] == "stock:TOO_LARGE"
    assert outcome[14] == "price:BAD_FORMAT"
