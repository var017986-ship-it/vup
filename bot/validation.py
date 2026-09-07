from urllib.parse import urlparse


STYLES = {"normal", "primary", "success", "danger"}
ACTION_TYPES = {"text", "url", "submenu"}


def validate_text(value: str, label: str, maximum: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label}: значение не должно быть пустым.")
    if len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError(f"{label}: максимум {maximum} символов.")
    return value


def validate_optional_emoji(value: str) -> str:
    value = value.strip()
    if len(value.encode("utf-16-le")) // 2 > 16:
        raise ValueError("Эмодзи: максимум 16 символов.")
    return value


def validate_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ссылка должна начинаться с http:// или https://.")
    if len(value) > 2048:
        raise ValueError("Ссылка: максимум 2048 символов.")
    return value
