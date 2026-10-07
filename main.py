import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers.access_request_router import router as access_request_router
from app.routers.admin_router import router as admin_router
from app.routers.ai_router import router as ai_router
from app.routers.auth_router import router as auth_router
from app.routers.ai_router_v2 import router as ai_router_v2
from app.routers.ai_router_v3 import router as ai_router_v3
from app.routers.ai_router_v4 import router as ai_router_v4
from app.routers.ai_router_v5 import router as ai_router_v5
# from app.routers.ai_router_v6 import router as ai_router_v6  # disabled: Anaya not in use yet
from app.routers.enquiry_router import router as enquiry_router
from app.routers.cities_router import router as cities_router
from app.routers.config_router import router as config_router
from app.routers.flight_router import router as flight_router
from app.routers.hotel_router import router as hotel_router
from app.routers.hotel_results_router import router as hotel_results_router
from app.routers.invite_router import router as invite_router
from app.routers.places_router import router as places_router
from app.routers.referral_router import router as referral_router
from app.internal.internal_router import router as internal_router
from app.routers.whatsapp_router_v6 import router as whatsapp_router_v6
from app.routers.comms_router import router as comms_router
from app.routers.me_router import router as me_router
from app.routers.my_year_router import router as my_year_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title="TripAgent Hotel Proxy")

# CORS: local dev vs. production is decided ONLY by settings.is_local_dev
# (APP_ENV=development, explicit and fail-closed — see config.py's own
# note). This is the ONE branch point; nothing else in this file, or
# anywhere else, can flip it.
#
# Local dev (APP_ENV=development): static dev servers (`npx serve .`,
# `python -m http.server`, Vite, etc.) pick a fresh random port on every
# restart — a fixed allow_origins list means every restart breaks CORS for
# request-access.html/invitation.html until someone manually adds the new
# port. allow_origin_regex matches ANY http://localhost:<port> or
# http://127.0.0.1:<port> automatically, so this is a one-time fix, not a
# recurring chore. This can ONLY match http://localhost or http://127.0.0.1
# origins — it cannot widen to any other host, scheme, or a real domain,
# by construction of the regex itself.
#
# Everything else (APP_ENV unset, misspelled, or "production"): the
# original strict, explicit allow_origins list — unchanged from before this
# fix. A real deployment that forgets to set APP_ENV gets the STRICT
# behavior, never the permissive one — see config.py's fail-closed note.
if settings.is_local_dev:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5500",
            "https://tripagent-site-orpin.vercel.app",
            # Real deployed frontend (2026-09-18, direct request) — the
            # designer's own Vercel deployment, calling this backend on
            # Render directly from the browser. Name ("tripcusfe" —
            # TripAgent Customer FE) closely resembles the app/ Vite
            # project this file already anticipated deploying (see the
            # "production origin isn't known yet" note that used to sit
            # here) — plausibly the same project, not confirmed as such;
            # noted, not assumed. Vercel preview/production URLs can
            # change on a later redeploy — if CORS breaks again after a
            # new deploy, that's the first thing to check.
            "https://tripcusfe-dzbkt4q0q-trip-agent.vercel.app",
            # The React app (app/) — Vite dev server.
            "http://localhost:5173",
            "http://localhost:5174",
            # TRIPAGENT-FE (Phase C access-request admin review screen,
            # 2026-09-16) — a SEPARATE codebase/deployment from this one,
            # calling GET/POST /access-requests/* directly from the browser
            # (AdminPanel.tsx). Its production origin isn't known yet either —
            # same "add it here once deployed" note as the React app above.
            "http://localhost:3000",
        ],
        # Vercel preview deployments for this project get a fresh,
        # randomly-generated subdomain on every deploy (e.g.
        # customerfe-3s1wwyrsr-trip-agent.vercel.app) — a fixed list can't
        # keep up. This regex allows ANY subdomain ending in
        # '-trip-agent.vercel.app', covering every preview/production URL
        # Vercel generates for this project without needing a manual
        # update each deploy.
        allow_origin_regex=r"^https://[a-zA-Z0-9-]+-trip-agent\.vercel\.app$",
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True
    )

app.include_router(hotel_router)
app.include_router(hotel_results_router)
app.include_router(flight_router)
app.include_router(admin_router)
app.include_router(access_request_router)
app.include_router(invite_router)
app.include_router(referral_router)
app.include_router(ai_router)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(my_year_router)
app.include_router(ai_router_v2)
app.include_router(ai_router_v3)
app.include_router(ai_router_v4)
app.include_router(ai_router_v5)
# app.include_router(ai_router_v6)  # disabled: Anaya not in use yet
app.include_router(whatsapp_router_v6)
app.include_router(comms_router)
app.include_router(internal_router)
app.include_router(enquiry_router)
app.include_router(cities_router)
app.include_router(config_router)
app.include_router(places_router)


@app.get("/health")
async def health():
    return {"status": "ok"}