from datetime import date, datetime
from io import BytesIO

from PIL import Image

from app.main import MOMENT_THUMB_DIR
from app.models import Moment
from app.web import MOMENT_MEDIA_DIR


def add_moment(db, user, *, day, title, photo_name=None):
    photo_path = f"/media/moments/{photo_name}" if photo_name else None
    moment = Moment(owner_id=user.id, title=title, happened_on=day, photo_path=photo_path, created_at=datetime(2026, 9, 10, 12, 0))
    db.add(moment)
    db.commit()
    return moment


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (1600, 1200), "red").save(output, format="JPEG")
    return output.getvalue()


def test_month_calendar_groups_days_navigates_and_handles_invalid_input(client, db, make_user, login):
    user = make_user("calendar-owner")
    login(user.username)
    add_moment(db, user, day=date(2026, 9, 10), title="First")
    add_moment(db, user, day=date(2026, 9, 10), title="Second")
    add_moment(db, user, day=date(2026, 10, 1), title="October")

    september = client.get("/moments?year=2026&month=9")
    october = client.get("/moments?year=2026&month=10")
    invalid = client.get("/moments?year=2026&month=99")
    january = client.get("/moments?year=2027&month=1")

    assert september.status_code == october.status_code == invalid.status_code == january.status_code == 200
    assert "Сентябрь 2026" in september.text and 'moments-day-count">2<' in september.text
    assert "October" not in september.text
    assert "Октябрь 2026" in october.text and "October" in october.text
    assert "Январь 2027" in january.text
    assert "moments-calendar" in september.text and "moment-thumbnail" in september.text


def test_selected_day_prefills_create_and_excludes_other_dates_and_owners(client, db, make_user, login):
    alice = make_user("day-alice")
    bob = make_user("day-bob")
    add_moment(db, alice, day=date(2026, 9, 10), title="Alice one")
    add_moment(db, alice, day=date(2026, 9, 10), title="Alice two")
    add_moment(db, alice, day=date(2026, 9, 11), title="Other day")
    add_moment(db, bob, day=date(2026, 9, 10), title="Bob private")
    login(alice.username)

    page = client.get("/moments?year=2026&month=9&day=2026-09-10")
    empty = client.get("/moments?year=2026&month=9&day=2026-09-12")
    day_view = page.text.split('class="moments-day-grid"', 1)[1].split('</section>', 1)[0]

    assert "Alice one" in page.text and "Alice two" in page.text
    assert "Other day" not in day_view and "Bob private" not in page.text
    assert 'value="2026-09-10"' in page.text
    assert "На этот день моментов пока нет" in empty.text


def test_edit_moves_calendar_day_delete_removes_it_and_thumbnail_is_private_cached(client, db, make_user, login):
    alice = make_user("photo-alice")
    bob = make_user("photo-bob")
    filename = "photo-calendar.jpg"
    MOMENT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MOMENT_MEDIA_DIR / filename).write_bytes(image_bytes())
    moment = add_moment(db, alice, day=date(2026, 9, 10), title="Photo", photo_name=filename)
    login(alice.username)

    first = client.get(f"/media/moments/thumb/{filename}")
    thumbnail = MOMENT_THUMB_DIR / "photo-calendar.jpg"
    second = client.get(f"/media/moments/thumb/{filename}")
    moved = client.post(f"/moments/{moment.id}/update", data={"title": "Photo", "description": "", "happened_on": "2026-09-11"}, follow_redirects=False)
    old_day = client.get("/moments?year=2026&month=9&day=2026-09-10")
    new_day = client.get("/moments?year=2026&month=9&day=2026-09-11")

    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg") and thumbnail.is_file()
    assert (MOMENT_MEDIA_DIR / filename).is_file()  # original remains untouched
    assert moved.status_code == 303 and "Photo" not in old_day.text and "Photo" in new_day.text
    client.post("/logout")
    login(bob.username)
    assert client.get(f"/media/moments/thumb/{filename}").status_code == 404
    client.post("/logout")
    login(alice.username)
    deleted = client.post(f"/moments/{moment.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303 and not (MOMENT_MEDIA_DIR / filename).exists() and not thumbnail.exists()


def test_existing_create_flow_with_photo_generates_thumbnail_lazily(client, db, make_user, login):
    user = make_user("upload-calendar")
    login(user.username)
    created = client.post("/moments", data={"title": "Uploaded", "description": "", "happened_on": "2026-09-10"}, files={"photo_file": ("phone.jpg", BytesIO(image_bytes()), "image/jpeg")}, follow_redirects=False)
    moment = db.query(Moment).one()
    filename = moment.photo_path.rsplit("/", 1)[-1]

    assert created.status_code == 303 and moment.photo_path
    assert client.get(f"/media/moments/thumb/{filename}").status_code == 200
