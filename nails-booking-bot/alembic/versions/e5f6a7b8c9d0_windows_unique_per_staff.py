"""day windows unique per staff

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-18
"""
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_day_windows_business_starts", "day_windows", type_="unique")
    op.create_unique_constraint(
        "uq_day_windows_staff_starts", "day_windows", ["staff_id", "starts_at"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_day_windows_staff_starts", "day_windows", type_="unique")
    op.create_unique_constraint(
        "uq_day_windows_business_starts", "day_windows", ["business_id", "starts_at"]
    )