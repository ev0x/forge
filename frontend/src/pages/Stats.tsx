import { useEffect, useMemo, useState } from 'react'
import { Account, FullStats, api, fmtUsd, fmtPct } from '../lib/api'

/**
 * Stats comparison page. Loads /api/stats once per selected account and once
 * combined, then displays a side-by-side table of headline performance metrics:
 * win rate, profit factor, expectancy, Sharpe, Sortino, consistency, and a
 * "psychology" score derived from the existing insight composite.
 */
export default function Stats({ accountIds, accounts }: { accountIds?: number[]; accounts: Account[] }) {
  // Resolve the actual list of accounts the user is looking at right now.
  const visibleAccounts = useMemo(() => {
    if (!accountIds) return accounts
    if (accountIds.length === 1 && accountIds[0] === -1) return []
    return accounts.filter(a => accountIds.includes(a.id))
  }, [accountIds, accounts])

  const [perAccount, setPerAccount] = useState<Record<number, FullStats>>({})
  const [combined, setCombined] = useState<FullStats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true); setPerAccount({}); setCombined(null)
    if (visibleAccounts.length === 0) {
      setLoading(false); return
    }
    // Combined first, then each account in parallel.
    Promise.all([
      api.stats({ account_ids: visibleAccounts.map(a => a.id) }),
      ...visibleAccounts.map(a => api.stats({ account_ids: [a.id] }).then(s => ({ id: a.id, s }))),
    ]).then(results => {
      if (cancelled) return
      const [comb, ...rest] = results as [FullStats, ...Array<{ id: number; s: FullStats }>]
      setCombined(comb)
      const map: Record<number, FullStats> = {}
      rest.forEach(r => { map[r.id] = r.s })
      setPerAccount(map)
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [visibleAccounts.map(a => a.id).join(',')]) // eslint-disable-line

  if (loading && !combined) return <div className="p-6 text-muted">Loading stats…</div>
  if (visibleAccounts.length === 0) {
    return <div className="p-6 text-muted">
      Select one or more accounts in the top-right picker to see their stats here.
    </div>
  }
  if (!combined) return <div className="p-6 text-muted">No stats available.</div>

  // Columns: combined first, then each account.
  const columns: Array<{ key: string; label: string; stats: FullStats; startingBalance: number; subtitle?: string }> = [
    {
      key: 'combined',
      label: `Combined (${visibleAccounts.length})`,
      stats: combined,
      startingBalance: visibleAccounts.reduce((a, x) => a + (x.starting_balance || 0), 0),
      subtitle: 'all selected accounts',
    },
    ...visibleAccounts.map(a => ({
      key: `acct-${a.id}`,
      label: a.display_name,
      stats: perAccount[a.id],
      startingBalance: a.starting_balance || 0,
      subtitle: a.prop_firm || a.account_type,
    })).filter(c => c.stats),
  ]

  // Build metric rows. Each row is a label + a computer that takes (stats, startingBalance) → cell.
  const rows: Row[] = [
    { label: 'Net P&L',         tone: 'pnl',     get: s => fmtUsd(s.cards.net_pnl, { signed: true }) },
    { label: 'Return %',        tone: 'pct-pnl', get: (s, sb) => fmtPct(sb > 0 ? s.cards.net_pnl / sb : 0, 2),
                                signFromValue: (s, sb) => sb > 0 ? s.cards.net_pnl / sb : 0 },
    { label: 'Trade count',     get: s => String(s.cards.trade_count) },
    { label: 'Win rate',        tone: 'rate',    get: s => fmtPct(s.cards.win_rate, 1),
                                signFromValue: s => s.cards.win_rate - 0.5 },
    { label: 'Day win rate',    tone: 'rate',    get: s => fmtPct(s.cards.day_win_rate, 1),
                                signFromValue: s => s.cards.day_win_rate - 0.5 },
    { label: 'Profit factor',   tone: 'pf',      get: s => s.cards.profit_factor === 0 ? '—'
                                                            : s.cards.profit_factor >= 99 ? '∞'
                                                            : s.cards.profit_factor.toFixed(2),
                                signFromValue: s => s.cards.profit_factor - 1 },
    { label: 'Payoff ratio',    tone: 'pf',      get: s => s.cards.payoff_ratio.toFixed(2),
                                signFromValue: s => s.cards.payoff_ratio - 1 },
    { label: 'Avg win',         tone: 'pos',     get: s => fmtUsd(s.cards.avg_win) },
    { label: 'Avg loss',        tone: 'neg',     get: s => fmtUsd(Math.abs(s.cards.avg_loss)) },
    { label: 'Expectancy / trade', tone: 'pnl',  get: s => fmtUsd(s.cards.expectancy, { signed: true }),
                                signFromValue: s => s.cards.expectancy },
    { label: 'Expectancy (R)',  tone: 'pnl',     get: s => s.cards.expectancy_r != null
                                                            ? `${s.cards.expectancy_r >= 0 ? '+' : ''}${s.cards.expectancy_r.toFixed(2)}R`
                                                            : '—',
                                signFromValue: s => s.cards.expectancy_r ?? 0 },
    { label: 'Sharpe (annualised)', tone: 'sharpe', get: (s, sb) => sharpe(dailyReturns(s, sb)).toFixed(2),
                                signFromValue: (s, sb) => sharpe(dailyReturns(s, sb)) - 1 },
    { label: 'Sortino (annualised)', tone: 'sharpe', get: (s, sb) => {
                                  const v = sortino(dailyReturns(s, sb))
                                  return v === Infinity ? '∞' : v.toFixed(2)
                                },
                                signFromValue: (s, sb) => {
                                  const v = sortino(dailyReturns(s, sb))
                                  return v === Infinity ? 999 : v - 1.5
                                } },
    { label: 'Consistency',     tone: 'consistency', get: s => fmtPct(consistencyScore(s), 0),
                                signFromValue: s => consistencyScore(s) - 0.7,
                                tooltip: 'Higher = profit comes from many days, not a single outlier. 100% = perfectly even. <30% = one day made nearly all the P&L.' },
    { label: 'Max drawdown',    tone: 'neg-num', get: s => fmtUsd(Math.abs(s.cards.max_drawdown)) },
    { label: 'Max DD %',        tone: 'neg-num', get: s => fmtPct(Math.abs(s.cards.max_drawdown_pct), 1) },
    { label: 'Psychology score', tone: 'insight', get: s => `${s.insight.score.toFixed(0)} / 100`,
                                subtitle: s => s.insight.grade,
                                signFromValue: s => s.insight.score - 50 },
    { label: 'Plan adherence',  tone: 'rate',    get: s => s.cards.plan_adherence != null
                                                            ? fmtPct(s.cards.plan_adherence, 0) : '—',
                                signFromValue: s => (s.cards.plan_adherence ?? 0) - 0.5 },
    { label: 'Mistake rate',    tone: 'rate-inv', get: s => s.cards.mistake_rate != null
                                                            ? fmtPct(s.cards.mistake_rate, 0) : '—',
                                signFromValue: s => -(s.cards.mistake_rate ?? 0) },
    { label: 'Avg rating',      tone: 'pos',     get: s => s.cards.avg_rating != null
                                                            ? `${s.cards.avg_rating.toFixed(1)}★` : '—',
                                signFromValue: s => (s.cards.avg_rating ?? 0) - 3 },
    { label: 'Avg daily P&L',   tone: 'pnl',     get: s => fmtUsd(s.cards.avg_daily_pnl, { signed: true }),
                                signFromValue: s => s.cards.avg_daily_pnl },
    { label: 'Trading days',    get: s => String(s.cards.trading_days) },
    { label: 'Commissions',     tone: 'neg-num', get: s => fmtUsd(s.cards.total_commissions) },
  ]

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm font-semibold">Stats — per account vs combined</div>
          <div className="text-xs text-muted">
            Comparing {visibleAccounts.length} selected account{visibleAccounts.length !== 1 ? 's' : ''} against the combined portfolio.
            Sharpe / Sortino are annualised (×√252) using daily returns vs starting balance.
          </div>
        </div>
      </div>

      <div className="overflow-x-auto bg-panel border border-border rounded-lg">
        <table className="w-full text-sm">
          <thead className="bg-panel2 text-xs uppercase text-muted">
            <tr>
              <th className="px-3 py-2 text-left sticky left-0 bg-panel2 z-10 min-w-[200px]">Metric</th>
              {columns.map(c => (
                <th key={c.key} className={`px-3 py-2 text-right min-w-[140px] ${
                  c.key === 'combined' ? 'bg-accent/10 text-accent' : ''
                }`}>
                  <div className="truncate" title={c.label}>{c.label}</div>
                  {c.subtitle && <div className="text-[10px] text-muted normal-case">{c.subtitle}</div>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.label} className="border-t border-border">
                <td className="px-3 py-2 text-muted sticky left-0 bg-panel z-10" title={row.tooltip}>
                  {row.label}{row.tooltip && <span className="ml-1 text-[10px] opacity-50">ℹ</span>}
                </td>
                {columns.map(c => {
                  const value = row.get(c.stats, c.startingBalance)
                  const signal = row.signFromValue?.(c.stats, c.startingBalance) ?? 0
                  const tone = colorForRow(row, signal, c.stats)
                  const subtitle = row.subtitle?.(c.stats)
                  return (
                    <td key={c.key} className={`px-3 py-2 text-right num ${tone} ${
                      c.key === 'combined' ? 'bg-accent/5 font-semibold' : ''
                    }`}>
                      {value}
                      {subtitle && <div className="text-[10px] text-muted">{subtitle}</div>}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="text-[10px] text-muted">
        Sharpe / Sortino assume risk-free rate = 0. They use $ daily P&L divided by starting balance as the daily return,
        annualised by √252 trading days. Consistency is 1 − (largest winning day ÷ sum of winning days);
        100% means no single day dominates.
      </div>
    </div>
  )
}


type Row = {
  label: string
  tone?: string
  tooltip?: string
  get: (s: FullStats, startingBalance: number) => string
  signFromValue?: (s: FullStats, startingBalance: number) => number
  subtitle?: (s: FullStats) => string
}

function colorForRow(row: Row, signal: number, _s: FullStats): string {
  if (row.tone === 'pos') return 'text-win'
  if (row.tone === 'neg' || row.tone === 'neg-num') return 'text-loss/90'
  if (row.tone === 'pnl' || row.tone === 'pct-pnl') {
    if (signal > 0) return 'text-win'
    if (signal < 0) return 'text-loss'
    return 'text-text'
  }
  if (row.tone === 'rate' || row.tone === 'pf' || row.tone === 'consistency' || row.tone === 'insight') {
    if (signal > 0.05) return 'text-win'
    if (signal < -0.05) return 'text-loss'
    return 'text-text'
  }
  if (row.tone === 'rate-inv') {
    // For mistake rate: lower is better
    if (signal > 0) return 'text-win'
    if (signal < -0.2) return 'text-loss'
    return 'text-text'
  }
  if (row.tone === 'sharpe') {
    if (signal > 0) return 'text-win'
    if (signal < 0) return 'text-loss'
    return 'text-text'
  }
  return 'text-text'
}

// Daily returns as fraction of starting balance.
function dailyReturns(s: FullStats, startingBalance: number): number[] {
  if (!s.daily?.length || startingBalance <= 0) return []
  return s.daily.map(d => d.net_pnl / startingBalance)
}

function sharpe(returns: number[]): number {
  if (returns.length < 2) return 0
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length
  const variance = returns.reduce((a, b) => a + (b - mean) ** 2, 0) / (returns.length - 1)
  const std = Math.sqrt(variance)
  if (std === 0) return 0
  return (mean / std) * Math.sqrt(252)
}

function sortino(returns: number[]): number {
  if (returns.length < 2) return 0
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length
  const downside = returns.filter(r => r < 0)
  if (downside.length === 0) return Infinity
  const downsideVar = downside.reduce((a, b) => a + b * b, 0) / downside.length
  const downsideStd = Math.sqrt(downsideVar)
  if (downsideStd === 0) return 0
  return (mean / downsideStd) * Math.sqrt(252)
}

function consistencyScore(s: FullStats): number {
  if (!s.daily?.length) return 0
  const positive = s.daily.filter(d => d.net_pnl > 0)
  if (positive.length === 0) return 0
  const totalPositive = positive.reduce((a, d) => a + d.net_pnl, 0)
  const largest = Math.max(...positive.map(d => d.net_pnl))
  if (totalPositive <= 0) return 0
  return 1 - largest / totalPositive
}
