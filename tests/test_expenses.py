from __future__ import annotations

from decimal import Decimal

from app.models import ExpenseCategory, ExpenseItem, ExpenseList, ExpenseListShare, WishlistItem


def test_expense_crud_and_analytics_flags(client, db, make_user, login):
    owner = make_user("owner")
    expense_list = ExpenseList(owner_id=owner.id, title="Дом")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name="Еда")
    db.add(category)
    db.commit()
    login("owner")

    created = client.post(
        f"/expenses/lists/{expense_list.id}/items",
        data={
            "category_id": category.id,
            "title": "Продукты",
            "amount": "1250,50",
            "expense_date": "2026-09-03",
            "exclude_from_analytics": "1",
            "exclude_from_forecast": "1",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    item = db.query(ExpenseItem).one()
    assert item.amount == Decimal("1250.50")
    assert item.include_in_analytics is False
    assert item.include_in_forecast is False

    updated = client.post(
        f"/expenses/items/{item.id}/update",
        data={
            "category_id": category.id,
            "title": "Продукты и вода",
            "amount": "1300",
            "expense_date": "2026-09-02",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db.refresh(item)
    assert item.title == "Продукты и вода"
    assert item.include_in_analytics is True
    assert item.include_in_forecast is True


def test_user_cannot_change_another_users_expense(client, db, make_user, login):
    owner = make_user("owner")
    intruder = make_user("intruder")
    expense_list = ExpenseList(owner_id=owner.id, title="Личное")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name="Другое")
    db.add(category)
    db.flush()
    item = ExpenseItem(category_id=category.id, title="Секрет", amount=Decimal("100"))
    db.add(item)
    db.commit()
    login(intruder.username)

    response = client.post(
        f"/expenses/items/{item.id}/update",
        data={"category_id": category.id, "title": "Взлом", "amount": "1", "expense_date": "2026-09-03"},
    )
    assert response.status_code == 403
    db.refresh(item)
    assert item.title == "Секрет"


def test_read_only_share_cannot_write(client, db, make_user, login):
    owner = make_user("owner")
    reader = make_user("reader")
    expense_list = ExpenseList(owner_id=owner.id, title="Общий")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name="Еда")
    db.add(category)
    db.add(ExpenseListShare(expense_list_id=expense_list.id, user_id=reader.id, can_edit=False))
    db.commit()
    login(reader.username)

    response = client.post(
        f"/expenses/lists/{expense_list.id}/items",
        data={"category_id": category.id, "title": "Нельзя", "amount": "10"},
    )
    assert response.status_code == 403


def test_deleting_expense_unlinks_wishlist(client, db, make_user, login):
    owner = make_user("owner")
    expense_list = ExpenseList(owner_id=owner.id, title="Дом")
    db.add(expense_list)
    db.flush()
    category = ExpenseCategory(expense_list_id=expense_list.id, name="Покупки")
    db.add(category)
    db.flush()
    expense = ExpenseItem(category_id=category.id, title="Кофемолка", amount=Decimal("5000"))
    db.add(expense)
    db.flush()
    wish = WishlistItem(owner_id=owner.id, title="Кофемолка", expense_item_id=expense.id)
    db.add(wish)
    db.commit()
    login(owner.username)

    response = client.post(f"/expenses/items/{expense.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    db.refresh(wish)
    assert wish.expense_item_id is None
