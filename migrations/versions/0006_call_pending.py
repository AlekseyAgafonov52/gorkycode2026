"""Persist the HR call-pending status.

Revision ID: 0006_call_pending
Revises: 0005_department_materials
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_call_pending"
down_revision = "0005_department_materials"
branch_labels = None
depends_on = None

CONSTRAINT = "responses_workflow_status_check"


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_constraint(CONSTRAINT, "responses", type_="check", schema=schema)
    op.create_check_constraint(CONSTRAINT, "responses",
                               "workflow_status IN ('new', 'call_pending', 'forwarded', 'not_forwarded')",
                               schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    responses = sa.table("responses", sa.column("workflow_status"), schema=schema)
    op.execute(responses.update().where(responses.c.workflow_status == "call_pending").values(workflow_status="new"))
    op.drop_constraint(CONSTRAINT, "responses", type_="check", schema=schema)
    op.create_check_constraint(CONSTRAINT, "responses",
                               "workflow_status IN ('new', 'forwarded', 'not_forwarded')", schema=schema)
