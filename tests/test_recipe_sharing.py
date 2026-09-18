from __future__ import annotations

from datetime import date

from app.models import (
    MenuItem,
    Recipe,
    RecipeCollectionShare,
    RecipeCookingTimer,
    ShoppingList,
)
from app.web import RECIPE_MEDIA_DIR


def add_recipe(db, owner, title: str, *, image_path: str | None = None) -> Recipe:
    recipe = Recipe(
        owner_id=owner.id,
        title=title,
        ingredients="Вода\nСоль",
        steps='[{"text":"Приготовить"}]',
        image_path=image_path,
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe


def test_recipes_are_private_until_owner_shares_collection(
    client, db, make_user, login
):
    owner = make_user("recipe-owner")
    reader = make_user("recipe-reader")
    stranger = make_user("recipe-stranger")
    private = add_recipe(db, owner, "Секретный суп")
    strangers = add_recipe(db, stranger, "Чужой пирог")

    login(reader.username)
    page = client.get("/recipes")
    assert "Секретный суп" not in page.text
    assert "Чужой пирог" not in page.text
    assert client.get(f"/recipes/{private.id}").status_code == 404
    assert client.get(f"/recipes/{strangers.id}").status_code == 404

    login(owner.username)
    response = client.post(
        "/recipes/share",
        data={"username": reader.username},
        follow_redirects=False,
    )
    assert response.status_code == 303
    share = db.query(RecipeCollectionShare).one()
    assert (share.owner_id, share.user_id) == (owner.id, reader.id)

    # A collection share includes recipes created after access was granted.
    future = add_recipe(db, owner, "Новый семейный рецепт")
    login(reader.username)
    page = client.get("/recipes")
    assert "Секретный суп" in page.text
    assert "Новый семейный рецепт" in page.text
    assert "Чужой пирог" not in page.text
    assert f"от @{owner.username}" in page.text
    assert client.get(f"/recipes/{private.id}").status_code == 200
    assert client.get(f"/recipes/{future.id}").status_code == 200


def test_shared_recipe_is_read_only_but_supports_personal_actions(
    client, db, make_user, login
):
    owner = make_user("recipe-actions-owner")
    reader = make_user("recipe-actions-reader")
    recipe = add_recipe(db, owner, "Общий суп")
    db.add(RecipeCollectionShare(owner_id=owner.id, user_id=reader.id))
    db.commit()
    login(reader.username)

    assert client.get(f"/recipes/{recipe.id}/edit").status_code == 403
    assert client.post(f"/recipes/{recipe.id}/delete").status_code == 403
    assert client.post(f"/recipes/{recipe.id}/cost-from-prices").status_code == 403

    timer = client.post(f"/recipes/{recipe.id}/timer/start")
    assert timer.status_code == 200
    saved_timer = db.query(RecipeCookingTimer).one()
    assert saved_timer.owner_id == reader.id

    menu = client.post(
        "/menu",
        data={
            "plan_date": date(2026, 9, 20).isoformat(),
            "meal_name": "Ужин",
            "recipe_id": recipe.id,
        },
        follow_redirects=False,
    )
    assert menu.status_code == 303
    assert db.query(MenuItem).one().owner_id == reader.id

    shopping = client.post(
        f"/recipes/{recipe.id}/shopping",
        data={"servings": "", "shopping_list_id": ""},
        follow_redirects=False,
    )
    assert shopping.status_code == 303
    assert db.query(ShoppingList).one().owner_id == reader.id


def test_revoking_recipe_collection_hides_details_media_and_personal_endpoints(
    client, db, make_user, login
):
    owner = make_user("recipe-revoke-owner")
    reader = make_user("recipe-revoke-reader")
    filename = "recipe-sharing-private-test.jpg"
    image = RECIPE_MEDIA_DIR / filename
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"private recipe image")
    recipe = add_recipe(db, owner, "Рецепт с фото", image_path=f"/media/recipes/{filename}")
    share = RecipeCollectionShare(owner_id=owner.id, user_id=reader.id)
    db.add(share)
    db.commit()
    share_id = share.id
    try:
        login(reader.username)
        assert client.get(f"/media/recipes/{filename}").status_code == 200
        assert client.post(f"/recipes/{recipe.id}/timer/start").status_code == 200
        assert db.query(RecipeCookingTimer).count() == 1

        login(owner.username)
        revoked = client.post(
            f"/recipes/share/{share_id}/delete", follow_redirects=False
        )
        assert revoked.status_code == 303

        login(reader.username)
        assert "Рецепт с фото" not in client.get("/recipes").text
        assert client.get(f"/recipes/{recipe.id}").status_code == 404
        assert client.get(f"/media/recipes/{filename}").status_code == 404
        assert client.get(f"/recipes/{recipe.id}/timer/status").status_code == 404
        assert db.query(RecipeCookingTimer).count() == 0
        assert client.post(
            "/menu",
            data={
                "plan_date": "2026-09-20",
                "meal_name": "Ужин",
                "recipe_id": recipe.id,
            },
        ).status_code == 404
        assert client.post(
            f"/recipes/{recipe.id}/shopping",
            data={"servings": "", "shopping_list_id": ""},
        ).status_code == 404
    finally:
        image.unlink(missing_ok=True)


def test_recipe_collection_share_is_unique_and_owner_controlled(
    client, db, make_user, login
):
    owner = make_user("recipe-share-owner")
    reader = make_user("recipe-share-reader")
    another_owner = make_user("recipe-share-another")

    login(owner.username)
    client.post("/recipes/share", data={"username": reader.username})
    client.post("/recipes/share", data={"username": reader.username})
    client.post("/recipes/share", data={"username": owner.username})
    assert db.query(RecipeCollectionShare).count() == 1
    share = db.query(RecipeCollectionShare).one()

    login(another_owner.username)
    assert client.post(
        f"/recipes/share/{share.id}/delete", follow_redirects=False
    ).status_code == 404
    assert db.get(RecipeCollectionShare, share.id) is not None
