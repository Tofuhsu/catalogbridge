"""Fixed output schema and limits (catalogbridge.v1).

Everything in this module is frozen into a job when the run is requested, so a
job always records which schema/rule version produced its results.
"""

SCHEMA_VERSION = "catalogbridge.v1"
RULES_VERSION = "rules.v1"

MAX_FILE_BYTES = 1_048_576  # 1 MiB
MAX_DATA_ROWS = 500
PREVIEW_ROWS = 20
MAX_COLUMNS = 100
MAX_HEADER_LEN = 128

# Target fields the user maps source columns onto. currency is NOT mapped from a
# column: it is a job-level setting (no exchange-rate conversion).
TARGET_FIELDS = ["sku", "title", "price", "stock"]
REQUIRED_TARGETS = ["sku", "title", "price", "stock"]

# Output column order for the standard CSV.
OUTPUT_COLUMNS = ["sku", "title", "price", "currency", "stock", "source_row"]
ERROR_COLUMNS = ["source_row", "sku", "field", "code", "message"]

# Allowed job currencies and their number of minor-unit digits.
CURRENCIES = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "TWD": 2,
    "JPY": 0,
}

MAX_SKU_LEN = 64
MAX_TITLE_LEN = 255
MAX_STOCK = 1_000_000_000

# Job statuses (spec §7.1).
CREATED = "CREATED"
VALIDATING_UPLOAD = "VALIDATING_UPLOAD"
AWAITING_MAPPING = "AWAITING_MAPPING"
REJECTED = "REJECTED"
RUN_REQUESTED = "RUN_REQUESTED"
PROCESSING = "PROCESSING"
REPORTING = "REPORTING"
COMPLETED = "COMPLETED"
COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
NEEDS_ATTENTION = "NEEDS_ATTENTION"

TERMINAL_STATUSES = {REJECTED, COMPLETED, COMPLETED_WITH_ERRORS}
ACTIVE_RUN_STATUSES = {RUN_REQUESTED, PROCESSING, REPORTING}
