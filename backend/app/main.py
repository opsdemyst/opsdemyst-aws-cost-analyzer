import os
import shutil
import tempfile
import uuid

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import analysis, schemas
from app.database import Base, engine, get_db, SessionLocal
from app.config import settings
from app.models import Upload, UploadStatus
from app.cur_parser import parse_and_ingest, CurParseError

MAX_UPLOAD_MB = settings.MAX_UPLOAD_MB

app = FastAPI(title="AWS CUR Cost Insights", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _process_upload_bg(tmp_path: str, upload_id: uuid.UUID):
    """Runs in a background task with its own DB session."""
    db = SessionLocal()
    try:
        parse_and_ingest(tmp_path, upload_id, db)
    except Exception as e:
        upload = db.get(Upload, upload_id)
        if upload:
            upload.status = UploadStatus.FAILED
            upload.error_message = str(e)
            db.commit()
    finally:
        db.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/api/upload", response_model=schemas.UploadOut)
async def upload_cur_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file (AWS CUR export).")

    upload = Upload(filename=file.filename, status=UploadStatus.PROCESSING)
    db.add(upload)
    db.commit()
    db.refresh(upload)

    # Stream to a temp file, checking size as we go
    fd, tmp_path = tempfile.mkstemp(suffix=".csv")
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, f"File exceeds {MAX_UPLOAD_MB}MB limit.")
                out.write(chunk)
    except HTTPException:
        os.remove(tmp_path)
        upload.status = UploadStatus.FAILED
        upload.error_message = f"File exceeds {MAX_UPLOAD_MB}MB limit."
        db.commit()
        raise

    # For an MVP, process inline for small/medium files so the user gets
    # immediate feedback; larger files fall back to a background task.
    if written <= 20 * 1024 * 1024:
        try:
            parse_and_ingest(tmp_path, upload.id, db)
        except CurParseError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Failed to process file: {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        db.refresh(upload)
    else:
        background_tasks.add_task(_process_upload_bg, tmp_path, upload.id)

    return upload


@app.get("/api/uploads", response_model=list[schemas.UploadOut])
def list_uploads(db: Session = Depends(get_db)):
    return db.query(Upload).order_by(Upload.uploaded_at.desc()).all()


@app.get("/api/uploads/{upload_id}", response_model=schemas.UploadOut)
def get_upload(upload_id: uuid.UUID, db: Session = Depends(get_db)):
    u = db.query(Upload).get(upload_id)
    if not u:
        raise HTTPException(404, "Upload not found")
    return u


@app.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: uuid.UUID, db: Session = Depends(get_db)):
    u = db.query(Upload).get(upload_id)
    if not u:
        raise HTTPException(404, "Upload not found")
    db.delete(u)
    db.commit()
    return {"deleted": str(upload_id)}


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@app.get("/api/insights/summary")
def summary(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.get_summary(db, upload_id)


@app.get("/api/insights/by-service")
def by_service(upload_id: uuid.UUID | None = None, limit: int = 15, db: Session = Depends(get_db)):
    return analysis.get_cost_by_service(db, upload_id, limit)


@app.get("/api/insights/by-account")
def by_account(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.get_cost_by_account(db, upload_id)


@app.get("/api/insights/by-region")
def by_region(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.get_cost_by_region(db, upload_id)


@app.get("/api/insights/trend")
def trend(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.get_daily_trend(db, upload_id)


@app.get("/api/insights/by-line-item-type")
def by_line_item_type(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.get_cost_by_line_item_type(db, upload_id)


@app.get("/api/insights/top-resources")
def top_resources(upload_id: uuid.UUID | None = None, limit: int = 20, db: Session = Depends(get_db)):
    return analysis.get_top_resources(db, upload_id, limit)


@app.get("/api/insights/top-usage-types")
def top_usage_types(upload_id: uuid.UUID | None = None, limit: int = 20, db: Session = Depends(get_db)):
    return analysis.get_top_usage_types(db, upload_id, limit)


@app.get("/api/insights/recommendations")
def recommendations(upload_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    return analysis.generate_recommendations(db, upload_id)
