from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal

from app.models import (
    ChatThread,
    ChatThreadMessage,
    MenuItem,
    Recipe,
    RecipeCookingTimer,
    ShoppingItem,
    ShoppingList,
    ShoppingListShare,
    ShoppingPriceHistory,
    WishlistItem,
)


def test_recipe_create_rejects_fake_image(client, db, make_user, login):
    make_user("cook")
    login("cook")
    response = client.post(
        "/recipes/new",
        data={"title": "Суп", "ingredients": "Вода", "step_text": "Варить", "step_minutes": "10"},
        files={"image_file": ("photo.jpg", io.BytesIO(b"not-a-jpeg"), "image/jpeg")},
    )
    assert response.status_code == 400
    assert db.query(Recipe).count() == 0


def test_recipe_delete_cleans_plans_and_timers(client, db, make_user, login):
    owner = make_user("cook")
    recipe = Recipe(owner_id=owner.id, title="Суп", ingredients="Вода", steps='[{"text":"Варить"}]')
    db.add(recipe)
    db.flush()
    db.add(MenuItem(owner_id=owner.id, recipe_id=recipe.id, plan_date=datetime(2026, 9, 3), meal_name="Обед"))
    db.add(RecipeCookingTimer(owner_id=owner.id, recipe_id=recipe.id))
    db.commit()
    login(owner.username)

    response = client.post(f"/recipes/{recipe.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert db.query(MenuItem).count() == 0
    assert db.query(RecipeCookingTimer).count() == 0


def test_read_only_shopping_share_cannot_mutate(client, db, make_user, login):
    owner = make_user("owner")
    reader = make_user("reader")
    shopping_list = ShoppingList(owner_id=owner.id, title="Магазин")
    db.add(shopping_list)
    db.flush()
    item = ShoppingItem(shopping_list_id=shopping_list.id, title="Молоко", department="Молочка")
    db.add(item)
    db.add(ShoppingListShare(shopping_list_id=shopping_list.id, user_id=reader.id, can_edit=False))
    db.commit()
    login(reader.username)

    moved = client.post(f"/shopping/items/{item.id}/department", data={"department": "Другое"})
    deleted = client.post(f"/shopping/items/{item.id}/delete")
    assert moved.status_code == 403
    assert deleted.status_code == 403
    assert db.get(ShoppingItem, item.id) is not None


def test_shopping_delete_preserves_price_history(client, db, make_user, login):
    owner = make_user("owner")
    shopping_list = ShoppingList(owner_id=owner.id, title="Магазин")
    db.add(shopping_list)
    db.flush()
    item = ShoppingItem(shopping_list_id=shopping_list.id, title="Молоко", department="Молочка")
    db.add(item)
    db.flush()
    history = ShoppingPriceHistory(
        owner_id=owner.id,
        shopping_item_id=item.id,
        title=item.title,
        normalized_title="молоко",
        department=item.department,
        price=Decimal("99.90"),
    )
    db.add(history)
    db.commit()
    login(owner.username)

    client.post(f"/shopping/items/{item.id}/delete")
    db.refresh(history)
    assert history.shopping_item_id is None


def test_wishlist_rejects_unsafe_url_and_negative_price(client, db, make_user, login):
    make_user("alice")
    login("alice")
    unsafe = client.post("/wishlist", data={"title": "Подарок", "url": "javascript:alert(1)"})
    negative = client.post("/wishlist", data={"title": "Подарок", "price": "-10"})
    assert unsafe.status_code == 400
    assert negative.status_code == 400
    assert db.query(WishlistItem).count() == 0


def test_chat_reply_cannot_reference_another_thread(client, db, make_user, login, monkeypatch):
    alice = make_user("alice")
    bob = make_user("bob")
    first = ChatThread(title="Первый", created_by_id=alice.id, user_a_id=alice.id, user_b_id=bob.id)
    second = ChatThread(title="Второй", created_by_id=alice.id, user_a_id=alice.id, user_b_id=bob.id)
    db.add_all([first, second])
    db.flush()
    foreign_message = ChatThreadMessage(thread_id=second.id, sender_id=bob.id, text="Не показывать")
    db.add(foreign_message)
    db.commit()
    monkeypatch.setattr("app.main.send_push_to_user", lambda *args, **kwargs: {"ok": True})
    login(alice.username)

    response = client.post(
        f"/chats/{first.id}",
        data={"text": "Ответ", "reply_to_id": str(foreign_message.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    sent = db.query(ChatThreadMessage).filter(ChatThreadMessage.thread_id == first.id).one()
    assert sent.reply_to_id is None
