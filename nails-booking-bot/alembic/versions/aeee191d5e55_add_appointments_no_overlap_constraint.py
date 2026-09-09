"""add appointments no overlap constraint

Revision ID: aeee191d5e55
Revises: 85eafec1fbdd
Create Date: 2026-09-09 00:33:28.010099

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aeee191d5e55'
down_revision: Union[str, Sequence[str], None] = '85eafec1fbdd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT appointments_no_overlap
        EXCLUDE USING gist (
            business_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        WHERE (status = 'confirmed')
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_no_overlap"
    )
    op.execute("DROP EXTENSION IF EXISTS btree_gist")