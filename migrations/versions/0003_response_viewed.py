"""Store HR viewed/unviewed independently of the phone interview outcome.

Revision ID: 0003_response_viewed
Revises: 0002_mvp_contract
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_response_viewed"
down_revision = "0002_mvp_contract"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.add_column("responses", sa.Column("is_viewed", sa.Boolean(), nullable=False,
                                        server_default=sa.text("FALSE")), schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_column("responses", "is_viewed", schema=schema)
