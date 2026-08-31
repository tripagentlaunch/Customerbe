import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.admin_router import router as admin_router
from app.routers.ai_router import router as ai_router
from app.routers.flight_router import router as flight_router
from app.routers.hotel_router import router as hotel_router
from app.routers.invite_router import router as invite_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title="TripAgent Hotel Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "https://tripagent-site-orpin.vercel.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(hotel_router)
app.include_router(flight_router)
app.include_router(admin_router)
app.include_router(invite_router)
app.include_router(ai_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
