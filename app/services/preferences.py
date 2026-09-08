from __future__ import annotations

import json
import re

PALETTE_TOKENS = (
    ("primary", "Основной акцент"),
    ("secondary", "Второй акцент"),
    ("bg", "Фон страницы"),
    ("surface", "Фон карточек"),
    ("text", "Основной текст"),
    ("muted", "Вторичный текст"),
    ("border", "Границы"),
    ("success", "Успех"),
    ("warning", "Предупреждение"),
    ("danger", "Ошибка"),
)
PALETTE_KEYS = {key for key, _label in PALETTE_TOKENS}
PALETTE_GROUPS = (
    ("Основные", ("primary", "secondary", "bg", "surface", "text", "muted")),
    ("Состояния и границы", ("success", "warning", "danger", "border")),
)
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
GRADIENT_KEYS = {"gradient_enabled", "gradient_start_color", "gradient_end_color", "gradient_angle"}

DEFAULT_PALETTES = {
    "light": {
        "primary": "#2563eb", "secondary": "#7c3aed", "bg": "#f6f7f9", "surface": "#ffffff",
        "text": "#111827", "muted": "#6b7280", "border": "#e5e7eb", "success": "#16a34a",
        "warning": "#d97706", "danger": "#dc2626",
    },
    "dark": {
        "primary": "#3b82f6", "secondary": "#a78bfa", "bg": "#0b1120", "surface": "#111827",
        "text": "#e5e7eb", "muted": "#94a3b8", "border": "#263244", "success": "#4ade80",
        "warning": "#fbbf24", "danger": "#f87171",
    },
}


def default_palette(theme: str) -> dict[str, str]:
    return dict(DEFAULT_PALETTES["dark" if theme == "dark" else "light"])


def load_palette(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {key: value.lower() for key, value in values.items() if key in PALETTE_KEYS and isinstance(value, str) and HEX_COLOR.fullmatch(value)}


def load_gradient(raw: str | None) -> dict[str, str | int | bool]:
    try:
        values = json.loads(raw or "{}")
    except (TypeError, ValueError):
        values = {}
    if not isinstance(values, dict):
        return {}
    start, end = values.get("gradient_start_color"), values.get("gradient_end_color")
    if not (isinstance(start, str) and isinstance(end, str) and HEX_COLOR.fullmatch(start) and HEX_COLOR.fullmatch(end)):
        return {}
    angle = values.get("gradient_angle", 135)
    if isinstance(angle, bool) or not isinstance(angle, int) or not 0 <= angle <= 360:
        return {}
    return {"gradient_enabled": values.get("gradient_enabled") is True, "gradient_start_color": start.lower(), "gradient_end_color": end.lower(), "gradient_angle": angle}


def relative_luminance(value: str) -> float:
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    normalized = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
    return .2126 * normalized[0] + .7152 * normalized[1] + .0722 * normalized[2]


def contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


def contrast_text_color(background: str) -> str:
    """Return the button foreground with the best contrast against its background."""
    return "#ffffff" if contrast_ratio("#ffffff", background) >= contrast_ratio("#111827", background) else "#111827"


def validate_palette(values: dict[str, str], *, theme: str) -> tuple[dict[str, str], str | None]:
    palette: dict[str, str] = {}
    for key, value in values.items():
        if key not in PALETTE_KEYS or not isinstance(value, str) or not HEX_COLOR.fullmatch(value.strip()):
            return {}, "Используйте цвета только в формате #RRGGBB."
        palette[key] = value.strip().lower()
    effective = default_palette(theme)
    effective.update(palette)
    critical_pairs = (
        ("text", "bg", "Основной текст плохо читается на фоне страницы."),
        ("text", "surface", "Основной текст плохо читается на фоне карточек."),
        ("muted", "surface", "Вторичный текст плохо читается на фоне карточек."),
    )
    for foreground, background, message in critical_pairs:
        if contrast_ratio(effective[foreground], effective[background]) < 4.5:
            return {}, message
    return palette, None


def palette_css_variables(palette: dict[str, str]) -> str:
    variables: dict[str, str] = {}
    for key, value in palette.items():
        variables[key] = value
        if key == "surface":
            variables["card"] = value
        elif key == "border":
            variables["line"] = value
        elif key == "primary":
            variables["primary-foreground"] = contrast_text_color(value)
    return ";".join(f"--{key}:{value}" for key, value in variables.items())


def gradient_css_variables(gradient: dict[str, str | int | bool]) -> str:
    if not gradient or not gradient.get("gradient_enabled"):
        return "--accent-background:var(--primary)"
    start, end, angle = gradient["gradient_start_color"], gradient["gradient_end_color"], gradient["gradient_angle"]
    foreground = contrast_text_color(str(start)) if contrast_text_color(str(start)) == contrast_text_color(str(end)) else "#ffffff"
    return f"--accent-background:linear-gradient({angle}deg,{start},{end});--primary-foreground:{foreground}"


def validate_gradient(enabled: bool, start: str, end: str, angle: str) -> tuple[dict[str, str | int | bool], str | None]:
    if not enabled:
        return {}, None
    if not HEX_COLOR.fullmatch(start.strip()) or not HEX_COLOR.fullmatch(end.strip()):
        return {}, "Используйте цвета градиента только в формате #RRGGBB."
    try:
        numeric_angle = int(angle)
    except ValueError:
        return {}, "Угол градиента должен быть числом от 0 до 360."
    if not 0 <= numeric_angle <= 360:
        return {}, "Угол градиента должен быть от 0 до 360."
    return {"gradient_enabled": True, "gradient_start_color": start.lower(), "gradient_end_color": end.lower(), "gradient_angle": numeric_angle}, None
