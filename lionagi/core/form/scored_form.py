from lionagi.integrations.bridge.pydantic_.pydantic_bridge import Field

from lionagi.core.form.form import Form


class ScoredForm(Form):
    confidence_score: float | None = Field(
        -1,
        description="a numeric score between 0 to 1 formatted in num:0.2f",
    )
    reason: str | None = Field(
        default_factory=str, description="brief reason for the given output"
    )
