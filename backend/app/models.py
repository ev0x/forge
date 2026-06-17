from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Index,
    UniqueConstraint, Text, Boolean,
)
from sqlalchemy.orm import relationship
from .db import Base


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    external_id = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    broker = Column(String, nullable=True)
    starting_balance = Column(Float, default=0.0)
    commission_per_side = Column(Float, default=0.0)         # fallback when commission_rates is empty
    fees_per_side = Column(Float, default=0.0)
    # JSON-encoded per-instrument-root commission overrides, e.g.
    # {"MNQ": 0.51, "MES": 0.51, "NQ": 1.99, "ES": 1.99}.
    # Per side per contract. Used in preference to commission_per_side when present.
    commission_rates = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Prop firm config
    account_type = Column(String, default="personal")    # personal | eval | pa | funded
    prop_firm = Column(String, nullable=True)            # Apex, TopStep, etc.
    profit_target = Column(Float, default=0.0)           # 0 = no target
    max_drawdown = Column(Float, default=0.0)            # positive amount (e.g. 2500)
    drawdown_mode = Column(String, default="none")       # none | static | trailing_eod | trailing_realtime
    trailing_drawdown_cap = Column(Float, default=0.0)   # optional cap on trailing peak rise (e.g. = profit_target for Apex)
    max_loss_per_day = Column(Float, default=0.0)        # 0 = none
    payout_threshold = Column(Float, default=0.0)        # min equity gain to be eligible
    payout_buffer = Column(Float, default=0.0)           # cushion to keep above DD floor when taking payout
    min_days_between_payouts = Column(Integer, default=0)

    archived = Column(Boolean, default=False)

    # Prop firm preset linkage
    prop_firm_key = Column(String, nullable=True)        # 'apex' | 'topstep' | ...
    prop_firm_plan_key = Column(String, nullable=True)   # 'apex_50k', 'topstep_100k', etc.

    # Payout rule details
    safety_net_amount = Column(Float, default=0.0)       # $ above starting that must remain after payout
    extra_safety_buffer = Column(Float, default=0.0)     # user-defined cushion ABOVE the firm's safety net
    payout_min = Column(Float, default=500.0)
    payout_caps = Column(String, nullable=True)          # comma-separated $ caps per payout #
    max_payouts = Column(Integer, default=0)             # 0 = unlimited
    min_trading_days_before_payout = Column(Integer, default=0)
    consistency_rule_pct = Column(Float, default=0.0)    # e.g. 0.30; 0 disables
    payout_preference = Column(String, default="max")    # 'min' | 'max' | 'custom'
    payout_preference_amount = Column(Float, default=0.0)  # used when preference='custom'

    # Contract limits (informational)
    contracts_micro_max = Column(Integer, default=0)
    contracts_mini_max = Column(Integer, default=0)

    # Lifecycle tracking
    status = Column(String, default="active")
    blown_date = Column(DateTime, nullable=True)
    blow_reason = Column(String, nullable=True)
    passed_eval_date = Column(DateTime, nullable=True)

    # Manual broker balance reconciliation. When set, the UI shows this alongside
    # the computed equity so the user can spot drift from withdrawals / fees /
    # resets that don't appear in trade data.
    broker_balance = Column(Float, nullable=True)
    broker_balance_updated_at = Column(DateTime, nullable=True)
    # High water mark from the broker (Sierra .data field 2005). Used as a floor
    # for the trailing peak in drawdown calculations so the DD floor reflects the
    # highest balance the broker has ever observed, not just what our trade log shows.
    broker_high_water_mark = Column(Float, nullable=True)
    # Cumulative P&L that happened before the trade data we have (e.g. trades from
    # before the Sierra export's start date, or balance carried over from a reset).
    # Added to starting_balance when computing current_equity so the broker matches.
    starting_balance_offset = Column(Float, default=0.0)
    reconciliation_note = Column(Text, nullable=True)

    executions = relationship("Execution", back_populates="account", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="account", cascade="all, delete-orphan")
    payouts = relationship("Payout", back_populates="account", cascade="all, delete-orphan")
    costs = relationship("AccountCost", back_populates="account", cascade="all, delete-orphan")


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    id = Column(Integer, primary_key=True)
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    row_count = Column(Integer, default=0)
    inserted_executions = Column(Integer, default=0)
    skipped_duplicates = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Strategy(Base):
    __tablename__ = "strategies"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String, default="#6ee7b7")  # hex
    archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    trades = relationship("Trade", back_populates="strategy")


