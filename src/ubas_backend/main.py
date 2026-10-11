from fastapi import FastAPI

from ubas_backend.api.router import router
from ubas_backend.query_manager.service import QueryManager

app = FastAPI(title="UBAS Backend")

app.state.query_manager = QueryManager()

app.include_router(router, prefix="/api/v1")
