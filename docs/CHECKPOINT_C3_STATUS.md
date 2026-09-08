# Checkpoint C3 — Vehicle maintenance tracker

- `VehicleMaintenanceItem` tracks mileage and/or calendar intervals per vehicle.
- Statuses are calculated centrally: OK, soon, overdue; month arithmetic handles month ends and leap years.
- Marking service updates the tracker atomically, never lowers the vehicle odometer, and can create a maintenance journal entry.
- Existing SQLite databases receive the new table via the runtime schema mechanism.
- Next checkpoint: C4 fuel log.
