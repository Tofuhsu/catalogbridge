"""Mapping validation shared by manual, template and AI-suggested mappings.

A mapping is {target_field: source_header}. Whatever its origin, it must pass
the same checks before it can be saved or run; a schema-valid AI answer is not
automatically a correct mapping.
"""

from shared import schema


class MappingError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def check_mapping(mapping, headers, require_complete: bool) -> dict:
    """Return a cleaned mapping or raise MappingError listing every problem."""
    if not isinstance(mapping, dict):
        raise MappingError(["mapping must be an object of target -> source column."])
    problems = []
    clean = {}
    header_set = set(headers)
    used = {}
    for target, source in mapping.items():
        if target not in schema.TARGET_FIELDS:
            problems.append(f"Unknown target field '{target}'.")
            continue
        if source is None or source == "":
            continue
        if not isinstance(source, str):
            problems.append(f"Source for '{target}' must be a column name.")
            continue
        if source not in header_set:
            problems.append(f"Column '{source}' (for {target}) is not in this file.")
            continue
        if source in used:
            problems.append(f"Column '{source}' is mapped to both {used[source]} and {target}.")
            continue
        used[source] = target
        clean[target] = source
    if require_complete:
        missing = [t for t in schema.REQUIRED_TARGETS if t not in clean]
        if missing:
            problems.append("Required field(s) not mapped: " + ", ".join(missing) + ".")
    if problems:
        raise MappingError(problems)
    return clean


def missing_template_columns(template_mapping: dict, headers) -> list:
    """Columns a saved template expects that this file no longer has."""
    header_set = set(headers)
    return sorted({src for src in template_mapping.values() if src and src not in header_set})
