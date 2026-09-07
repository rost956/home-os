from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AIAction, Recipe
from scripts.home_ai_semantic_cases import HELD_OUT_SEMANTIC_CASES
from scripts.home_ai_semantic_regression import _seed_synthetic_db

ROOT = Path(__file__).resolve().parents[1]


def test_semantic_regression_has_exact_audit_cases_and_critical_failures():
    texts = {case.text for case in HELD_OUT_SEMANTIC_CASES}
    audit = (ROOT / "docs" / "home_ai" / "AI_VALUE_AUDIT.md").read_text(encoding="utf-8")

    assert len(HELD_OUT_SEMANTIC_CASES) == 40
    assert {case.domain for case in HELD_OUT_SEMANTIC_CASES} == {"expense", "finance", "recipe", "menu"}
    assert {
        "Хочу три дня домашней еды без курицы",
        "Разложи расходы за март 2025 по направлениям",
        "Покажи рецепт номер 12",
        "Пятого числа купил корм коту за 1200",
        "Саша вернул 500 за такси",
    }.issubset(texts)
    assert all(case.text in audit for case in HELD_OUT_SEMANTIC_CASES)


def test_held_out_phrases_are_not_copied_into_production_prompts():
    prompt_sources = "\n".join(
        (ROOT / "app" / "ai" / name).read_text(encoding="utf-8")
        for name in ("expenses.py", "finance.py", "recipes.py", "menu.py")
    )

    assert all(case.text not in prompt_sources for case in HELD_OUT_SEMANTIC_CASES)


def test_semantic_regression_mock_mode_never_requires_a_model_or_production_db():
    completed = subprocess.run(
        [sys.executable, "scripts/home_ai_semantic_regression.py", "--mock"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert '"cases": 40' in completed.stdout
    assert '"real_model_called": false' in completed.stdout
    assert '"synthetic_database": true' in completed.stdout


def test_real_model_runner_is_synthetic_and_does_not_confirm_actions():
    source = (ROOT / "scripts" / "home_ai_semantic_regression.py").read_text(encoding="utf-8")

    assert 'create_engine("sqlite://"' not in source  # multiline call avoids a production URL literal
    assert '"sqlite://"' in source
    assert "SessionLocal" not in source
    assert "confirm_action" not in source
    assert "create_expense" not in source
    assert "create_menu_proposal" in source
    assert "db.rollback()" in source


def test_real_model_runner_synthetic_fixture_has_required_recipe_id_and_no_actions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = _seed_synthetic_db(db)

        assert db.scalar(select(Recipe).where(Recipe.id == 12, Recipe.owner_id == user.id)) is not None
        assert db.scalar(select(func.count()).select_from(AIAction)) == 0
