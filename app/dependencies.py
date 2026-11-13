from fastapi import HTTPException

def parse_optional_form_int(value: str | None, field_name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(
            422, f"Поле «{field_name}» должно содержать целое число."
        ) from None
