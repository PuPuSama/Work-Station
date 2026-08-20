from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models import PromptKind


AttachmentClassificationKind = Literal[
    "knowledge_source",
    "prompt_asset",
    "task_workbook",
    "project_notes",
    "topic_library",
    "unsupported",
    "needs_user_choice",
]


class AttachmentClassification(BaseModel):
    """Strict, reviewable output accepted from an attachment classifier.

    This is a proposal only.  A valid instance never grants permission to
    import, publish knowledge, or change project configuration.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    classification: AttachmentClassificationKind
    reason: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    target_project_id: str | None = Field(default=None, min_length=1, max_length=255)
    prompt_kind: PromptKind | None = None
    candidate_classifications: list[AttachmentClassificationKind] = Field(
        default_factory=list,
        max_length=7,
    )
    is_ambiguous: bool = False
    structure_compatible: bool = True
    affects_multiple_projects: bool = False

    @model_validator(mode="after")
    def enforce_user_choice_boundaries(self) -> "AttachmentClassification":
        candidates = self.candidate_classifications
        if len(candidates) != len(set(candidates)):
            raise ValueError("candidate classifications must be unique")
        if "needs_user_choice" in candidates:
            raise ValueError("needs_user_choice cannot be a candidate classification")

        choice_required = (
            self.is_ambiguous
            or not self.structure_compatible
            or self.affects_multiple_projects
        )
        if self.classification != "unsupported":
            choice_required = choice_required or self.target_project_id is None
        if self.classification == "prompt_asset":
            choice_required = choice_required or self.prompt_kind is None
        if (
            self.classification == "needs_user_choice"
            and "prompt_asset" in candidates
        ):
            choice_required = choice_required or self.prompt_kind is None

        if choice_required and self.classification != "needs_user_choice":
            raise ValueError("ambiguous or incomplete classification must need user choice")

        if self.classification == "needs_user_choice":
            if not choice_required and len(candidates) < 2:
                raise ValueError("needs_user_choice must identify an unresolved choice")
        elif candidates:
            raise ValueError("candidate classifications are only valid for needs_user_choice")

        if self.classification != "prompt_asset" and self.prompt_kind is not None:
            if self.classification != "needs_user_choice":
                raise ValueError("prompt kind is only valid for prompt assets")

        if self.classification == "unsupported":
            if self.target_project_id is not None or self.prompt_kind is not None:
                raise ValueError("unsupported attachments cannot name an import target")

        return self


__all__ = [
    "AttachmentClassification",
    "AttachmentClassificationKind",
]
