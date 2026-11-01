import logging

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import artifacts, auth, files, sessions
from app.config import get_settings
from app.db import engine
from app.documents.preview import find_soffice
from app.research.index import search_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()
if settings.jwt_secret == "change-me-in-production":
    logging.getLogger(__name__).warning("JWT_SECRET is still the default. Set it before exposing this API.")

app = FastAPI(title="Redline", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

api = APIRouter(prefix="/api")
api.include_router(auth.router)
api.include_router(sessions.router)
api.include_router(files.router)
api.include_router(artifacts.router)


@api.get("/health")
def health() -> dict[str, object]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.llm_label,
            "configured": settings.llm_configured,
        },
        "search": search_backend(),
        "web": "tavily" if settings.tavily_api_key else "duckduckgo",
        "previews": find_soffice() is not None,
    }


app.include_router(api)
