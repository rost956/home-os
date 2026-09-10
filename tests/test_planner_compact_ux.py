import re
from datetime import date

from app.models import PlannerItem


def test_planner_form_uses_compact_recurrence_disclosure_after_title(client, db, make_user, login):
    user = make_user("planner-compact")
    login(user.username)

    response = client.get("/planner?month=2026-09&day=2026-09-10")

    assert response.status_code == 200
    create_form = response.text[response.text.index('data-planner-create'):]
    assert create_form.index('name="title"') < create_form.index('planner-recurrence')
    assert create_form.index('planner-recurrence') < create_form.index('name="scheduled_for"')
    assert '<details class="planner-recurrence">' in create_form
    for field in ("recurrence_frequency", "recurrence_interval", "recurrence_until"):
        assert f'name="{field}"' in create_form
    assert 'class="planner-color-field"' in create_form
    assert 'data-planner-color-value' in create_form


def test_existing_recurring_planner_event_has_collapsed_summary(client, db, make_user, login):
    user = make_user("planner-recurring-summary")
    item = PlannerItem(
        owner_id=user.id,
        title="Monthly review",
        scheduled_for=date(2026, 9, 10),
        end_date=date(2026, 9, 10),
        recurrence_frequency="monthly",
        recurrence_interval=2,
        recurrence_until=date(2026, 12, 31),
        color="#65d6e0",
    )
    db.add(item)
    db.commit()
    login(user.username)

    response = client.get("/planner?month=2026-09&day=2026-09-10")

    assert response.status_code == 200
    assert "Каждые 2 месяца · до 31.12.2026" in re.sub(r"\s+", " ", response.text)
    assert response.text.count('class="planner-recurrence"') >= 2
