# F — Moments photo calendar

Moments is now a private, month-based photo calendar. The existing `Moment` model remains unchanged: one optional permanent image, date, title and description per record.

- `/moments?year=YYYY&month=M` is the deterministic month view; legacy `month=YYYY-MM` URLs continue to work. Invalid values safely open the current month.
- Calendar days use one bounded owner-scoped month query, group records in memory, and link to `day=YYYY-MM-DD`. The selected day shows all of that owner's moments in chronological/stable order.
- A selected date pre-fills the existing create form. Existing update/delete flows move/remove the record from its day naturally.
- Calendar cells use the first available image as a single thumbnail and a clear total-moment count. Empty days remain quiet.
- Full originals remain private at `/media/moments/{filename}`. Calendar thumbnails are private at `/media/moments/thumb/{filename}` and require the same owner check.
- Thumbnails are derived lazily once under `data/uploads/moments/thumbs/`, EXIF-orientation corrected, resized to fit 480×360, JPEG encoded, and reused. Original uploads are never modified. Replacing/deleting a moment photo also removes its derived thumbnail.
- The selected-day image opens a small vanilla-JS lightbox; Escape closes it. Mobile rules keep the seven-column calendar within the viewport and only show date, thumbnail and count.

No database migration was necessary. Existing moments without photos continue as text cards; one Moment still has one optional photo.

Tests cover month navigation and invalid input, grouping and owner isolation, selected-day behavior and date movement, thumbnail generation/reuse/privacy, original preservation, and create/delete behavior.

Known limitations: no multi-photo galleries, folders/albums, video processing, face recognition, GPS/EXIF timeline, collaboration, cloud sync, or image editing.
