"""The eight tables and seven indexes from the agreed HR platform schema.

Revision ID: 0001
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')
    op.create_table('vacancies',
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'active'"), nullable=False),
    sa.Column('source_json', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('criteria_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'active', 'closed')", name='vacancies_status_check'),
    sa.CheckConstraint('criteria_version > 0', name='vacancies_criteria_version_check'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('responses',
    sa.Column('vacancy_id', sa.UUID(), nullable=False),
    sa.Column('candidate_name', sa.String(length=255), nullable=True),
    sa.Column('source_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('workflow_status', sa.String(length=20), server_default=sa.text("'new'"), nullable=False),
    sa.Column('is_archived', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("workflow_status IN ('new', 'reviewed', 'contacted')", name='responses_workflow_status_check'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_responses_status', 'responses', ['vacancy_id', 'workflow_status', 'is_archived'], unique=False)
    op.create_index('idx_responses_vacancy', 'responses', ['vacancy_id'], unique=False)
    op.create_table('vacancy_criteria',
    sa.Column('vacancy_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('importance', sa.String(length=20), server_default=sa.text("'preferred'"), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("importance IN ('required', 'preferred', 'irrelevant')", name='vacancy_criteria_importance_check'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_criteria_vacancy', 'vacancy_criteria', ['vacancy_id'], unique=False)
    op.create_table('analysis_tasks',
    sa.Column('vacancy_id', sa.UUID(), nullable=True),
    sa.Column('response_id', sa.UUID(), nullable=True),
    sa.Column('task_type', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'processing', 'completed', 'failed')", name='analysis_tasks_status_check'),
    sa.CheckConstraint("task_type IN ('matching', 'questions')", name='analysis_tasks_task_type_check'),
    sa.CheckConstraint('vacancy_id IS NOT NULL OR response_id IS NOT NULL', name='analysis_tasks_target_required'),
    sa.ForeignKeyConstraint(['response_id'], ['responses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tasks_status', 'analysis_tasks', ['status'], unique=False)
    op.create_table('match_results',
    sa.Column('response_id', sa.UUID(), nullable=False),
    sa.Column('match_score', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('match_status', sa.String(length=10), nullable=False),
    sa.Column('missing_data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('criteria_version', sa.Integer(), nullable=False),
    sa.Column('analyzed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.CheckConstraint("match_status IN ('green', 'yellow', 'red')", name='match_results_match_status_check'),
    sa.CheckConstraint('criteria_version > 0', name='match_results_criteria_version_check'),
    sa.CheckConstraint('match_score BETWEEN 0 AND 100', name='match_results_match_score_check'),
    sa.ForeignKeyConstraint(['response_id'], ['responses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('response_id', name='match_results_response_id_key')
    )
    op.create_index('idx_match_results_score', 'match_results', [sa.literal_column('match_score DESC')], unique=False)
    op.create_table('reports',
    sa.Column('response_id', sa.UUID(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('strengths', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('structured_resume', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('criteria_version', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('criteria_version > 0', name='reports_criteria_version_check'),
    sa.ForeignKeyConstraint(['response_id'], ['responses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('response_id', name='reports_response_id_key')
    )
    op.create_table('criterion_evaluations',
    sa.Column('match_result_id', sa.UUID(), nullable=False),
    sa.Column('criterion_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.CheckConstraint("status IN ('matched', 'partial', 'not_matched', 'unknown')", name='criterion_evaluations_status_check'),
    sa.ForeignKeyConstraint(['criterion_id'], ['vacancy_criteria.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['match_result_id'], ['match_results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('match_result_id', 'criterion_id', name='criterion_evaluations_match_result_id_criterion_id_key')
    )
    op.create_index('idx_evaluations_match', 'criterion_evaluations', ['match_result_id'], unique=False)
    op.create_table('questions',
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('criterion_id', sa.UUID(), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('expected_answer', sa.Text(), nullable=True),
    sa.Column('follow_up_hint', sa.Text(), nullable=True),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.ForeignKeyConstraint(['criterion_id'], ['vacancy_criteria.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_questions_report', 'questions', ['report_id', 'position'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_questions_report', table_name='questions')
    op.drop_table('questions')
    op.drop_index('idx_evaluations_match', table_name='criterion_evaluations')
    op.drop_table('criterion_evaluations')
    op.drop_table('reports')
    op.drop_index('idx_match_results_score', table_name='match_results')
    op.drop_table('match_results')
    op.drop_index('idx_tasks_status', table_name='analysis_tasks')
    op.drop_table('analysis_tasks')
    op.drop_index('idx_criteria_vacancy', table_name='vacancy_criteria')
    op.drop_table('vacancy_criteria')
    op.drop_index('idx_responses_vacancy', table_name='responses')
    op.drop_index('idx_responses_status', table_name='responses')
    op.drop_table('responses')
    op.drop_table('vacancies')
    # pgcrypto may be shared with other applications; keep the extension.
