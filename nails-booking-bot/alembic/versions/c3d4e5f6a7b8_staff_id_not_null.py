"""staff_id not null

Revision ID: c3d4e5f6a7b8
Revises: b7c1d2e3f4a5
Create Date: 2026-09-18
"""
import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b7c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    businesses = conn.execute(
        sa.text("SELECT id FROM businesses")
    ).fetchall()
    for biz in businesses:
        row = conn.execute(
            sa.text(
                "SELECT id FROM staff WHERE business_id = :bid AND is_owner = TRUE LIMIT 1"
            ),
            {"bid": biz.id},
        ).fetchone()
        if row is None:
            continue
        staff_id = row[0]
        for table in ("services", "day_windows", "appointments"):
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET staff_id = :sid "
                    f"WHERE business_id = :bid AND staff_id IS NULL"
                ),
                {"sid": staff_id, "bid": biz.id},
            )
    op.alter_column("services", "staff_id", nullable=False)
    op.alter_column("day_windows", "staff_id", nullable=False)
    op.alter_column("appointments", "staff_id", nullable=False)


def downgrade() -> None:
    op.alter_column("appointments", "staff_id", nullable=True)
    op.alter_column("day_windows", "staff_id", nullable=True)
    op.alter_column("services", "staff_id", nullable=True)