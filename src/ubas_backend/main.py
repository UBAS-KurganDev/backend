import uvicorn
from fastapi import FastAPI

from ubas_backend.api.router import router
from ubas_backend.query_manager.service import QueryManager

app = FastAPI(title="UBAS Backend")

app.state.query_manager = QueryManager()

app.include_router(router, prefix="/api/v1")


def main() -> None:
    """Run the development ASGI server on http://127.0.0.1:8000."""
    uvicorn.run("ubas_backend.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
