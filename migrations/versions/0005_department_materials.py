"""Track delivery of the candidate card, report and original resume per recipient.

Revision ID: 0005_department_materials
Revises: 0004_department_closure
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_department_materials"
down_revision = "0004_department_closure"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.add_column("responses", sa.Column("department_delivery_state", JSONB(), nullable=False,
                                        server_default=sa.text("'{}'::jsonb")), schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_column("responses", "department_delivery_state", schema=schema)
