import { useEffect, useState } from 'react'
import { api, PayoutForecast, UserSettings, fmtUsd } from '../lib/api'

type CurrencyMode = 'USD' | 'BOTH'

export default function PayoutForecastWidget() {
  const [data, setData] = useState<PayoutForecast | null>(null)
  const [settings, setSettings] = useState<UserSettings | null>(null)
  const [currencyMode, setCurrencyMode] = useState<CurrencyMode>(() =>
    (localStorage.getItem('forecast.currencyMode') as CurrencyMode) || 'USD'
  )
  useEffect(() => { localStorage.setItem('forecast.currencyMode', currencyMode) }, [currencyMode])
  useEffect(() => { api.plan.forecast().then(setData) }, [])
  useEffect(() => { api.settings.get().then(setSettings) }, [])
  if (!data) return null

  const secondary = settings?.secondary_currency || 'AUD'
  const fx = settings?.secondary_currency_fx_rate || 1.5
  const showSplit = data.total_next_6_months_to_trader < data.total_next_6_months - 1
  const fmtSec = (usd: number) => `${secondary} ${fmtUsd(usd * fx).replace(/^\$/, '')}`
  const fmtPair = (usd: number) => currencyMode === 'BOTH' ? `${fmtUsd(usd)} · ${fmtSec(usd)}` : fmtUsd(usd)

  return (
    <div className="bg-gradient-to-br from-panel to-panel2/30 border border-border rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-muted">Forecast</div>
          <div className="text-sm font-semibold mt-0.5">
            Expected payouts over the next 6 months
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 bg-panel border border-border rounded-md p-1 text-xs">
            <button onClick={() => setCurrencyMode('USD')}
              className={`px-2.5 py-0.5 rounded ${currencyMode === 'USD' ? 'bg-panel2 text-text' : 'text-muted hover:text-text'}`}>USD</button>
            <button onClick={() => setCurrencyMode('BOTH')}
              className={`px-2.5 py-0.5 rounded ${currencyMode === 'BOTH' ? 'bg-panel2 text-text' : 'text-muted hover:text-text'}`}
              title={`Show USD and ${secondary} side-by-side (FX ${fx})`}>USD / {secondary}</button>
          </div>
          <div className="text-right">
            <div className="text-[10px] text-muted uppercase">6-month total</div>
            <div className="text-2xl font-bold text-accent num">{fmtPair(data.total_next_6_months)}</div>
            {showSplit && (
              <div className="text-[11px] text-win num">
                yours: {fmtPair(data.total_next_6_months_to_trader)}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-9 gap-2">
        {data.buckets.map(b => (
          <div key={b.label} className="bg-panel border border-border rounded-md p-2.5">
            <div className="text-[10px] text-muted uppercase tracking-wider">{b.label}</div>
            <div className={`num font-semibold mt-1 ${b.total > 0 ? 'text-win' : 'text-muted'}`}>
              {b.total > 0 ? fmtPair(b.total) : '—'}
            </div>
            {b.total_to_trader > 0 && b.total_to_trader < b.total - 0.01 && (
              <div className="text-[10px] num text-win/80">yours {fmtPair(b.total_to_trader)}</div>
            )}
            <div className="text-[10px] text-muted mt-0.5">{b.payouts.length} payout{b.payouts.length !== 1 ? 's' : ''}</div>
          </div>
        ))}
      </div>

      {data.all_predicted.length > 0 && (
        <div className="mt-4">
          <details>
            <summary className="text-xs text-muted cursor-pointer hover:text-text">
              View all {data.all_predicted.length} projected payouts
            </summary>
            <div className="mt-2 max-h-64 overflow-auto bg-panel/50 border border-border rounded">
              <table className="w-full text-xs">
                <thead className="text-muted uppercase bg-panel2 sticky top-0">
                  <tr>
                    <th className="px-3 py-1.5 text-left">Date</th>
                    <th className="px-3 py-1.5 text-left">Account</th>
                    <th className="px-3 py-1.5 text-right">Payout #</th>
                    <th className="px-3 py-1.5 text-right">Amount</th>
                    <th className="px-3 py-1.5 text-right">Yours</th>
                  </tr>
                </thead>
                <tbody>
                  {[...data.all_predicted]
                    .sort((a, b) => new Date(a.predicted_date).getTime() - new Date(b.predicted_date).getTime())
                    .map((p, i) => (
                      <tr key={i} className="border-t border-border/70">
                        <td className="px-3 py-1 num text-muted">{new Date(p.predicted_date).toLocaleDateString()}</td>
                        <td className="px-3 py-1">{p.account_name}</td>
                        <td className="px-3 py-1 text-right num">#{p.payout_number}</td>
                        <td className="px-3 py-1 text-right num text-win">{fmtPair(p.amount)}</td>
                        <td className="px-3 py-1 text-right num text-win/80">
                          {fmtPair(p.amount_to_trader || p.amount)}
                          {p.trader_split_pct < 1 && (
                            <span className="ml-1 text-[9px] text-muted">{Math.round(p.trader_split_pct * 100)}%</span>
                          )}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}
      <div className="text-[10px] text-muted mt-3">
        Projections assume recent average daily P&L holds, account caps + safety nets are respected,
        and min-trading-day / spacing rules are enforced. "Yours" applies each firm's profit-split.
        {currencyMode === 'BOTH' && ` Secondary currency uses FX rate USD × ${fx} = ${secondary} from settings.`}
      </div>
    </div>
  )
}
