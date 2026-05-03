import re

from j1.errors.exceptions import InvalidIdentifierError

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidIdentifierError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    if not value:
        raise InvalidIdentifierError(f"{field} must not be empty")
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidIdentifierError(
            f"{field} {value!r} is invalid; must match {_IDENTIFIER_PATTERN.pattern}"
        )
    return value
