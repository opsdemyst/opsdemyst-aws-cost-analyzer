import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class UploadOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: str
    row_count: int
    total_unblended_cost: float
    billing_period_start: Optional[str] = None
    billing_period_end: Optional[str] = None
    error_message: Optional[str] = None
    uploaded_at: datetime

    class Config:
        from_attributes = True
