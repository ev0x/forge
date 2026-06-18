import { useEffect, useMemo, useState } from 'react'
import { api, Trade, Execution, parseBackendTime } from '../lib/api'
import { useTimezone } from '../lib/timezone'

/**
 * Footprint grid with tick-level replay.
 *
 * The whole component is driven by a list of classified ticks fetched once per
 * (trade, bar size). During replay the playhead `cursor` (in ms) advances at
 * speed × wall-clock; on every frame we re-aggregate ticks whose `t <= cursor`
 * into the precomputed (stable) bar/price axes. That gives true tick-by-tick
 * print without the axes flickering as data arrives.
 */

type Side = 'A' | 'B'
type Tick = { t: number; price: number; side: Side; size: number }
type ServerData = {
  cached: boolean; symbol: string; tick_count: number; cost: number
  bar_seconds: number; tick_size: number
  bars: Array<{ ts: number }>
  prices: number[]
  ticks: Tick[]
  markers: {
    direction: 'LONG' | 'SHORT' | null
    entry: { ts: number; price: number } | null
    exit: { ts: number; price: number } | null
  }
}

const BARS = [
  { label: '30s', v: 30 }, { label: '1m', v: 60 }, { label: '2m', v: 120 }, { label: '5m', v: 300 },
  { label: '15m', v: 900 }, { label: '30m', v: 1800 }, { label: '1h', v: 3600 },
]
const SPEEDS = [
  { label: '1×', v: 1 }, { label: '3×', v: 3 }, { label: '5×', v: 5 }, { label: '10×', v: 10 },
  { label: '30×', v: 30 }, { label: '60×', v: 60 },
]
const CELL_MODES = [
  { label: 'Bid×Ask', v: 'bidask' }, { label: 'Total×Δ', v: 'totaldelta' },
] as const
type CellMode = (typeof CELL_MODES)[number]['v']

function fmtTime(ms: number, tz: string) {
  return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: tz })
}
function fmtClock(ms: number, tz: string) {
  return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: tz })
}

/** Accumulate ticks up to `cursor` into a grid keyed by barTs (ms) and quantised
 *  price key. Side 'A' = aggressive sell (left/red); 'B' = aggressive buy (right/green). */
function partialGrid(ticks: Tick[], cursor: number, barSeconds: number, tickSize: number) {
  const barMs = barSeconds * 1000
  const grid: Record<number, Record<string, [number, number]>> = {}
  for (const tk of ticks) {
    if (tk.t > cursor) break
    const barTs = Math.floor(tk.t / barMs) * barMs
    const price = Math.round(tk.price / tickSize) * tickSize
    const key = price.toFixed(8)
    if (!grid[barTs]) grid[barTs] = {}
    if (!grid[barTs][key]) grid[barTs][key] = [0, 0]
    if (tk.side === 'A') grid[barTs][key][0] += tk.size
    else if (tk.side === 'B') grid[barTs][key][1] += tk.size
  }
  return grid
}