class PropFirmDef(Base):
    """User-editable prop firm definitions (seeded from prop_firms.py on first boot)."""
    __tablename__ = "prop_firm_defs"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    label = Column(String, nullable=False)
    website = Column(String, nullable=True)
    default_drawdown_mode = Column(String, default="trailing_realtime")
    default_consistency_rule_pct = Column(Float, default=0.0)
    default_min_trading_days_before_payout = Column(Integer, default=0)
    default_payout_min = Column(Float, default=500.0)
    notes = Column(Text, nullable=True)
    verified_at = Column(String, nullable=True)
    archived = Column(Boolean, default=False)
    is_custom = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Promo / discount code currently usable for this firm. Informational —
    # apply manually when purchasing an account.
    discount_code = Column(String, nullable=True)
    discount_pct = Column(Float, default=0.0)         # 0..1
    discount_expires = Column(DateTime, nullable=True)
    # Fraction of profit the trader keeps post-funded. e.g. Tradeify 0.9 = 90%.
    trader_profit_split_pct = Column(Float, default=0.9)

    plans = relationship("PropFirmPlanDef", back_populates="firm", cascade="all, delete-orphan")


class PropFirmPlanDef(Base):
    __tablename__ = "prop_firm_plan_defs"
    id = Column(Integer, primary_key=True)
    firm_id = Column(Integer, ForeignKey("prop_firm_defs.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    account_size = Column(Float, nullable=False)
    profit_target = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    drawdown_mode_override = Column(String, nullable=True)  # null = use firm default
    safety_net_amount = Column(Float, default=0.0)
    payout_caps = Column(String, nullable=True)  # CSV
    max_payouts = Column(Integer, default=0)
    contracts_micro = Column(Integer, default=0)
    contracts_mini = Column(Integer, default=0)
    eval_cost_monthly = Column(Float, default=0.0)
    activation_fee = Column(Float, default=0.0)
    reset_fee = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    archived = Column(Boolean, default=False)

    firm = relationship("PropFirmDef", back_populates="plans")

    __table_args__ = (UniqueConstraint("firm_id", "key", name="uq_firm_plan_key"),)


class TradingPlaybook(Base):
    """A reusable trading-rules profile for groups of accounts (e.g. 'PA Conservative')."""
    __tablename__ = "trading_playbooks"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    account_group = Column(String, default="pa")  # 'pa' | 'eval' | 'all' | custom group label
    contracts = Column(Integer, default=1)
    stop_ticks = Column(Integer, default=10)
    target_ticks = Column(Integer, default=20)
    risk_dollars = Column(Float, default=0.0)  # if zero, computed from stop_ticks * instrument
    max_trades_per_day = Column(Integer, default=0)
    max_loss_per_day = Column(Float, default=0.0)
    archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSettings(Base):
    """Singleton settings row (id=1) for cross-cutting preferences and the multi-account plan."""
    __tablename__ = "user_settings"
    id = Column(Integer, primary_key=True)
    target_pa_count = Column(Integer, default=0)
    monthly_budget = Column(Float, default=0.0)
    preferred_firm_key = Column(String, nullable=True)
    preferred_plan_key = Column(String, nullable=True)
    dd_alert_threshold = Column(Float, default=0.0)
    pa_playbook_id = Column(Integer, ForeignKey("trading_playbooks.id"), nullable=True)
    eval_playbook_id = Column(Integer, ForeignKey("trading_playbooks.id"), nullable=True)
    # IANA timezone for date bucketing (Daily P&L, Calendar, etc.). Default = system UTC.
    timezone = Column(String, default="UTC")
    # Whether trade_date should follow entry_time or exit_time (TradeZella uses exit).
    date_by = Column(String, default="exit")    # 'exit' | 'entry'
    # Secondary currency for UI conversion display (USD numbers shown alongside
    # local-currency equivalent). FX rate is `USD * rate = secondary`.
    secondary_currency = Column(String, default="AUD")
    secondary_currency_fx_rate = Column(Float, default=1.5)


class MarketDataBar(Base):
    """OHLCV bar cache, populated by user uploads from Sierra Chart or auto-fetched."""
    __tablename__ = "market_data_bars"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)        # 'MNQM6.CME' (exact Sierra symbol)
    timeframe = Column(String, nullable=False, default="1m")   # '1m' | '5m' | '15m' | '1h' | '1d'
    ts = Column(DateTime, nullable=False, index=True)          # bar open time (UTC)
    o = Column(Float, nullable=False)
    h = Column(Float, nullable=False)
    l = Column(Float, nullable=False)
    c = Column(Float, nullable=False)
    v = Column(Float, default=0)
    source = Column(String, nullable=True)                     # 'sierra_upload' | 'yahoo' | 'manual'

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_bar"),
        Index("ix_bars_symbol_tf_ts", "symbol", "timeframe", "ts"),
    )


