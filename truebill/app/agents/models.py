"""Pydantic v2 models for every agent output (validated by the runner)."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RetrievalOut(BaseModel):
    lane: str
    fact_ids: list[str]
    records: int


class Explanation(BaseModel):
    rule: Literal["R1", "R2", "R3", "R4", "PB1", "PB2", "PB3"]
    text: str
    fact_ids: list[str] = Field(min_length=1)


class ReconciliationOut(BaseModel):
    fired: list[str]
    explanations: list[Explanation]
    fact_ids: list[str]
    rule_set_version: str


class Alternative(BaseModel):
    option: str
    label: str
    impact: str
    amount: Optional[float] = None


class SimilarCase(BaseModel):
    id: str
    text: str
    score: float


class RecommendationOut(BaseModel):
    option: Literal["PRORATE", "SUPPRESS", "FLAG", "RESPOND"]
    label: str
    probable_cause: str
    cause_label: str
    cause_quote: str = ""
    confidence: float = Field(ge=0, le=1)
    signals: list[str]
    amount: float
    amount_kind: Literal["reduction", "credit", "projected_exposure", "linked_case"]
    amount_fact_id: Optional[str] = None
    calculation: dict[str, Any] = {}
    alternatives: list[Alternative]
    s_codes: list[str]
    similar_cases: list[SimilarCase]
    rationale: str
    fact_ids: list[str]
    rule_set_version: str


class PlanStep(BaseModel):
    seq: int
    step: Literal["CORRECT_PREMISE_REFERENCE", "REPROCESS_INTEGRATION_MESSAGE", "STOP_SA", "ZERO_POST_DISCONNECT_READS",
                  "REGENERATE_DRAFT_BILL", "CANCEL_DRAFT_BILL", "CLOSE_TODO", "CREATE_CREDIT_MEMO", "ADD_CRM_NOTE"]
    params: dict[str, Any]
    check: str


class PlanOut(BaseModel):
    option: str
    steps: list[PlanStep]
    fact_ids: list[str] = []


class ExecutionOut(BaseModel):
    status: Literal["DONE", "STEP_FAILED"]
    explanation: str
    fact_ids: list[str] = []


class CommunicationsOut(BaseModel):
    customer_summary: Optional[str] = None
    executive_brief: Optional[str] = None
    jira_ticket: Optional[str] = None
    regulator_response: Optional[str] = None
    fact_ids: list[str] = []
