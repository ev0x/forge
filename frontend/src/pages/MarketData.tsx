import { useEffect, useState } from 'react'
import { api, MarketDataSummaryRow } from '../lib/api'

export default function MarketData() {
  const [summary, setSummary] = useState<MarketDataSummaryRow[]>([])
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const [symbolOverride, setSymbolOverride] = useState('')

  async function refresh() {
    setSummary(await api.marketData.summary())
  }
  useEffect(() => { refresh() }, [])

  async function uploadFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    setBusy(true); setErr(null); setResult(null)
    const results: any[] = []
    try {
      for (const file of Array.from(files)) {
        const r = await api.marketData.uploadNTTick(file, symbolOverride.trim() || undefined)
        results.push({ file: file.name, ...r })
      }
      setResult(results)
      refresh()
    } catch (e: any) {
      setErr(String(e.message || e))
    } finally { setBusy(false) }
  }

  async function del(s: MarketDataSummaryRow) {
    if (!confirm(`Delete all ${s.bar_count} ${s.timeframe} bars for ${s.symbol}?`)) return
    await api.marketData.deleteSymbol(s.symbol, s.timeframe); refresh()
  }

  async function delAll() {
    if (!confirm('Delete ALL cached bars across every symbol and timeframe? This cannot be undone.')) return
    const r = await api.marketData.deleteAll()
    alert(`Deleted ${r.deleted} bars.`)
    refresh()
  }

  // Group summary rows by symbol so each contract appears once with its
  // available timeframes underneath.
  const bySymbol: Record<string, MarketDataSummaryRow[]> = {}
  summary.forEach(s => {
    (bySymbol[s.symbol] ||= []).push(s)
  })

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div className="bg-panel border border-border rounded-lg p-4">
        <div className="text-sm font-semibold mb-1">Market Data — NinjaTrader tick imports</div>
        <div className="text-xs text-muted leading-relaxed">
          Drop NinjaTrader tick text exports here. Filenames like{' '}
          <code className="bg-bg px-1 mx-0.5 rounded">FGBL 03-26.Last.txt</code> are recognised automatically —
          the parser extracts the root (<code className="bg-bg px-1 mx-0.5 rounded">FGBL</code>) and contract
          (<code className="bg-bg px-1 mx-0.5 rounded">FGBLH6</code>) from the filename. Each upload is streamed
          once and aggregated into all supported timeframes simultaneously
          (s30, m1, m2, m5, m15, m30, h1, h4, d1).
          <div className="mt-2 text-warn text-[11px]">
            Trade charts match by root prefix — a trade on{' '}
            <code className="bg-bg px-1 mx-0.5 rounded">FGBLU6.CME</code> will find any{' '}
            <code className="bg-bg px-1 mx-0.5 rounded">FGBL*</code> data you've uploaded.
          </div>
        </div>
      </div>

      <div className="bg-panel border border-border rounded-lg p-5 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <div className="text-sm font-semibold">Upload NinjaTrader tick files</div>
            <div className="text-xs text-muted mt-0.5">
              In NT: <em>Control Center → Connections → Historical Data → right-click symbol → Export</em>.
              Pick <em>Last</em> tick type. Drop the resulting <code className="bg-bg px-1 rounded">.txt</code>(s) below.
            </div>
          </div>
          <label className="bg-accent text-bg font-medium px-3 py-1.5 rounded text-sm cursor-pointer hover:opacity-90">
            {busy ? 'Uploading…' : 'Choose .txt file(s)'}
            <input type="file" accept=".txt" multiple className="hidden"
              onChange={e => uploadFiles(e.target.files)} />
          </label>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-muted">Symbol override (optional)</label>
            <input value={symbolOverride} onChange={e => setSymbolOverride(e.target.value)}
              placeholder="leave blank to parse from filename"
              className="w-full mt-1 bg-panel2 border border-border rounded px-2 py-1.5 text-sm" />
            <div className="text-[10px] text-muted mt-0.5">
              Only set if the filename can't be parsed (e.g. you renamed the export).
            </div>
          </div>
        </div>

        {err && <div className="text-xs text-loss bg-loss/10 border border-loss/40 rounded p-2">{err}</div>}
        {result && Array.isArray(result) && (
          <div className="text-xs bg-win/10 border border-win/40 rounded p-3 space-y-2">
            <div className="text-win font-medium">Upload complete — {result.length} file{result.length !== 1 ? 's' : ''}</div>
            {result.map((r: any, i: number) => (
              <div key={i} className="border-t border-border/50 pt-2 first:border-0 first:pt-0">
                <div className="font-medium">{r.file} → {r.symbol}</div>
                <div className="text-muted">
                  Parsed <b className="text-text">{r.parsed.toLocaleString()}</b> bars ·
                  inserted <b className="text-text">{r.inserted.toLocaleString()}</b> ·
                  skipped <b className="text-text">{r.skipped_duplicates.toLocaleString()}</b> dupes
                </div>
                {r.earliest && r.latest && (
                  <div className="text-muted">
                    Range: {new Date(r.earliest).toLocaleString()} → {new Date(r.latest).toLocaleString()}
                  </div>
                )}
                {(r.notes || []).map((n: string, j: number) => <div key={j} className="text-muted">· {n}</div>)}
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm font-semibold">Cached bars</div>
          {summary.length > 0 && (
            <button onClick={delAll} className="text-xs text-loss/80 hover:text-loss">
              Delete all bars
            </button>
          )}
        </div>
        <div className="bg-panel border border-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-panel2 text-xs uppercase text-muted">
              <tr>
                <th className="px-3 py-2 text-left">Symbol</th>
                <th className="px-3 py-2 text-left">Timeframe</th>
                <th className="px-3 py-2 text-right">Bars</th>
                <th className="px-3 py-2 text-left">Range</th>
                <th className="px-3 py-2 text-left">Sources</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {summary.map(s => (
                <tr key={`${s.symbol}-${s.timeframe}`} className="border-t border-border">
                  <td className="px-3 py-2 font-medium">{s.symbol}</td>
                  <td className="px-3 py-2">{s.timeframe}</td>
                  <td className="px-3 py-2 text-right num">{s.bar_count.toLocaleString()}</td>
                  <td className="px-3 py-2 text-xs text-muted num">
                    {new Date(s.earliest).toLocaleString()} → {new Date(s.latest).toLocaleString()}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">{s.sources.join(', ') || '—'}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => del(s)}
                      className="text-xs text-loss/80 hover:text-loss">Delete</button>
                  </td>
                </tr>
              ))}
              {!summary.length && (
                <tr><td colSpan={6} className="px-3 py-6 text-center text-muted">
                  No bars cached yet — upload NinjaTrader tick files above.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
