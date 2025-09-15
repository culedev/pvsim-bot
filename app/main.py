from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from app.log_config import configure_logging
import os

configure_logging()

load_dotenv()

from app.endpoints import router

app = FastAPI(title="Sistema de Dimensionado Fotovoltaico Profesional", version="3.0.0")

API_KEY = os.getenv("API_KEY")

@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if request.url.path in ("/", "/health"):
        return await call_next(request)

    provided = request.headers.get("x-api-key")
    if API_KEY and provided == API_KEY:
        return await call_next(request)

    return JSONResponse({"detail": "Unauthorized"}, status_code=401)

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
