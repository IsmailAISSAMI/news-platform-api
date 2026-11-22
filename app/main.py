
from fastapi import FastAPI

app = FastAPI(
    title="News API",
    description="Enterprise News Platform API",
    version="0.1.0",
)


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "application": "news-backend",
    }
