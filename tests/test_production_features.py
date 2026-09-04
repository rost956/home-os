from __future__ import annotations

import os
import sqlite3
import stat
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.main import linkify_text
from app.models import (
    ChatThread,
    ChatThreadMessage,
    ExpenseCategory,
    ExpenseItem,
    ExpenseList,
    ExpenseListShare,
    IncomeItem,
    Moment,
    PlannerItem,
    PushSubscription,
    Recipe,
    WishlistItem,
)
from app.web import RECIPE_MEDIA_DIR, RequestSizeLimitMiddleware
from scripts.sqlite_backup import create_backup, restore_backup


def test_linkify_text_escapes_url_attributes_and_surrounding_html():
    rendered = str(linkify_text('<script>alert(1)</script> https://example.test/?q=" onmouseover="alert(2)'))

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="https://example.test/?q=&#34;"' in rendered
    assert 'href="https://example.test/?q=" onmouseover=' not in rendered


def test_push_subscription_rejects_untrusted_endpoint(client, db, make_user, login):
    make_user("alice")
    login("alice")

    rejected = client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://example.test/push", "keys": {"p256dh": "key", "auth": "auth"}},
    )
    accepted = client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://fcm.googleapis.com/push/abc", "keys": {"p256dh": "key", "auth": "auth"}},
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert db.query(PushSubscription).count() == 1


def test_logout_removes_server_push_subscriptions(client, db, make_user, login):
    user = make_user("alice")
    db.add(PushSubscription(user_id=user.id, endpoint="https://fcm.googleapis.com/push/device", p256dh="key", auth="auth"))
    db.commit()
    login("alice")

    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert db.query(PushSubscription).count() == 0


def test_chunked_request_body_is_limited():
    limited_app = FastAPI()
    limited_app.add_middleware(RequestSizeLimitMiddleware, max_bytes=10)

    @limited_app.post("/consume")
    async def consume(request: Request):
        return {"size": len(await request.body())}

    def chunks():
        yield b"123456"
        yield b"789012"

    with TestClient(limited_app) as client:
        response = client.post("/consume", content=chunks())

    assert response.status_code == 413


def test_income_cannot_be_changed_by_another_user(client, db, make_user, login):
    alice = make_user("alice")
    make_user("bob")
    item = IncomeItem(owner_id=alice.id, title="Salary", amount=Decimal("1000.00"))
    db.add(item)
    db.commit()

    login("bob")
    response = client.post(f"/income/{item.id}/delete")

    assert response.status_code == 404
    assert db.get(IncomeItem, item.id) is not None


def test_moment_create_and_owner_protection(client, db, make_user, login):
    alice = make_user("alice")
    make_user("bob")
    login("alice")
    created = client.post(
        "/moments",
        data={"title": "Trip", "description": "A good day", "happened_on": "2026-09-01"},
        follow_redirects=False,
    )
    moment = db.query(Moment).one()
    client.post("/logout")
    login("bob")
    deleted = client.post(f"/moments/{moment.id}/delete")

    assert created.status_code == 303
    assert moment.owner_id == alice.id
    assert deleted.status_code == 404
    assert db.get(Moment, moment.id) is not None


def test_planner_validates_time_and_owner(client, db, make_user, login):
    alice = make_user("alice")
    make_user("bob")
    login("alice")
    invalid = client.post(
        "/planner",
        data={"title": "Meeting", "scheduled_for": "2026-09-02", "start_time": "13:00", "end_time": "12:00"},
    )
    valid = client.post(
        "/planner",
        data={"title": "Meeting", "scheduled_for": "2026-09-02", "start_time": "12:00", "end_time": "13:00"},
        follow_redirects=False,
    )
    item = db.query(PlannerItem).one()
    client.post("/logout")
    login("bob")
    toggled = client.post(f"/planner/{item.id}/toggle")

    assert invalid.status_code == 400
    assert valid.status_code == 303
    assert item.owner_id == alice.id
    assert toggled.status_code == 404


