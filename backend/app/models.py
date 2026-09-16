import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, ForeignKey, Index, Enum, Text
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class UploadStatus(str, enum.Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Upload(Base):
    """One row per uploaded CUR CSV file (a 'batch')."""
    __tablename__ = "uploads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String, nullable=False)
    status = Column(Enum(UploadStatus), default=UploadStatus.PROCESSING, nullable=False)
    row_count = Column(Integer, default=0)
    total_unblended_cost = Column(Float, default=0.0)
    billing_period_start = Column(String, nullable=True)
    billing_period_end = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    line_items = relationship(
        "CurLineItem", back_populates="upload", cascade="all, delete-orphan"
    )


class CurLineItem(Base):
    """A single normalized row from the CUR CSV."""
    __tablename__ = "cur_line_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    upload_id = Column(UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False)

    invoice_id = Column(String, nullable=True)
    usage_account_id = Column(String, nullable=True)
    line_item_type = Column(String, nullable=True)  # Usage, Tax, Credit, Refund, Fee, DiscountedUsage...
    usage_start_date = Column(DateTime, nullable=True)
    usage_end_date = Column(DateTime, nullable=True)
    product_code = Column(String, nullable=True)     # e.g. AmazonEC2, AmazonS3
    product_name = Column(String, nullable=True)      # e.g. Amazon Elastic Compute Cloud
    usage_type = Column(String, nullable=True)        # e.g. BoxUsage:t3.micro, DataTransfer-Out-Bytes
    operation = Column(String, nullable=True)
    region = Column(String, nullable=True)
    availability_zone = Column(String, nullable=True)
    resource_id = Column(String, nullable=True)
    item_description = Column(String, nullable=True)

    usage_amount = Column(Float, default=0.0)
    unblended_cost = Column(Float, default=0.0)
    blended_cost = Column(Float, default=0.0)
    currency_code = Column(String, nullable=True)

    upload = relationship("Upload", back_populates="line_items")

    __table_args__ = (
        Index("ix_cur_upload_id", "upload_id"),
        Index("ix_cur_product_code", "product_code"),
        Index("ix_cur_usage_start_date", "usage_start_date"),
        Index("ix_cur_line_item_type", "line_item_type"),
        Index("ix_cur_usage_account_id", "usage_account_id"),
    )
