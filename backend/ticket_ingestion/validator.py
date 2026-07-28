"""Ticket validator.

Gates a story before it is provisioned/handed to the agent: the description must
be non-empty and at least ``config.min_description_length`` characters (after
stripping surrounding whitespace), and the story must carry at least one
acceptance criterion. Stories that fail are routed to the clarification flow.
"""

from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import Ticket, ValidationResult


class TicketValidator:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def validate(self, story: Ticket) -> ValidationResult:
        failures: list[str] = []

        description = (story.description or "").strip()
        min_len = self.config.min_description_length
        if not description:
            failures.append(
                "Story description is empty; add at least "
                f"{min_len} characters describing the work."
            )
        elif len(description) < min_len:
            failures.append(
                f"Story description is too short: {len(description)} characters "
                f"(minimum {min_len})."
            )

        if not story.acceptance_criteria:
            failures.append(
                "Story is missing acceptance criteria; add at least one so the "
                "agent knows when the work is done."
            )

        return ValidationResult(is_valid=not failures, failures=failures)