def test_chat_sync_is_incremental_and_private(client, db, make_user, login):
    alice = make_user("alice")
    bob = make_user("bob")
    make_user("eve")
    thread = ChatThread(title="Private", created_by_id=alice.id, user_a_id=alice.id, user_b_id=bob.id)
    db.add(thread)
    db.flush()
    first = ChatThreadMessage(thread_id=thread.id, sender_id=alice.id, text="one")
    second = ChatThreadMessage(thread_id=thread.id, sender_id=bob.id, text="two")
    db.add_all([first, second])
    db.commit()

    login("bob")
    synced = client.get(f"/api/chats/{thread.id}/messages", params={"after_id": first.id})
    client.post("/logout")
    login("eve")
    denied = client.get(f"/api/chats/{thread.id}/messages")

    assert synced.status_code == 200
    assert [message["id"] for message in synced.json()["messages"]] == [second.id]
    assert denied.status_code == 403


def test_read_only_expense_share_cannot_delete_through_wishlist_undo(client, db, make_user, login):
    wishlist_owner = make_user("alice")
    list_owner = make_user("bob")
    expense_list = ExpenseList(owner_id=list_owner.id, title="Shared")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name="Gifts")
    db.add(category)
    db.flush()
    expense = ExpenseItem(category_id=category.id, title="Present", amount=Decimal("100.00"))
    db.add(expense)
    db.flush()
    wishlist = WishlistItem(
        owner_id=wishlist_owner.id,
        title="Present",
        status="bought",
        is_done=True,
        expense_item_id=expense.id,
    )
    db.add(wishlist)
    db.add(ExpenseListShare(expense_list_id=expense_list.id, user_id=wishlist_owner.id, can_edit=False))
    db.commit()
    login("alice")

    response = client.post(f"/wishlist/{wishlist.id}/undo-expense", follow_redirects=False)

    assert response.status_code == 303
    assert db.get(ExpenseItem, expense.id) is not None


def test_invalid_recipe_update_keeps_existing_image(client, db, make_user, login):
    owner = make_user("cook")
    image = RECIPE_MEDIA_DIR / "test-existing-image.png"
    image.write_bytes(b"existing-image")
    recipe = Recipe(
        owner_id=owner.id,
        title="Soup",
        ingredients="Water",
        steps='[{"text":"Boil"}]',
        image_path=f"/media/recipes/{image.name}",
    )
    db.add(recipe)
    db.commit()
    login("cook")
    try:
        response = client.post(
            f"/recipes/{recipe.id}/edit",
            data={"title": "Changed", "ingredients": "Water", "remove_image": "1"},
        )
        assert response.status_code == 400
        assert image.is_file()
    finally:
        image.unlink(missing_ok=True)


def test_service_worker_does_not_force_activation(client):
    response = client.get("/service-worker.js")

    assert response.status_code == 200
    assert "skipWaiting" not in response.text
    assert "clients.claim" not in response.text
    assert "requestedUrl.origin === self.location.origin" in response.text


def test_deployment_backup_round_trip(tmp_path):
    database = tmp_path / "data" / "app.db"
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
    connection.execute("INSERT INTO sample VALUES ('before')")
    connection.commit()
    connection.close()

    archive = create_backup(database, tmp_path / "backups", retention=2, prefix="test")
    connection = sqlite3.connect(database)
    connection.execute("UPDATE sample SET value = 'after'")
    connection.commit()
    connection.close()
    restore_backup(archive, database, tmp_path / "backups")

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "before"
    finally:
        connection.close()


def test_full_backup_restores_data_files(tmp_path):
    data_dir = tmp_path / "data"
    database = data_dir / "app.db"
    upload = data_dir / "uploads" / "moments" / "photo.jpg"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"photo-before")
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
    connection.execute("INSERT INTO sample VALUES ('before')")
    connection.commit()
    connection.close()

    archive = create_backup(
        database,
        data_dir / "backups",
        prefix="full",
        data_dir=data_dir,
        include_data_files=True,
    )
    upload.write_bytes(b"photo-after")
    restore_backup(archive, database, data_dir / "backups")

    assert upload.read_bytes() == b"photo-before"
    if os.name != "nt":
        assert stat.S_IMODE(upload.stat().st_mode) == 0o600
