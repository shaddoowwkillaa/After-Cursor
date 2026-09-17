"""add staff table and per-staff ownership

Revision ID: b7c1d2e3f4a5
Revises: a254298369b9
Create Date: 2026-09-14
"""
import sqlalchemy as sa
from alembic import op

revision = "b7c1d2e3f4a5"
down_revision = "a254298369b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("is_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "telegram_id", name="uq_staff_business_telegram"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_staff_business_id", "staff", ["business_id"])

    op.add_column("services", sa.Column("staff_id", sa.BigInteger(), nullable=True))
    op.add_column("day_windows", sa.Column("staff_id", sa.BigInteger(), nullable=True))
    op.add_column("appointments", sa.Column("staff_id", sa.BigInteger(), nullable=True))

    # Перенос данных: владелец каждого бизнеса становится staff №1,
    # все его услуги, окошки и записи привязываются к нему.
    conn = op.get_bind()
    businesses = conn.execute(
        sa.text("SELECT id, name, owner_telegram_id FROM businesses")
    ).fetchall()
    for biz in businesses:
        row = conn.execute(
            sa.text(
                "INSERT INTO staff (business_id, name, telegram_id, is_owner, is_active) "
                "VALUES (:bid, :name, :tid, TRUE, TRUE) RETURNING id"
            ),
            {"bid": biz.id, "name": biz.name, "tid": biz.owner_telegram_id},
        ).fetchone()
        staff_id = row[0]
        for table in ("services", "day_windows", "appointments"):
            conn.execute(
                sa.text(f"UPDATE {table} SET staff_id = :sid WHERE business_id = :bid"),
                {"sid": staff_id, "bid": biz.id},
            )

    op.create_index("ix_services_staff_id", "services", ["staff_id"])
    op.create_index("ix_day_windows_staff_id", "day_windows", ["staff_id"])
    op.create_index("ix_appointments_staff_id", "appointments", ["staff_id"])
    op.create_foreign_key(
        "fk_services_staff", "services", "staff", ["staff_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_day_windows_staff", "day_windows", "staff", ["staff_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_appointments_staff", "appointments", "staff", ["staff_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint("fk_appointments_staff", "appointments", type_="foreignkey")
    op.drop_constraint("fk_day_windows_staff", "day_windows", type_="foreignkey")
    op.drop_constraint("fk_services_staff", "services", type_="foreignkey")
    op.drop_index("ix_appointments_staff_id", table_name="appointments")
    op.drop_index("ix_day_windows_staff_id", table_name="day_windows")
    op.drop_index("ix_services_staff_id", table_name="services")
    op.drop_column("appointments", "staff_id")
    op.drop_column("day_windows", "staff_id")
    op.drop_column("services", "staff_id")
    op.drop_index("ix_staff_business_id", table_name="staff")
    op.drop_table("staff")