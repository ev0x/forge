from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..analytics import compute_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=schemas.FullStats)
def get_stats(
    db: Session = Depends(get_db),
    account_ids: list[int] = Query(default=[]),
    symbol: Optional[str] = None,
    strategy_id: Optional[int] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
):
    q = db.query(models.Trade)
    starting = 0.0
    total_payouts = 0.0
    if account_ids:
        q = q.filter(models.Trade.account_id.in_(account_ids))
        accts = db.query(models.Account).filter(models.Account.id.in_(account_ids)).all()
        starting = sum(a.starting_balance or 0 for a in accts)
        payouts = (
            db.query(models.Payout)
            .filter(models.Payout.account_id.in_(account_ids))
            .all()
        )
        total_payouts = sum(p.amount for p in payouts)
    else:
        accts = db.query(models.Account).all()
        starting = sum(a.starting_balance or 0 for a in accts)
        total_payouts = sum(p.amount for p in db.query(models.Payout).all())

    # Broker balance reconciliation. We compute current_equity PER ACCOUNT:
    #   - broker_balance when it's set (authoritative)
    #   - starting_balance + offset + trade_net_pnl - payouts  otherwise
    # …then sum across selected accounts. That way mixed coverage (e.g. 3 of 4
    # accounts have broker entered) still gives an accurate aggregate, instead
    # of silently falling back to a trade-only number for everyone.
    broker_accounts_covered = sum(1 for a in accts if a.broker_balance is not None)
    broker_accounts_total = len(accts)
    broker_total: Optional[float] = None
    if broker_accounts_covered > 0 and broker_accounts_total > 0:
        per_account_pnl: dict[int, float] = {}
        per_account_payouts: dict[int, float] = {}
        for t_row in db.query(models.Trade.account_id, models.Trade.net_pnl).all():
            per_account_pnl[t_row.account_id] = per_account_pnl.get(t_row.account_id, 0.0) + (t_row.net_pnl or 0)
        for p_row in db.query(models.Payout.account_id, models.Payout.amount).all():
            per_account_payouts[p_row.account_id] = per_account_payouts.get(p_row.account_id, 0.0) + (p_row.amount or 0)
        broker_total = 0.0
        for a in accts:
            if a.broker_balance is not None:
                broker_total += float(a.broker_balance)
            else:
                broker_total += float((a.starting_balance or 0)
                                      + (a.starting_balance_offset or 0)
                                      + per_account_pnl.get(a.id, 0.0)
                                      - per_account_payouts.get(a.id, 0.0))
    if symbol:
        q = q.filter(models.Trade.symbol == symbol)
    if strategy_id:
        q = q.filter(models.Trade.strategy_id == strategy_id)
    if from_date:
        q = q.filter(models.Trade.trade_date >= datetime.fromisoformat(from_date))
    if to_date:
        q = q.filter(models.Trade.trade_date <= datetime.fromisoformat(to_date))
    trades = q.all()
    strategies = {s.id: s for s in db.query(models.Strategy).all()}
    return compute_stats(
        trades, starting_balance=starting,
        total_payouts=total_payouts,
        strategies_by_id=strategies,
        broker_total=broker_total,
        broker_accounts_covered=broker_accounts_covered,
        broker_accounts_total=broker_accounts_total,
    )
