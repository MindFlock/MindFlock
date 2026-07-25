"""Interactive clarification handler for low-context tickets."""

import logging
import tempfile
from pathlib import Path

from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import (
    ClarificationResult,
    ProvisionedEnvironment,
    Ticket,
    ValidationResult,
)
from backend.ticket_ingestion.provisioner import EnvironmentProvisioner

_logger = logging.getLogger(__name__)


class InteractiveClarificationHandler:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self._provisioner = EnvironmentProvisioner(config)
        self._claude_runner = ClaudeCodeRunner(config)

    async def request_clarification(
        self,
        story: Ticket,
        validation_result: ValidationResult,
    ) -> ClarificationResult:
        env = await self._provisioner.provision(story)
        prompt = self._build_clarification_prompt(story, validation_result)
        await self._invoke_clarification_session(env, prompt, story)
        return ClarificationResult(action="provide_context", supplemental_context=None)

    def clarification_context(
        self,
        story: Ticket,
        validation_result: ValidationResult,
    ) -> str:
        """The clarification ask as supplemental context — no provisioning, no
        session launch.

        Used in engine mode: instead of spawning a separate standalone Cursor +
        terminal clarification session, the pipeline folds this prompt into the
        single MindFlock session so the agent asks the developer for the missing
        context in that one window.
        """
        return self._build_clarification_prompt(story, validation_result)

    def _build_clarification_prompt(
        self,
        story: Ticket,
        validation_result: ValidationResult,
    ) -> str:
        description = story.description.strip() or "(No description provided)"
        lines: list[str] = []
        lines.append(f"# Clarification Needed: SC-{story.id}")
        lines.append("")
        lines.append(f"## Story: {story.name}")
        lines.append("")
        lines.append("### Description")
        lines.append("")
        lines.append(description)
        lines.append("")
        lines.append("### Validation Failures")
        lines.append("")
        for failure in validation_result.failures:
            lines.append(f"- {failure}")
        lines.append("")
        lines.append(
            "Please ask the developer for additional context to address the "
            "failures above. If the developer prefers to skip this story, "
            "they can choose to skip this story and the pipeline will move on."
        )
        lines.append("")
        return "\n".join(lines)

    async def _invoke_clarification_session(
        self,
        env: ProvisionedEnvironment,
        prompt: str,
        story: Ticket,
    ) -> None:
        fd, path = tempfile.mkstemp(prefix=f"clarification_{story.id}_", suffix=".md")
        try:
            with open(fd, "w") as f:
                f.write(prompt)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        await self._claude_runner.invoke(
            env=env, story=story, supplemental_context=prompt
        )
