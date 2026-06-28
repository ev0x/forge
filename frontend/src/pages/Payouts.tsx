import { useEffect, useMemo, useState } from 'react'
import { api, Account, Payout, PropStatus, UserSettings, PayoutForecast, fmtUsd } from '../lib/api'
import PayoutForecastWidget from '../components/PayoutForecast'
import PayoutsCalendar from '../components/PayoutsCalendar'

type CurrencyMode = 'USD' | 'BOTH'

export default function Payouts({ accounts }: { accounts: Account[] }) {
  const [payouts, setPayouts] = useState<Payout[]>([])
  const [statuses, setStatuses] = useState<Record<number, PropStatus>>({})
  const [creating, setCreating] = useState(false)
  const [settings, setSettings] = useState<UserSettings | null>(null)
  const [forecast, setForecast] = useState<PayoutForecast | null>(null)
  const [horizonDate, setHorizonDate] = useState<string>(() => {
    const d = new Date(); d.setMonth(d.getMonth() + 1)
    return d.toISOString().slice(0, 10)
  })
  const [taxPct, setTaxPct] = useState<number>(() => {
    const v = parseFloat(localStorage.getItem('payouts.taxPct') || '')
    return Number.isFinite(v) && v >= 0 && v <= 100 ? v : 30
  })
  useEffect(() => { localStorage.setItem('payouts.taxPct', String(taxPct)) }, [taxPct])
  const [currencyMode, setCurrencyMode] = useState<CurrencyMode>(() =>
    (localStorage.getItem('payouts.currencyMode') as CurrencyMode) || 'USD'
  )
  useEffect(() => { localStorage.setItem('payouts.currencyMode', currencyMode) }, [currencyMode])
  useEffect(() => { api.settings.get().then(setSettings) }, [])
  useEffect(() => { api.plan.forecast().then(setForecast) }, [])

  // Summarise the next-payout window: the soonest predicted payout, plus the
  // accumulated total/yours up to the user-picked horizon date.
  const summary = useMemo(() => {
    if (!forecast || forecast.all_predicted.length === 0) return null
    const sorted = [...forecast.all_predicted]
      .sort((a, b) => new Date(a.predicted_date).getTime() - new Date(b.predicted_date).getTime())
    const next = sorted[0]
    const now = Date.now()
    const nextDate = new Date(next.predicted_date).getTime()
    const daysToNext = Math.max(0, Math.ceil((nextDate - now) / (24 * 60 * 60 * 1000)))
    const horizonMs = new Date(horizonDate + 'T23:59:59').getTime()
    const inRange = sorted.filter(p => new Date(p.predicted_date).getTime() <= horizonMs)
    return {
      next, daysToNext,
      horizonTotal: inRange.reduce((a, p) => a + p.amount, 0),
      horizonYours: inRange.reduce((a, p) => a + (p.amount_to_trader ?? p.amount), 0),
      horizonCount: inRange.length,
    }
  }, [forecast, horizonDate])
  const secondary = settings?.secondary_currency || 'AUD'
  const fx = settings?.secondary_currency_fx_rate || 1.5
  function fmtSec(usd: number): string {
    return `${secondary} ${fmtUsd(usd * fx).replace(/^\$/, '')}`
  }
  function fmtUsdPair(usd: number): string {
    return currencyMode === 'BOTH' ? `${fmtUsd(usd)} · ${fmtSec(usd)}` : fmtUsd(usd)
  }

  // Only PA / funded accounts can take payouts. Eval accounts — even ones that
  // have passed_eval status — still need activation/PA conversion before any
  // payout, so skip them here. Also skip blown / closed.
  const payoutAccounts = accounts.filter(a =>
    (a.account_type === 'pa' || a.account_type === 'funded')
    && a.status !== 'blown' && a.status !== 'closed'
  )

  const [form, setForm] = useState({
    account_id: payoutAccounts[0]?.id || 0,
    payout_date: new Date().toISOString().slice(0, 10),
    amount: 0,
    notes: '',
  })

  async function refresh() {
    setPayouts(await api.payouts.list())
    Promise.all(payoutAccounts.map(a => api.accounts.propStatus(a.id))).then(rows => {
      const m: Record<number, PropStatus> = {}
      rows.forEach(r => { m[r.account_id] = r })
      setStatuses(m)
    })
  }
  useEffect(() => { refresh() }, [payoutAccounts.map(a => a.id).join(',')]) // eslint-disable-line

  async function create() {
    if (!form.account_id || !form.amount) return
    await api.payouts.create({
      account_id: form.account_id,
      payout_date: new Date(form.payout_date).toISOString(),
      amount: form.amount,
      notes: form.notes || undefined,
    })
    setForm({ ...form, amount: 0, notes: '' }); setCreating(false); refresh()
  }
  async function remove(id: number) {
    if (!confirm('Delete this payout?')) return
    await api.payouts.delete(id); refresh()
  }

  const acctMap = Object.fromEntries(accounts.map(a => [a.id, a]))
  // Restrict the visible payout history to PA/funded too (so blown/eval accounts don't clutter)
  const visibleAccountIds = new Set(payoutAccounts.map(a => a.id))
  const visiblePayouts = payouts.filter(p => visibleAccountIds.has(p.account_id))
  const totalPaid = visiblePayouts.reduce((acc, p) => acc + p.amount, 0)
  const totalByAccount = visiblePayouts.reduce((acc, p) => {
    acc[p.account_id] = (acc[p.account_id] || 0) + p.amount
    return acc
  }, {} as Record<number, number>)

  async function saveFx(curr: string, rate: number) {
    if (!settings) return
    const updated = await api.settings.update({ secondary_currency: curr, secondary_currency_fx_rate: rate })
    setSettings(updated)
  }

  return (
    <div className="p-6 space-y-6">
      {/* Currency / FX rate quick-edit */}
      {settings && (
        <div className="bg-panel border border-border rounded-lg p-3 flex items-center gap-3 flex-wrap text-xs">
          <span className="text-muted uppercase tracking-wider">Local currency</span>
          <select value={settings.secondary_currency}
            onChange={e => saveFx(e.target.value, settings.secondary_currency_fx_rate)}
            className="bg-panel2 border border-border rounded px-2 py-1 text-sm">
            {['AUD','GBP','EUR','CAD','NZD','JPY','CHF','SGD','HKD'].map(c =>
              <option key={c} value={c}>{c}</option>
            )}
            {!['AUD','GBP','EUR','CAD','NZD','JPY','CHF','SGD','HKD'].includes(settings.secondary_currency) &&
              <option value={settings.secondary_currency}>{settings.secondary_currency}</option>}
          </select>
          <span className="text-muted">FX rate: USD ×</span>
          <input type="number" step="0.0001" min="0"
            defaultValue={settings.secondary_currency_fx_rate}
            onBlur={e => {
              const v = parseFloat(e.target.value)
              if (!isNaN(v) && v > 0 && v !== settings.secondary_currency_fx_rate) {
                saveFx(settings.secondary_currency, v)
              }
            }}
            className="bg-panel2 border border-border rounded px-2 py-1 text-sm num w-24" />
          <span className="text-muted">= 1 {settings.secondary_currency}</span>
          <span className="text-[10px] text-muted ml-auto">Applies to forecast + payouts here and on the dashboard.</span>
        </div>
      )}

      {/* Next payout + horizon picker */}
      {summary ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="bg-gradient-to-br from-panel to-panel2/30 border border-accent/30 rounded-xl p-4">
            <div className="text-[10px] text-muted uppercase tracking-wider">Next payout</div>
            <div className="text-2xl font-bold text-accent num mt-1">
              {summary.daysToNext === 0 ? 'today' : `in ${summary.daysToNext}d`}
            </div>
            <div className="text-xs text-muted num mt-0.5">
              {new Date(summary.next.predicted_date).toLocaleDateString(undefined, {
                weekday: 'short', month: 'short', day: 'numeric',
              })} · {summary.next.account_name}
            </div>
            <div className="text-sm font-semibold mt-2 num text-text">
              {fmtUsdPair(summary.next.amount)}
              {summary.next.trader_split_pct < 1 && (
                <span className="ml-1 text-[11px] text-win">
                  yours {fmtUsdPair(summary.next.amount_to_trader)}
                </span>
              )}
            </div>
          </div>
          <div className="bg-panel border border-border rounded-xl p-4 md:col-span-2">
            <div className="flex items-center justify-between flex-wrap gap-3 mb-2">
              <div>
                <div className="text-[10px] text-muted uppercase tracking-wider">
                  Potential payouts from now until…
                </div>
                <input type="date" value={horizonDate}
                  min={new Date().toISOString().slice(0, 10)}
                  onChange={e => setHorizonDate(e.target.value)}
                  className="mt-1 bg-panel2 border border-border rounded px-2 py-1 text-sm" />
              </div>
              <div className="text-right">
                <div className="text-[10px] text-muted uppercase">Gross total</div>
                <div className="text-2xl font-bold text-win num">{fmtUsdPair(summary.horizonTotal)}</div>
                {summary.horizonYours < summary.horizonTotal - 1 && (
                  <div className="text-[11px] text-win/80 num">
                    yours: {fmtUsdPair(summary.horizonYours)}
                  </div>
                )}
                <div className="text-[10px] text-muted mt-1">{summary.horizonCount} payout{summary.horizonCount !== 1 ? 's' : ''} expected</div>
              </div>
            </div>
            {/* Tax split — applied to your-after-firm-split amount */}
            <div className="flex items-center gap-3 my-3 px-3 py-2 bg-panel2/40 border border-border rounded text-xs flex-wrap">
              <label className="text-muted">Tax set-aside</label>
              <div className="flex items-center gap-1">
                <input type="number" min={0} max={100} step={0.5} value={taxPct}
                  onChange={e => setTaxPct(Math.max(0, Math.min(100, parseFloat(e.target.value) || 0)))}
                  className="w-16 bg-bg border border-border rounded px-2 py-0.5 text-sm num text-right" />
                <span className="text-muted">%</span>
              </div>
              <div className="flex gap-1">
                {[0, 15, 25, 30, 35, 47].map(p => (
                  <button key={p} onClick={() => setTaxPct(p)}
                    className={`px-1.5 py-0.5 rounded text-[10px] ${
                      taxPct === p ? 'bg-accent text-bg' : 'bg-panel border border-border text-muted hover:text-text'
                    }`}>
                    {p}%
                  </button>
                ))}
              </div>
              {(() => {
                const taxBase = summary.horizonYours    // tax applies to the trader's share after the firm's split
                const tax = taxBase * (taxPct / 100)
                const afterTax = taxBase - tax
                return (
                  <div className="ml-auto flex gap-4 text-right">
                    <div>
                      <div className="text-[10px] text-muted uppercase">Set aside</div>
                      <div className="num text-warn font-semibold">{fmtUsdPair(tax)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-muted uppercase">Take home</div>
                      <div className="num text-win font-semibold">{fmtUsdPair(afterTax)}</div>
                    </div>
                  </div>
                )
              })()}
            </div>
            <div className="flex gap-1.5 text-[11px] flex-wrap">
              {([
                ['1w', 7], ['2w', 14], ['1m', 30], ['2m', 60], ['3m', 90], ['6m', 180],
              ] as Array<[string, number]>).map(([label, days]) => {
                const d = new Date(); d.setDate(d.getDate() + days)
                const iso = d.toISOString().slice(0, 10)
                const active = iso === horizonDate
                return (
                  <button key={label} onClick={() => setHorizonDate(iso)}
                    className={`px-2 py-0.5 rounded border ${
                      active ? 'border-accent bg-accent/15 text-accent'
                             : 'border-border bg-panel2 text-muted hover:text-text'
                    }`}>
                    {label}
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      ) : null}

      <PayoutForecastWidget />

      <PayoutsCalendar accounts={accounts} />

      {/* Per-account next-payout cards */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold">Upcoming Payouts</div>
          <div className="flex items-center gap-1 bg-panel border border-border rounded-md p-1 text-xs">
            <button onClick={() => setCurrencyMode('USD')}
              className={`px-2.5 py-0.5 rounded ${currencyMode === 'USD' ? 'bg-panel2 text-text' : 'text-muted hover:text-text'}`}>USD</button>
            <button onClick={() => setCurrencyMode('BOTH')}
              className={`px-2.5 py-0.5 rounded ${currencyMode === 'BOTH' ? 'bg-panel2 text-text' : 'text-muted hover:text-text'}`}
              title={`Show USD and ${secondary} side-by-side (FX ${fx})`}>
              USD / {secondary}
            </button>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {Object.values(statuses).filter(s => s.payout_max_for_next > 0 || s.payout_min > 0).map(s => (
            <div key={s.account_id} className={`bg-panel border rounded-lg p-4 ${s.eligible_for_payout ? 'border-accent/60' : 'border-border'}`}>
              <div className="flex items-center justify-between mb-1.5 gap-2 flex-wrap">
                <div className="text-sm font-medium">{acctMap[s.account_id]?.display_name}</div>
                <div className="flex gap-1.5">
                  {s.is_final_payout && <span className="text-[10px] uppercase bg-warn/20 text-warn px-1.5 py-0.5 rounded">FINAL TIER</span>}
                  {s.eligible_for_payout && <span className="text-[10px] uppercase bg-accent/20 text-accent px-1.5 py-0.5 rounded">Ready</span>}
                </div>
              </div>
              <div className="text-xs text-muted">
                {s.prop_firm} · {s.account_type} · payout #{s.next_payout_number}
                {s.max_payouts > 0 && ` of ${s.max_payouts}`}
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
                {(() => {
                  const acct = acctMap[s.account_id]
                  const usesBroker = acct?.broker_balance != null
                  const delta = s.current_equity - s.starting_balance
                  return (
                    <div>
                      <div className="text-[10px] text-muted uppercase flex items-center gap-1">
                        Current Balance
                        {usesBroker && <span className="text-[8px] uppercase tracking-wider bg-accent/15 text-accent px-1 rounded">broker</span>}
                      </div>
                      <div className={`font-semibold num ${delta > 0 ? 'text-win' : delta < 0 ? 'text-loss' : 'text-text'}`}>
                        {fmtUsdPair(s.current_equity)}
                      </div>
                      <div className="text-[9px] text-muted num">
                        start {fmtUsd(s.starting_balance)} ·{' '}
                        <span className={delta > 0 ? 'text-win' : delta < 0 ? 'text-loss' : 'text-muted'}>
                          {fmtUsd(delta, { signed: true })}
                        </span>
                      </div>
                    </div>
                  )
                })()}
                <div>
                  <div className="text-[10px] text-muted uppercase">Est. Date</div>
                  <div className="font-semibold">{s.predicted_next_payout_date ? new Date(s.predicted_next_payout_date).toLocaleDateString() : '—'}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase">Next Payout Amount</div>
                  <div className="font-semibold num text-accent">
                    {fmtUsdPair(s.payout_amount_for_next)}
                    {s.payout_amount_label && (
                      <span className="ml-1 text-[10px] uppercase tracking-wider text-muted">({s.payout_amount_label})</span>
                    )}
                  </div>
                  {s.trader_profit_split_pct < 1 && (
                    <div className="text-[10px] text-muted num">
                      yours: <span className="text-win">{fmtUsdPair(s.payout_amount_to_trader)}</span>
                      <span className="ml-1">({Math.round(s.trader_profit_split_pct * 100)}% split)</span>
                    </div>
                  )}
                  <div className="text-[9px] text-muted">cap {fmtUsd(s.payout_max_for_next)} · min {fmtUsd(s.payout_min)}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase">
                    {s.is_final_payout ? 'Required Post-Balance (final)' : 'Required Post-Balance'}
                  </div>
                  <div className="num">{fmtUsd(s.safety_net_balance)}</div>
                  <div className="text-[9px] text-muted">
                    start + firm SN{s.is_final_payout ? '' : ` + buffer ${fmtUsd(s.extra_safety_buffer)}`}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase">Distance to Payout</div>
                  <div className="num">
                    {fmtUsd(s.distance_to_next_payout)}
                    <span className="ml-1 text-[10px] text-muted">({s.payout_amount_label || 'Max'})</span>
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase">Avg/day (recent)</div>
                  <div className={`num ${s.avg_daily_pnl_recent > 0 ? 'text-win' : s.avg_daily_pnl_recent < 0 ? 'text-loss' : 'text-muted'}`}>
                    {fmtUsd(s.avg_daily_pnl_recent, { signed: true })}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase">Trading days</div>
                  <div className="num">{s.trading_days_used}{s.min_trading_days_before_payout > 0 ? `/${s.min_trading_days_before_payout}` : ''}</div>
                </div>
              </div>
              {s.eligibility_reason && (
                <div className="text-[10px] text-warn mt-2">⚠ {s.eligibility_reason}</div>
              )}
            </div>
          ))}
        </div>
        {!Object.values(statuses).filter(s => s.payout_max_for_next > 0 || s.payout_min > 0).length && (
          <div className="bg-panel border border-border rounded-lg p-4 text-sm text-muted">
            {payoutAccounts.length === 0
              ? 'No active PA / funded accounts. Mark an account type as "pa" or "funded" to see payouts here.'
              : 'Apply a prop firm preset on an account to see payout predictions here.'}
          </div>
        )}
      </div>

      {/* Per-account payout totals */}
      {Object.keys(totalByAccount).length > 0 && (
        <div>
          <div className="text-sm font-semibold mb-2">Total Payouts</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="bg-panel border border-accent/40 rounded-lg p-3">
              <div className="text-[10px] text-muted uppercase tracking-wider">All accounts</div>
              <div className="text-2xl font-bold text-win num">{fmtUsd(totalPaid)}</div>
              <div className="text-[10px] text-muted">{payouts.length} payouts</div>
            </div>
            {Object.entries(totalByAccount).map(([aid, total]) => {
              const count = payouts.filter(p => p.account_id === Number(aid)).length
              return (
                <div key={aid} className="bg-panel border border-border rounded-lg p-3">
                  <div className="text-[10px] text-muted uppercase tracking-wider truncate">{acctMap[Number(aid)]?.display_name}</div>
                  <div className="text-xl font-semibold text-win num">{fmtUsd(total)}</div>
                  <div className="text-[10px] text-muted">{count} payout{count !== 1 ? 's' : ''}</div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Recorded payouts */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold">Recorded Payouts <span className="text-muted">· total {fmtUsd(totalPaid)}</span></div>
          {!creating && (
            <button onClick={() => setCreating(true)}
              className="bg-accent text-bg px-3 py-1.5 rounded text-sm">+ Record Payout</button>
          )}
        </div>
        {creating && (
          <div className="bg-panel border border-border rounded-lg p-4 mb-3 grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
            <div>
              <label className="text-xs text-muted">Account</label>
              <select value={form.account_id} onChange={e => setForm({ ...form, account_id: Number(e.target.value) })}
                className="w-full mt-1 bg-panel2 border border-border rounded px-2 py-1.5 text-sm">
                {accounts.map(a => <option key={a.id} value={a.id}>{a.display_name}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs text-muted">Date</label>
              <input type="date" value={form.payout_date} onChange={e => setForm({ ...form, payout_date: e.target.value })}
                className="w-full mt-1 bg-panel2 border border-border rounded px-2 py-1.5 text-sm" />
            </div>
            <div>
              <label className="text-xs text-muted">Amount $</label>
              <input type="number" step="0.01" value={form.amount} onChange={e => setForm({ ...form, amount: parseFloat(e.target.value) || 0 })}
                className="w-full mt-1 bg-panel2 border border-border rounded px-2 py-1.5 text-sm num" />
            </div>
            <div className="flex gap-2">
              <button onClick={create} className="bg-accent text-bg px-3 py-1.5 rounded text-sm flex-1">Save</button>
              <button onClick={() => setCreating(false)} className="text-muted text-sm px-3">Cancel</button>
            </div>
            <input value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })}
              placeholder="Notes (optional)" className="md:col-span-4 bg-panel2 border border-border rounded px-2 py-1.5 text-sm" />
          </div>
        )}
        <div className="bg-panel border border-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-panel2 text-xs uppercase text-muted">
              <tr>
                <th className="px-3 py-2 text-left">Date</th>
                <th className="px-3 py-2 text-left">Account</th>
                <th className="px-3 py-2 text-right">Amount</th>
                <th className="px-3 py-2 text-right">Yours (after split)</th>
                <th className="px-3 py-2 text-left">Notes</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {payouts.map(p => {
                const split = statuses[p.account_id]?.trader_profit_split_pct ?? 1
                const yours = p.amount * split
                return (
                  <tr key={p.id} className="border-t border-border">
                    <td className="px-3 py-2">{new Date(p.payout_date).toLocaleDateString()}</td>
                    <td className="px-3 py-2">{acctMap[p.account_id]?.display_name || p.account_id}</td>
                    <td className="px-3 py-2 text-right num text-win font-medium">{fmtUsdPair(p.amount)}</td>
                    <td className="px-3 py-2 text-right num text-win">
                      {fmtUsdPair(yours)}
                      {split < 1 && <span className="ml-1 text-[10px] text-muted">({Math.round(split * 100)}%)</span>}
                    </td>
                    <td className="px-3 py-2 text-muted text-xs">{p.notes || ''}</td>
                    <td className="px-3 py-2 text-right">
                      <button onClick={() => remove(p.id)} className="text-xs text-loss/80 hover:text-loss">Delete</button>
                    </td>
                  </tr>
                )
              })}
              {!payouts.length && <tr><td colSpan={6} className="px-3 py-6 text-center text-muted">No payouts recorded yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
