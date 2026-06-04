import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import engine, Base, SessionLocal
from . import models  # noqa: F401 -- register tables
from .migrations import run_migrations
from .prop_firms import seed_prop_firms
from .routers import (
    accounts, uploads, trades, stats, executions, strategies, payouts,
    prop_firms, account_costs, economics, playbooks, settings as settings_router, plan,
    attachments, admin, market_data,
)

app = FastAPI(title="Forge — self-hosted trading journal", version="0.2.0")

cors_origins = os.environ.get("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if cors_origins == "*" else [o.strip() for o in cors_origins.split(",")],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    # Seed prop firm presets from prop_firms.py (idempotent — doesn't overwrite user edits)
    db = SessionLocal()
    try:
        seed_prop_firms(db)
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True}


app.include_router(accounts.router)
app.include_router(uploads.router)
app.include_router(trades.router)
app.include_router(stats.router)
app.include_router(executions.router)
app.include_router(strategies.router)
app.include_router(payouts.router)
app.include_router(prop_firms.router)
app.include_router(account_costs.router)
app.include_router(economics.router)
app.include_router(playbooks.router)
app.include_router(settings_router.router)
app.include_router(plan.router)
app.include_router(attachments.router)
app.include_router(admin.router)
app.include_router(market_data.router)
