"""Persist vacancy pools and prepared MVP reports; migrate workflow vocabulary.

Revision ID: 0002_mvp_contract
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_mvp_contract"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.add_column("vacancies", sa.Column("question_pool", postgresql.JSONB(), nullable=False,
                                        server_default=sa.text("'[]'::jsonb")), schema=schema)
    op.add_column("vacancies", sa.Column("question_pool_hash", sa.String(64)), schema=schema)
    op.add_column("reports", sa.Column("contract_json", postgresql.JSONB()), schema=schema)
    op.add_column("reports", sa.Column("prepared_hash", sa.String(64)), schema=schema)
    op.add_column("responses", sa.Column("legacy_workflow_status", sa.String(20)), schema=schema)
    op.drop_constraint("responses_workflow_status_check", "responses", type_="check", schema=schema)
    # reviewed/contacted do not express a hiring decision. Preserve, do not guess it.
    responses = sa.table("responses", sa.column("workflow_status"), sa.column("legacy_workflow_status"), schema=schema)
    op.execute(responses.update().where(responses.c.workflow_status != "new").values(
        legacy_workflow_status=responses.c.workflow_status, workflow_status="new"))
    op.create_check_constraint("responses_workflow_status_check", "responses",
                               "workflow_status IN ('new', 'forwarded', 'not_forwarded')", schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_constraint("responses_workflow_status_check", "responses", type_="check", schema=schema)
    responses = sa.table("responses", sa.column("workflow_status"), sa.column("legacy_workflow_status"), schema=schema)
    op.execute(responses.update().values(workflow_status=sa.func.coalesce(responses.c.legacy_workflow_status, "new")))
    op.create_check_constraint("responses_workflow_status_check", "responses",
                               "workflow_status IN ('new', 'reviewed', 'contacted')", schema=schema)
    op.drop_column("responses", "legacy_workflow_status", schema=schema)
    op.drop_column("reports", "prepared_hash", schema=schema)
    op.drop_column("reports", "contract_json", schema=schema)
    op.drop_column("vacancies", "question_pool_hash", schema=schema)
    op.drop_column("vacancies", "question_pool", schema=schema)
