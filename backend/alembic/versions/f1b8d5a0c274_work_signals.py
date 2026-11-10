"""Wake automation on committed changes to its durable queues."""

from alembic import op

revision = "f1b8d5a0c274"
down_revision = "e0a7c4d2b965"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION prophet_signal_work() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('prophet_work_ready', TG_ARGV[0]);
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER prophet_evidence_arrived
        AFTER INSERT ON raw_evidence FOR EACH ROW
        WHEN (NOT NEW.is_processed AND NEW.source_item_type <> 'conversation_turn')
        EXECUTE FUNCTION prophet_signal_work('evidence_arrived')
    """)
    op.execute("""
        CREATE TRIGGER prophet_evidence_ready
        AFTER UPDATE OF extraction_status ON evidence_processing_states FOR EACH ROW
        WHEN (OLD.extraction_status IS DISTINCT FROM NEW.extraction_status
              AND NEW.extraction_status = 'completed' AND NEW.persisted_object_count > 0)
        EXECUTE FUNCTION prophet_signal_work('evidence_ready')
    """)
    op.execute("""
        CREATE TRIGGER prophet_question_opened
        AFTER INSERT ON unresolved_questions FOR EACH ROW
        WHEN (NEW.next_action = 'research' AND NEW.superseded_by_id IS NULL
              AND (NEW.next_review_at IS NULL OR NEW.next_review_at <= statement_timestamp()))
        EXECUTE FUNCTION prophet_signal_work('question_actionable')
    """)
    op.execute("""
        CREATE TRIGGER prophet_question_resumed
        AFTER UPDATE OF next_action, next_review_at ON unresolved_questions FOR EACH ROW
        WHEN (NEW.next_action = 'research' AND NEW.superseded_by_id IS NULL
              AND (NEW.next_review_at IS NULL OR NEW.next_review_at <= statement_timestamp())
              AND (OLD.next_action IS DISTINCT FROM NEW.next_action
                   OR OLD.next_review_at > statement_timestamp()))
        EXECUTE FUNCTION prophet_signal_work('question_actionable')
    """)
    op.execute("""
        CREATE TRIGGER prophet_shadow_evidence_queued
        AFTER INSERT ON shadow_evidence_events FOR EACH ROW
        WHEN (NEW.status = 'queued')
        EXECUTE FUNCTION prophet_signal_work('shadow_evidence_queued')
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER prophet_shadow_evidence_queued ON shadow_evidence_events")
    op.execute("DROP TRIGGER prophet_question_resumed ON unresolved_questions")
    op.execute("DROP TRIGGER prophet_question_opened ON unresolved_questions")
    op.execute("DROP TRIGGER prophet_evidence_ready ON evidence_processing_states")
    op.execute("DROP TRIGGER prophet_evidence_arrived ON raw_evidence")
    op.execute("DROP FUNCTION prophet_signal_work()")
