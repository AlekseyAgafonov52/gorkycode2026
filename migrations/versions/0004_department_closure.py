"""Department completion is independent of the HR workflow.

Revision ID: 0004_department_closure
Revises: 0003_response_viewed
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_department_closure"
down_revision = "0003_response_viewed"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.add_column("responses", sa.Column("is_closed", sa.Boolean(), nullable=False,
                                        server_default=sa.text("FALSE")), schema=schema)
    for table in ("vacancies", "responses"):
        op.add_column(table, sa.Column("closed_at", sa.DateTime(timezone=True)), schema=schema)
    op.add_column("responses", sa.Column("closed_by_telegram_id", sa.BigInteger()), schema=schema)
    op.add_column("responses", sa.Column("hired_at", sa.DateTime(timezone=True)), schema=schema)
    op.add_column("responses", sa.Column("hired_by_telegram_id", sa.BigInteger()), schema=schema)
    op.add_column("responses", sa.Column("recruiter_recommendation", JSONB()), schema=schema)
    op.add_column("responses", sa.Column("department_notification_pending", sa.Boolean(), nullable=False,
                                        server_default=sa.text("FALSE")), schema=schema)
    op.add_column("responses", sa.Column("department_notification_version", sa.Integer(), nullable=False,
                                        server_default=sa.text("0")), schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    for column in ("department_notification_version", "department_notification_pending", "recruiter_recommendation",
                   "hired_by_telegram_id", "hired_at"):
        op.drop_column("responses", column, schema=schema)
    for table in ("responses", "vacancies"):
        op.drop_column(table, "closed_at", schema=schema)
    op.drop_column("responses", "closed_by_telegram_id", schema=schema)
    op.drop_column("responses", "is_closed", schema=schema)
