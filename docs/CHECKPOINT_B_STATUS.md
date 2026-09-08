# Checkpoint B — analytics drill-down and UI palette

Analytics category rows, legend entries, and chart segments open a bookmarkable
category drill-down while preserving the resolved date range. The page is
owner/share scoped, paginated, and supports date and amount sorting.

Users can now save a per-account palette on `/settings`: primary, secondary,
page and surface backgrounds, text, muted text, borders, success, warning, and
danger. Values are validated `#RRGGBB`, applied before rendering, and can be
reset to the selected theme defaults. The non-destructive SQLite schema upgrade
adds `users.ui_palette_json`.

The next checkpoint is C1: Vehicle domain, vehicle list, overview, and basic
CRUD.