export default function FootprintGrid({ trade, executions }: { trade: Trade; executions: Execution[] }) {
  const tz = useTimezone()
  const [data, setData] = useState<ServerData | null>(null)
  const [bar, setBar] = useState(300)
  const [cellMode, setCellMode] = useState<CellMode>('totaldelta')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [replay, setReplay] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [cursor, setCursor] = useState(0)

  // Fetch once per (trade, bar). Window pads ~5 bars either side of the trade.
  useEffect(() => {
    let alive = true
    setLoading(true); setErr('')
    const padSec = Math.max(60, bar * 5)
    const entryMs = parseBackendTime(trade.entry_time).getTime()
    const exitMs = parseBackendTime(trade.exit_time).getTime()
    const fromIso = new Date(entryMs - padSec * 1000).toISOString()
    const toIso = new Date(exitMs + padSec * 1000).toISOString()
    api.marketData.footprintTicks(trade.symbol, fromIso, toIso, bar, trade.id)
      .then(d => { if (alive) setData(d) })
      .catch(e => { if (alive) setErr(String(e?.message || e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [trade.id, trade.symbol, trade.entry_time, trade.exit_time, bar])

  const ticks = useMemo(() => data?.ticks || [], [data])
  const tRange = useMemo<[number, number]>(
    () => ticks.length ? [ticks[0].t, ticks[ticks.length - 1].t] : [0, 0],
    [ticks])

  // Reset cursor whenever data changes or replay re-engages.
  useEffect(() => { setCursor(tRange[0]); setPlaying(false) }, [replay, tRange[0]])

  // Playback timer — advance cursor by speed × 120 ms each 120 ms (so 1× = real-time).
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      setCursor(c => {
        const next = c + speed * 120
        if (next >= tRange[1]) { setPlaying(false); return tRange[1] }
        return next
      })
    }, 120)
    return () => clearInterval(id)
  }, [playing, speed, tRange])

  // Spacebar play/pause when replay is on (skip if typing in an input).
  useEffect(() => {
    if (!replay) return
    function onKey(e: KeyboardEvent) {
      if (e.code === 'Space' && (e.target as HTMLElement).tagName !== 'INPUT') {
        e.preventDefault()
        setPlaying(p => !p)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [replay])

  const hasData = !!data && (data.bars?.length || 0) > 0 && (data.ticks?.length || 0) > 0

  // In replay mode, rebuild bars + markers from ticks up to cursor.
  const view = useMemo<ViewData | null>(() => {
    if (!data) return null
    if (!replay || !hasData) {
      // Static view: aggregate all ticks once.
      const grid = partialGrid(data.ticks, tRange[1], data.bar_seconds, data.tick_size)
      return {
        ...data,
        bars: data.bars.map(b => ({ ts: b.ts, cells: grid[b.ts] || {} })),
        markers: data.markers,
        lastPrice: data.ticks.length ? data.ticks[data.ticks.length - 1].price : null,
      }
    }
    const grid = partialGrid(data.ticks, cursor, data.bar_seconds, data.tick_size)
    const m = data.markers || { direction: null, entry: null, exit: null }
    // Most recent tick whose timestamp is <= cursor — used to render the
    // "current price" line during replay.
    let lastPrice: number | null = null
    for (let i = 0; i < data.ticks.length; i++) {
      if (data.ticks[i].t > cursor) break
      lastPrice = data.ticks[i].price
    }
    return {
      ...data,
      bars: data.bars.map(b => ({ ts: b.ts, cells: grid[b.ts] || {} })),
      markers: {
        direction: m.direction,
        entry: m.entry && cursor >= m.entry.ts ? m.entry : null,
        exit: m.exit && cursor >= m.exit.ts ? m.exit : null,
      },
      lastPrice,
    }
  }, [data, replay, hasData, ticks, cursor, tRange])

  const barMs = data ? data.bar_seconds * 1000 : 0
  const activeBarTs = replay && barMs ? Math.floor(cursor / barMs) * barMs : null

  return (
    <div className="bg-panel border border-border rounded-lg p-4">
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div className="text-xs uppercase tracking-wider text-muted font-semibold">Footprint chart</div>
        <div className="flex bg-panel2 border border-border rounded text-xs overflow-hidden">
          {BARS.map(b => (
            <button key={b.v} onClick={() => setBar(b.v)}
              className={`px-2.5 py-1 ${bar === b.v ? 'bg-accent text-bg font-medium' : 'text-muted hover:text-text'}`}>
              {b.label}
            </button>
          ))}
        </div>
        <div className="flex bg-panel2 border border-border rounded text-xs overflow-hidden">
          {CELL_MODES.map(m => (
            <button key={m.v} onClick={() => setCellMode(m.v)}
              className={`px-2.5 py-1 ${cellMode === m.v ? 'bg-accent text-bg font-medium' : 'text-muted hover:text-text'}`}>
              {m.label}
            </button>
          ))}
        </div>
        {hasData && (
          <button onClick={() => setReplay(r => !r)}
            className={`text-xs px-3 py-1 rounded border ${
              replay ? 'border-accent/60 bg-accent/15 text-accent' : 'border-border bg-panel2 text-muted hover:text-text'
            }`}>
            {replay ? '✕ Exit replay' : '▶ Replay'}
          </button>
        )}
        {hasData && !replay && (
          <span className="text-[11px] text-muted ml-auto">
            {data!.symbol} · {data!.tick_count.toLocaleString()} ticks · {data!.bars.length} bars
          </span>
        )}
        {err && <span className="text-xs text-loss">{err}</span>}
      </div>

      {/* Replay controls strip */}
      {replay && hasData && (
        <div className="mb-4 flex items-center gap-3 bg-panel2/40 border border-border rounded-lg px-3 py-2.5 flex-wrap">
          <button onClick={() => {
            if (cursor >= tRange[1]) setCursor(tRange[0])
            setPlaying(p => !p)
          }}
            className={`text-sm w-20 py-1.5 rounded font-bold ${
              playing ? 'bg-warn/20 text-warn border border-warn/40' : 'bg-accent text-bg'
            }`}>
            {playing ? '❚❚ Pause' : '▶ Play'}
          </button>
          <div className="flex bg-bg border border-border rounded text-xs overflow-hidden"
            title="× real-time. At 1× one wall-clock second = one second of market time.">
            {SPEEDS.map(s => (
              <button key={s.v} onClick={() => setSpeed(s.v)}
                className={`px-2.5 py-1 ${speed === s.v ? 'bg-accent text-bg font-medium' : 'text-muted hover:text-text'}`}>
                {s.label}
              </button>
            ))}
          </div>
          <input type="range" min={tRange[0]} max={tRange[1]} step={1000} value={cursor}
            onChange={e => { setPlaying(false); setCursor(parseInt(e.target.value)) }}
            className="flex-1 min-w-[160px] accent-accent" />
          <span className="text-xs num text-muted min-w-[78px] text-right">{fmtClock(cursor, tz)}</span>
          <button onClick={() => { setPlaying(false); setCursor(tRange[0]) }}
            className="text-xs px-2.5 py-1 rounded border border-border bg-bg text-muted hover:text-text">
            ⟲ Reset
          </button>
        </div>
      )}

      {/* Body */}
      {loading && <div className="text-xs text-muted py-12 text-center">Loading footprint…</div>}
      {!loading && !hasData && (
        <div className="text-xs text-muted py-12 text-center">
          No footprint tick data for {trade.symbol} in this window. Upload tick files on the{' '}
          <a href="/market-data" className="text-accent hover:underline">Market Data</a> page.
        </div>
      )}
      {!loading && view && hasData && (
        <FpGridView data={view} cellMode={cellMode} activeBarTs={activeBarTs} tz={tz} />
      )}
    </div>
  )
}


type ViewBar = { ts: number; cells: Record<string, [number, number]> }
type ViewData = Omit<ServerData, 'bars'> & { bars: ViewBar[]; lastPrice: number | null }

function FpGridView({ data, cellMode, activeBarTs, tz }: {
  data: ViewData
  cellMode: CellMode
  activeBarTs: number | null
  tz: string
}) {
  const prices = useMemo(() => [...data.prices].sort((a, b) => b - a), [data.prices])
  const decimals = data.tick_size && data.tick_size < 0.01 ? 5 : 2
  const cellW = 96
  const labelW = 80

  const barMs = data.bar_seconds * 1000
  const snap = (price: number): string | null => {
    let best: number | null = null
    let bd = Infinity
    for (const p of prices) {
      const d = Math.abs(p - price)
      if (d < bd) { bd = d; best = p }
    }
    return best != null ? best.toFixed(8) : null
  }
  const markerFor = (m: { ts: number; price: number } | null) =>
    m ? { bar: Math.floor(m.ts / barMs) * barMs, key: snap(m.price) } : null
  const entryM = markerFor(data.markers.entry)
  const exitM = markerFor(data.markers.exit)
  // Snap the current-price marker onto a displayed price row.
  const lastPriceKey = data.lastPrice != null ? snap(data.lastPrice) : null
  const isShort = data.markers.direction === 'SHORT'
  const entryColor = isShort ? '#ef4444' : '#22c55e'
  const entryLabel = isShort ? 'S' : 'L'
  const badge = (barTs: number, key: string): [string, string][] => {
    const out: [string, string][] = []
    if (entryM && entryM.bar === barTs && entryM.key === key) out.push([entryLabel, entryColor])
    if (exitM && exitM.bar === barTs && exitM.key === key) out.push(['X', '#f59e0b'])
    return out
  }

  return (
    <div className="overflow-x-auto bg-bg border border-border rounded-lg p-2.5">
      <div style={{ display: 'inline-block', minWidth: '100%' }}>
        {/* Header */}
        <div style={{ display: 'flex' }}>
          <div style={{ width: labelW, flexShrink: 0 }} />
          {data.bars.map(b => (
            <div key={b.ts}
              style={{
                width: cellW, flexShrink: 0, textAlign: 'center',
                fontSize: 11, paddingBottom: 6,
                color: b.ts === activeBarTs ? '#a78bfa' : '#71717a',
                fontWeight: b.ts === activeBarTs ? 800 : 600,
              }}>
              {fmtTime(b.ts, tz)}
            </div>
          ))}
        </div>
        {/* Rows */}
        {prices.map(price => {
          const key = price.toFixed(8)
          const isCurrentPrice = key === lastPriceKey
          return (
            <div key={key} style={{
              display: 'flex', alignItems: 'stretch',
              // Subtle horizontal accent across the whole row at the current price.
              boxShadow: isCurrentPrice ? 'inset 0 0 0 1px rgba(167, 139, 250, 0.55)' : undefined,
              background: isCurrentPrice ? 'rgba(167, 139, 250, 0.06)' : undefined,
            }}>
              <div style={{
                width: labelW, flexShrink: 0, fontSize: 11,
                color: isCurrentPrice ? '#a78bfa' : '#a1a1aa',
                fontFamily: 'monospace', fontWeight: isCurrentPrice ? 700 : 400,
                display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
                paddingRight: 8, gap: 4,
              }}>
                {isCurrentPrice && <span style={{ fontSize: 10 }}>▶</span>}
                {price.toFixed(decimals)}
              </div>
              {data.bars.map(b => {
                const marks = badge(b.ts, key)
                const markEl = marks.length > 0 && (
                  <span style={{ position: 'absolute', top: 1, right: 2, display: 'flex', gap: 2 }}>
                    {marks.map(([t, c]) => (
                      <span key={t} style={{
                        background: c, color: '#09090b', fontSize: 9, fontWeight: 800,
                        borderRadius: 3, padding: '0 3px', lineHeight: '13px',
                      }}>{t}</span>
                    ))}
                  </span>
                )
                const cell = b.cells[key]
                if (!cell) {
                  return (
                    <div key={b.ts} style={{
                      position: 'relative', width: cellW, flexShrink: 0,
                      borderBottom: '1px solid #111',
                      border: marks.length ? `1px solid ${marks[0][1]}` : undefined,
                    }}>
                      {markEl}
                    </div>
                  )
                }
                const [bid, ask] = cell
                const delta = ask - bid
                const total = ask + bid
                const intensity = Math.min(1, 0.18 + total / 80)
                const bg = delta > 0
                  ? `rgba(16,120,40,${intensity})`
                  : delta < 0 ? `rgba(150,25,25,${intensity})` : 'rgba(40,40,46,0.5)'
                return (
                  <div key={b.ts} style={{
                    position: 'relative', width: cellW, flexShrink: 0, background: bg,
                    border: marks.length ? `2px solid ${marks[0][1]}` : '1px solid #1a1a1f',
                    fontSize: 11, color: '#e6e6e6', fontFamily: 'monospace',
                    textAlign: 'center', padding: '3px 0',
                  }}>
                    {cellMode === 'totaldelta'
                      ? <>{fmtN(total)} <span style={{ color: '#71717a' }}>×</span> {delta >= 0 ? '+' : ''}{fmtN(delta)}</>
                      : <>{fmtN(bid)} <span style={{ color: '#71717a' }}>×</span> {fmtN(ask)}</>}
                    {markEl}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
      <div className="text-[11px] text-muted mt-2 flex items-center gap-3 flex-wrap">
        <span>cells: {cellMode === 'totaldelta' ? 'total × delta' : 'bid × ask'} · green = net buying · red = net selling</span>
        <span>· {isShort ? 'S' : 'L'} = entry, X = exit</span>
        <span className="text-accent">· ▶ = current price{data.lastPrice != null ? ` (${data.lastPrice.toFixed(decimals)})` : ''}</span>
      </div>
    </div>
  )
}

function fmtN(v: number): string {
  const a = Math.abs(v)
  if (a >= 10000) return `${Math.sign(v) < 0 ? '-' : ''}${(a / 1000).toFixed(0)}k`
  if (a >= 1000) return `${Math.sign(v) < 0 ? '-' : ''}${(a / 1000).toFixed(1)}k`
  return Math.round(v).toString()
}
