"""
Parses AWS Cost & Usage Report (CUR) CSV exports.

AWS CUR column names vary depending on export settings (legacy CUR vs CUR 2.0 in
CSV form, resource-ID inclusion, etc). Real headers look like:
    lineItem/UsageAccountId, lineItem/UnblendedCost, product/ProductName, ...
or sometimes without the slash prefix, or with different casing.

Rather than hard-coding exact header strings, we normalize every header
(lowercase, strip whitespace) and match against a list of known candidate
names for each field we care about. This makes ingestion resilient to the
column-set differences you'll see across payer accounts / CUR versions.
"""
import uuid
from datetime import datetime

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Upload, UploadStatus

CHUNK_SIZE = 50_000

# canonical_field -> ordered list of candidate normalized header names
CANDIDATES = {
    "invoice_id": ["bill/invoiceid"],
    "usage_account_id": ["lineitem/usageaccountid", "lineitem/usageaccountname"],
    "line_item_type": ["lineitem/lineitemtype"],
    "usage_start_date": ["lineitem/usagestartdate"],
    "usage_end_date": ["lineitem/usageenddate"],
    "product_code": ["lineitem/productcode", "product/productname"],
    "product_name": ["product/productname", "lineitem/productcode"],
    "usage_type": ["lineitem/usagetype"],
    "operation": ["lineitem/operation"],
    "region": ["product/region", "product/regioncode"],
    "availability_zone": ["lineitem/availabilityzone"],
    "resource_id": ["lineitem/resourceid"],
    "item_description": ["lineitem/lineitemdescription"],
    "usage_amount": ["lineitem/usageamount"],
    "unblended_cost": ["lineitem/unblendedcost"],
    "blended_cost": ["lineitem/blendedcost", "lineitem/unblendedcost"],
    "currency_code": ["lineitem/currencycode", "pricing/currency"],
    "billing_period_start": ["bill/billingperiodstartdate"],
    "billing_period_end": ["bill/billingperiodenddate"],
}


def _normalize(h: str) -> str:
    return h.strip().lower()


def _build_header_map(actual_headers):
    """Map canonical field -> actual CSV header name (or None if absent)."""
    norm_to_actual = {_normalize(h): h for h in actual_headers}
    resolved = {}
    for field, candidates in CANDIDATES.items():
        found = None
        for cand in candidates:
            if cand in norm_to_actual:
                found = norm_to_actual[cand]
                break
        resolved[field] = found
    return resolved


def _to_datetime(series):
    return pd.to_datetime(series, errors="coerce", utc=True).dt.tz_localize(None)


def _to_float(series):
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


class CurParseError(Exception):
    pass


def parse_and_ingest(file_path: str, upload_id: uuid.UUID, db: Session) -> None:
    """
    Streams the CSV in chunks, normalizes columns, and bulk-inserts into
    cur_line_items. Updates the Upload row's status/row_count/total_cost
    when done (or FAILED with an error message on exception).
    """
    upload = db.query(Upload).get(upload_id)
    try:
        # Peek headers first
        header_df = pd.read_csv(file_path, nrows=0)
        headers = list(header_df.columns)
        colmap = _build_header_map(headers)

        if not colmap["unblended_cost"] and not colmap["blended_cost"]:
            raise CurParseError(
                "Could not find a cost column (lineItem/UnblendedCost or "
                "lineItem/BlendedCost) in this file. Is this a valid AWS CUR export?"
            )

        total_rows = 0
        total_cost = 0.0
        billing_start, billing_end = None, None

        reader = pd.read_csv(file_path, chunksize=CHUNK_SIZE, low_memory=False)
        for chunk in reader:
            out = pd.DataFrame()
            out["upload_id"] = [str(upload_id)] * len(chunk)

            for field in [
                "invoice_id", "usage_account_id", "line_item_type", "product_code",
                "product_name", "usage_type", "operation", "region",
                "availability_zone", "resource_id", "item_description", "currency_code",
            ]:
                col = colmap.get(field)
                out[field] = chunk[col] if col else None

            for field in ["usage_start_date", "usage_end_date"]:
                col = colmap.get(field)
                out[field] = _to_datetime(chunk[col]) if col else pd.NaT

            for field, fallback in [("unblended_cost", None), ("blended_cost", "unblended_cost")]:
                col = colmap.get(field)
                if col:
                    out[field] = _to_float(chunk[col])
                elif fallback and colmap.get(fallback):
                    out[field] = _to_float(chunk[colmap[fallback]])
                else:
                    out[field] = 0.0

            col = colmap.get("usage_amount")
            out["usage_amount"] = _to_float(chunk[col]) if col else 0.0

            # Track billing period + running totals from the first/available chunk
            bp_start_col, bp_end_col = colmap.get("billing_period_start"), colmap.get("billing_period_end")
            if billing_start is None and bp_start_col and len(chunk):
                billing_start = str(chunk[bp_start_col].iloc[0])
            if billing_end is None and bp_end_col and len(chunk):
                billing_end = str(chunk[bp_end_col].iloc[0])

            total_cost += float(out["unblended_cost"].sum())
            total_rows += len(out)

            out.to_sql(
                "cur_line_items", con=db.get_bind(), if_exists="append",
                index=False, method="multi", chunksize=5000,
            )

        upload.status = UploadStatus.COMPLETED
        upload.row_count = total_rows
        upload.total_unblended_cost = total_cost
        upload.billing_period_start = billing_start
        upload.billing_period_end = billing_end
        db.commit()

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        upload = db.query(Upload).get(upload_id)
        upload.status = UploadStatus.FAILED
        upload.error_message = str(exc)[:2000]
        db.commit()
        raise