class TradeAttachment(Base):
    """Screenshots and other files attached to a trade."""
    __tablename__ = "trade_attachments"
    id = Column(Integer, primary_key=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    kind = Column(String, default="screenshot")   # 'screenshot' | 'chart' | 'other'
    size_bytes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class AccountCost(Base):
    """Money spent on a prop firm journey: eval subs, activation fees, resets, etc."""
    __tablename__ = "account_costs"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    # Costs aren't always tied to an account (e.g. monthly subs paid before passing eval).
    # account_id is nullable; we also store firm_key for unattached costs.
    prop_firm_key = Column(String, nullable=True)
    category = Column(String, nullable=False)             # eval_monthly | activation | reset | data_feed | platform | other
    amount = Column(Float, nullable=False)
    cost_date = Column(DateTime, nullable=False)        # one-shot date OR start date for recurring
    description = Column(Text, nullable=True)
    is_recurring = Column(Boolean, default=False, nullable=False)
    recurring_end_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="costs")


class Payout(Base):
    __tablename__ = "payouts"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    payout_date = Column(DateTime, nullable=False)
    amount = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="payouts")


class Execution(Base):
    __tablename__ = "executions"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    upload_batch_id = Column(Integer, ForeignKey("upload_batches.id"), nullable=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True, index=True)

    fill_id = Column(String, nullable=False)
    internal_order_id = Column(String, nullable=True)
    service_order_id = Column(String, nullable=True)
    parent_internal_order_id = Column(String, nullable=True)

    fill_time = Column(DateTime, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    instrument_root = Column(String, nullable=False, index=True)

    side = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    fill_price = Column(Float, nullable=False)
    raw_price = Column(Float, nullable=False)

    order_type = Column(String, nullable=True)
    open_close = Column(String, nullable=True)
    position_after = Column(Integer, nullable=True)

    high_during_position = Column(Float, nullable=True)
    low_during_position = Column(Float, nullable=True)
    note = Column(Text, nullable=True)
    is_automated = Column(Boolean, default=False)
    # Per-fill commission paid (USD). Tradovate fill exports carry this directly;
    # NinjaTrader exports include it in the Commission column. NULL means the
    # parser didn't have it — matching falls back to account-level rates.
    commission = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "fill_id", name="uq_account_fill"),
        Index("ix_exec_account_symbol_time", "account_id", "symbol", "fill_time"),
    )

    account = relationship("Account", back_populates="executions")
    trade = relationship("Trade", back_populates="executions")


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True, index=True)

    symbol = Column(String, nullable=False, index=True)
    instrument_root = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)

    entry_time = Column(DateTime, nullable=False, index=True)
    exit_time = Column(DateTime, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    avg_entry_price = Column(Float, nullable=False)
    avg_exit_price = Column(Float, nullable=False)

    gross_pnl = Column(Float, nullable=False)
    commissions = Column(Float, default=0.0)
    fees = Column(Float, default=0.0)
    net_pnl = Column(Float, nullable=False)

    point_value = Column(Float, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    mfe = Column(Float, nullable=True)
    mae = Column(Float, nullable=True)
    mfe_pnl = Column(Float, nullable=True)
    mae_pnl = Column(Float, nullable=True)

    # Trade journal
    notes = Column(Text, nullable=True)
    tags = Column(String, nullable=True)             # comma-separated freeform tags
    mistakes = Column(String, nullable=True)         # comma-separated mistake tags
    rating = Column(Integer, nullable=True)          # 1..5 stars

    # Planned levels — either price OR ticks (mode + value)
    planned_target_mode = Column(String, nullable=True)  # 'price' | 'ticks' | null
    planned_target_value = Column(Float, nullable=True)  # raw value in chosen mode
    planned_stop_mode = Column(String, nullable=True)
    planned_stop_value = Column(Float, nullable=True)
    # Cached derived metrics computed when planned levels saved
    planned_risk = Column(Float, nullable=True)          # $ amount risked
    planned_reward = Column(Float, nullable=True)        # $ amount targeted
    planned_rr = Column(Float, nullable=True)            # reward / risk
    realized_r = Column(Float, nullable=True)            # net_pnl / planned_risk

    trade_date = Column(DateTime, nullable=False, index=True)

    account = relationship("Account", back_populates="trades")
    strategy = relationship("Strategy", back_populates="trades")
    executions = relationship("Execution", back_populates="trade")
