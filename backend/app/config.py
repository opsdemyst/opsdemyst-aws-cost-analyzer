import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
    DATABASE_URL = os.getenv("DATABASE_URL")
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://cloud-cost.local")
    BACKEND_URL = os.getenv("BACKEND_URL", "http://api-cloud-cost.local")
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))

settings = Settings()
