"""Limited offline display grammar; never guesses a platform field's unit."""
from decimal import Decimal
import re

from .models import ContractError, validate_value


def normalize_display(raw: str, unit: str):
    if not isinstance(raw, str) or len(raw) > 128:
        raise ContractError("DISPLAY_INPUT")
    result = {"status": "unknown", "decimal": None, "relation": "unknown", "lower": None, "upper": None,
              "unit": unit, "display_raw": raw, "missing_reason": "not_observed", "precision": "unknown"}
    label = raw.strip()
    if label not in {"", "-", "—"} and unit != "unknown":
        match = re.fullmatch(r"(>|>=|<|<=)?(0|[1-9]\d*)(\.\d+)?(万|秒|分钟)?", label)
        if match:
            sign, whole, fraction, suffix = match.groups()
            number = Decimal(whole + (fraction or ""))
            if suffix in {"秒", "分钟"} and unit != "second":
                match = None
            elif suffix == "万" and unit not in {"count", "yinlang", "doucoin", "contribution"}:
                match = None
            else:
                number *= Decimal(10000 if suffix == "万" else 60 if suffix == "分钟" else 1)
                relation = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", None: "eq"}[sign]
                if suffix == "万" and sign is None:
                    relation = "approx"
                result.update(status="known", relation=relation, missing_reason=None,
                              precision="rounded" if suffix == "万" else "minute" if suffix == "分钟" else "exact")
                result["lower" if relation in {"gt", "gte"} else "upper" if relation in {"lt", "lte"} else "decimal"] = format(number, "f")
        if not match:
            result.update(status="parse_failed", missing_reason="unsupported_display_grammar")
    elif unit == "unknown":
        result["missing_reason"] = "unit_unverified"
    validate_value(result)
    return result
