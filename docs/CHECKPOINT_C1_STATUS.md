# Checkpoint C1 — Vehicle domain

Home OS now has a user-owned `Vehicle` model with nickname, make/model, year,
optional plate and VIN, odometer, notes, and timestamps. `/vehicles` provides
the list, creation, overview, editing, and destructive deletion flows; all
vehicle routes are owner-scoped.

The runtime SQLite schema upgrade creates the non-destructive `vehicles` table.
The model is ready for future vehicle-bound logbook, maintenance, and fuel
records without creating those domains prematurely.

The next checkpoint is C2: vehicle logbook CRUD, filters/sorting, and printable
logbook.
