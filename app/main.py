from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.console.routes import router as console_router

app = FastAPI(title="Jarvis MCP")

app.include_router(console_router)
app.mount("/console/static", StaticFiles(directory="app/console/static"), name="console-static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
