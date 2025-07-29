from fastapi import FastAPI
from dotenv import load_dotenv
from app.log_config import configure_logging

configure_logging()

load_dotenv()

from app.endpoints import router

app = FastAPI(title="Sistema de Dimensionado Fotovoltaico Profesional", version="3.0.0")

app.include_router(router)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)