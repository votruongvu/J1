import dataclasses
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj):
        return {
            f.name: to_jsonable(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    raise TypeError(f"cannot serialize value of type {type(obj).__name__}")
