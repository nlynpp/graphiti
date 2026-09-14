from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from graph_service.config import get_settings
from graph_service.routers import ingest, retrieve
from graph_service.zep_graphiti import initialize_graphiti


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    await initialize_graphiti(settings)
    yield
    # Shutdown
    # No need to close Graphiti here, as it's handled per-request


app = FastAPI(lifespan=lifespan)


app.include_router(retrieve.router)
app.include_router(ingest.router)

# Retrieval test bench (static single-page UI).
_STATIC_DIR = Path(__file__).parent / 'static'
app.mount('/ui', StaticFiles(directory=_STATIC_DIR, html=True), name='ui')


@app.get('/healthcheck')
async def healthcheck():
    return JSONResponse(content={'status': 'healthy'}, status_code=200)
