import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    theme: Mapped[str] = mapped_column(String(20), default="system", nullable=False)
    expense_period_start_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ui_palette_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    recipes: Mapped[list["Recipe"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    owned_expense_lists: Mapped[list["ExpenseList"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    wishlist_items: Mapped[list["WishlistItem"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    menu_items: Mapped[list["MenuItem"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    shopping_lists: Mapped[list["ShoppingList"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    shopping_category_rules: Mapped[list["ShoppingCategoryRule"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    push_subscriptions: Mapped[list["PushSubscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    cooking_timers: Mapped[list["RecipeCookingTimer"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    moments: Mapped[list["Moment"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    planner_items: Mapped[list["PlannerItem"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    vehicles: Mapped[list["Vehicle"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    ai_settings: Mapped["AIUserSettings | None"] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    ai_actions: Mapped[list["AIAction"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    temporary_file_transfers: Mapped[list["TemporaryFileTransfer"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class TemporaryFileTransfer(Base):
    __tablename__ = "temporary_file_transfers"
    __table_args__ = (Index("ix_temporary_file_transfers_owner_expires", "owner_id", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="temporary_file_transfers")
    files: Mapped[list["TemporarySharedFile"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="TemporarySharedFile.id"
    )

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


class TemporarySharedFile(Base):
    __tablename__ = "temporary_shared_files"
    __table_args__ = (Index("ix_temporary_shared_files_transfer", "transfer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("temporary_file_transfers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)

    transfer: Mapped[TemporaryFileTransfer] = relationship(back_populates="files")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    make: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    license_plate: Mapped[str | None] = mapped_column(String(40), nullable=True)
    vin: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_odometer: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False)

    owner: Mapped[User] = relationship(back_populates="vehicles")
    log_entries: Mapped[list["VehicleLogEntry"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )
    maintenance_items: Mapped[list["VehicleMaintenanceItem"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )
    fuel_entries: Mapped[list["VehicleFuelEntry"]] = relationship(back_populates="vehicle", cascade="all, delete-orphan")

    @property
    def title(self) -> str:
        return self.display_name or f"{self.make} {self.model}"


class VehicleLogEntry(Base):
    __tablename__ = "vehicle_log_entries"
    __table_args__ = (Index("ix_vehicle_log_entries_vehicle_occurred_on", "vehicle_id", "occurred_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    odometer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    service_location: Mapped[str | None] = mapped_column(String(180), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False
    )

    vehicle: Mapped[Vehicle] = relationship(back_populates="log_entries")


class VehicleMaintenanceItem(Base):
    __tablename__ = "vehicle_maintenance_items"
    __table_args__ = (Index("ix_vehicle_maintenance_items_vehicle_category", "vehicle_id", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_service_odometer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False)

    vehicle: Mapped[Vehicle] = relationship(back_populates="maintenance_items")


class VehicleFuelEntry(Base):
    __tablename__ = "vehicle_fuel_entries"
    __table_args__ = (Index("ix_vehicle_fuel_entries_vehicle_occurred_on", "vehicle_id", "occurred_on"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    odometer: Mapped[int] = mapped_column(Integer, nullable=False)
    liters: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_per_liter: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    full_tank: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fuel_station: Mapped[str | None] = mapped_column(String(180), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False)
    vehicle: Mapped[Vehicle] = relationship(back_populates="fuel_entries")


class AIUserSettings(Base):
    __tablename__ = "ai_user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_general: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_finance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_recipes: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_menu: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_planner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_wishlist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_chat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_today: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="ai_settings")


class AIAction(Base):
    __tablename__ = "ai_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired', 'failed')",
            name="ck_ai_action_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    public_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    proposed_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    owner: Mapped[User] = relationship(back_populates="ai_actions")


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tags: Mapped[str | None] = mapped_column(String(250), nullable=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    ingredients: Mapped[str] = mapped_column(Text, nullable=False)
    cook_time_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    servings: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    steps: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="recipes")

    @property
    def cost_per_serving(self) -> Decimal | None:
        if self.cost is None or not self.servings:
            return None
        servings_value = Decimal(str(self.servings))
        if servings_value <= 0:
            return None
        return (self.cost / servings_value).quantize(Decimal("0.01"))




class RecipeCookingTimer(Base):
    __tablename__ = "recipe_cooking_timers"
    __table_args__ = (UniqueConstraint("owner_id", "recipe_id", name="uq_recipe_cooking_timer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), nullable=False, index=True)
    elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="cooking_timers")
    recipe: Mapped[Recipe] = relationship()


class ShoppingPriceHistory(Base):
    __tablename__ = "shopping_price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    shopping_item_id: Mapped[int | None] = mapped_column(ForeignKey("shopping_items.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    normalized_title: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    amount: Mapped[str | None] = mapped_column(String(120), nullable=True)
    department: Mapped[str | None] = mapped_column(String(80), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, index=True)

    owner: Mapped[User] = relationship()
    shopping_item: Mapped["ShoppingItem | None"] = relationship()


class WatchItem(Base):
    __tablename__ = "watch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="movie", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="planned", nullable=False, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    watch_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive, index=True)

    owner: Mapped[User] = relationship()


class Moment(Base):
    __tablename__ = "moments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    happened_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    photo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="moments")


class PlannerItem(Base):
    __tablename__ = "planner_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recurrence_frequency: Mapped[str | None] = mapped_column(String(12), nullable=True)
    recurrence_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recurrence_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    color: Mapped[str] = mapped_column(String(7), default="#2563eb", nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="planner_items")
    reminders: Mapped[list["PlannerReminder"]] = relationship(
        back_populates="planner_item",
        cascade="all, delete-orphan",
        order_by="PlannerReminder.id",
    )


class PlannerReminder(Base):
    __tablename__ = "planner_reminders"
    __table_args__ = (
        UniqueConstraint(
            "planner_item_id",
            "offset_value",
            "offset_unit",
            "relation",
            name="uq_planner_reminder_config",
        ),
        CheckConstraint("offset_value >= 0", name="ck_planner_reminder_offset_nonnegative"),
        CheckConstraint("offset_unit IN ('minutes', 'hours', 'days')", name="ck_planner_reminder_unit"),
        CheckConstraint("relation = 'before_start'", name="ck_planner_reminder_relation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    planner_item_id: Mapped[int] = mapped_column(
        ForeignKey("planner_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    offset_value: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_unit: Mapped[str] = mapped_column(String(10), nullable=False)
    relation: Mapped[str] = mapped_column(String(20), nullable=False, default="before_start")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    planner_item: Mapped[PlannerItem] = relationship(back_populates="reminders")
    deliveries: Mapped[list["PlannerReminderDelivery"]] = relationship(
        back_populates="planner_reminder",
        cascade="all, delete-orphan",
    )


class ExpenseList(Base):
    __tablename__ = "expense_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="owned_expense_lists")
    categories: Mapped[list["ExpenseCategory"]] = relationship(back_populates="expense_list", cascade="all, delete-orphan")
    shares: Mapped[list["ExpenseListShare"]] = relationship(back_populates="expense_list", cascade="all, delete-orphan")

    @property
    def total(self) -> Decimal:
        return sum((category.total for category in self.categories), Decimal("0.00"))


class ExpenseListShare(Base):
    __tablename__ = "expense_list_shares"
    __table_args__ = (UniqueConstraint("expense_list_id", "user_id", name="uq_expense_share"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    expense_list_id: Mapped[int] = mapped_column(ForeignKey("expense_lists.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    expense_list: Mapped[ExpenseList] = relationship(back_populates="shares")
    user: Mapped[User] = relationship()


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    expense_list_id: Mapped[int] = mapped_column(ForeignKey("expense_lists.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    expense_list: Mapped[ExpenseList] = relationship(back_populates="categories")
    items: Mapped[list["ExpenseItem"]] = relationship(back_populates="category", cascade="all, delete-orphan")

    @property
    def total(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal("0.00"))


class ExpenseItem(Base):
    __tablename__ = "expense_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    include_in_analytics: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    include_in_forecast: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    category: Mapped[ExpenseCategory] = relationship(back_populates="items")


class ExpenseMerchantRule(Base):
    __tablename__ = "expense_merchant_rules"
    __table_args__ = (
        UniqueConstraint("owner_id", "expense_list_id", "merchant_key", name="uq_expense_merchant_rule"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    expense_list_id: Mapped[int] = mapped_column(ForeignKey("expense_lists.id"), nullable=False, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"), nullable=False, index=True)
    merchant_key: Mapped[str] = mapped_column(String(120), nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )


class IncomeItem(Base):
    __tablename__ = "income_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped[User] = relationship()


class ExpenseLimit(Base):
    __tablename__ = "expense_limits"
    __table_args__ = (UniqueConstraint("owner_id", "category_name", name="uq_expense_limit_category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    category_name: Mapped[str] = mapped_column(String(120), nullable=False)
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship()


class RecurringExpense(Base):
    __tablename__ = "recurring_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    expense_list_id: Mapped[int] = mapped_column(ForeignKey("expense_lists.id"), nullable=False)
    category_name: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_applied_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship()
    expense_list: Mapped[ExpenseList] = relationship()


class DebtSplit(Base):
    __tablename__ = "debt_splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    debtor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    debtor_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    debtor: Mapped[User] = relationship(foreign_keys=[debtor_id])


class ChatMessage(Base):
    """Legacy table from the first project version. Kept so existing databases do not break."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, index=True)

    sender: Mapped[User] = relationship(foreign_keys=[sender_id])
    recipient: Mapped[User] = relationship(foreign_keys=[recipient_id])


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    user_a_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    user_b_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    user_a: Mapped[User] = relationship(foreign_keys=[user_a_id])
    user_b: Mapped[User] = relationship(foreign_keys=[user_b_id])
    messages: Mapped[list["ChatThreadMessage"]] = relationship(back_populates="thread", cascade="all, delete-orphan")
    notes: Mapped[list["ChatNote"]] = relationship(back_populates="thread", cascade="all, delete-orphan")


class ChatThreadMessage(Base):
    __tablename__ = "chat_thread_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("chat_thread_messages.id"), nullable=True)
    attachment_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, index=True)

    thread: Mapped[ChatThread] = relationship(back_populates="messages", foreign_keys=[thread_id])
    sender: Mapped[User] = relationship(foreign_keys=[sender_id])
    reply_to: Mapped["ChatThreadMessage | None"] = relationship(remote_side=[id])


class ChatNote(Base):
    __tablename__ = "chat_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    thread: Mapped[ChatThread] = relationship(back_populates="notes")
    author: Mapped[User] = relationship()


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="want", nullable=False)
    goal_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    saved_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    expense_item_id: Mapped[int | None] = mapped_column(ForeignKey("expense_items.id"), nullable=True)
    expense_prev_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    expense_prev_is_done: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="wishlist_items")
    expense_item: Mapped["ExpenseItem | None"] = relationship()


class WishlistShare(Base):
    __tablename__ = "wishlist_shares"
    __table_args__ = (UniqueConstraint("owner_id", "user_id", name="uq_wishlist_share"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), nullable=False)
    plan_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    meal_name: Mapped[str] = mapped_column(String(80), nullable=False, default="Обед")
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="menu_items")
    recipe: Mapped[Recipe] = relationship()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(600), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(300), nullable=False)
    auth: Mapped[str] = mapped_column(String(120), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    user: Mapped[User] = relationship(back_populates="push_subscriptions")
    reminder_deliveries: Mapped[list["PlannerReminderDelivery"]] = relationship(
        back_populates="push_subscription",
        cascade="all, delete-orphan",
    )


class PlannerReminderDelivery(Base):
    __tablename__ = "planner_reminder_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "planner_reminder_id",
            "occurrence_key",
            "push_subscription_id",
            name="uq_planner_reminder_delivery_identity",
        ),
        CheckConstraint(
            "status IN ('pending', 'retry', 'sent', 'failed')",
            name="ck_planner_reminder_delivery_status",
        ),
        Index("ix_planner_reminder_delivery_due_status", "due_at", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    planner_reminder_id: Mapped[int] = mapped_column(
        ForeignKey("planner_reminders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    occurrence_key: Mapped[str] = mapped_column(String(100), nullable=False)
    push_subscription_id: Mapped[int] = mapped_column(
        ForeignKey("push_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    planner_reminder: Mapped[PlannerReminder] = relationship(back_populates="deliveries")
    push_subscription: Mapped[PushSubscription] = relationship(back_populates="reminder_deliveries")


class ShoppingCategoryRule(Base):
    __tablename__ = "shopping_category_rules"
    __table_args__ = (UniqueConstraint("owner_id", "keyword", name="uq_shopping_rule_keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(120), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="shopping_category_rules")


class ShoppingList(Base):
    __tablename__ = "shopping_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="shopping_lists")
    items: Mapped[list["ShoppingItem"]] = relationship(back_populates="shopping_list", cascade="all, delete-orphan")
    shares: Mapped[list["ShoppingListShare"]] = relationship(back_populates="shopping_list", cascade="all, delete-orphan")


class ShoppingListShare(Base):
    __tablename__ = "shopping_list_shares"
    __table_args__ = (UniqueConstraint("shopping_list_id", "user_id", name="uq_shopping_share"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shopping_list_id: Mapped[int] = mapped_column(ForeignKey("shopping_lists.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="shares")
    user: Mapped[User] = relationship()


class ShoppingItem(Base):
    __tablename__ = "shopping_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shopping_list_id: Mapped[int] = mapped_column(ForeignKey("shopping_lists.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    amount: Mapped[str | None] = mapped_column(String(120), nullable=True)
    department: Mapped[str] = mapped_column(String(80), default="Прочее", nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive)

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="items")
