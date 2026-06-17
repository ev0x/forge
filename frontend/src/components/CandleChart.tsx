import { useEffect, useRef, useState } from 'react'
import { createChart, ColorType, IChartApi, ISeriesApi, CandlestickData, LineStyle, UTCTimestamp, SeriesMarker, Time } from 'lightweight-charts'
import { Trade, Execution, MarketDataBar, fmtUsd, api, parseBackendTime } from '../lib/api'
import { useTimezone } from '../lib/timezone'

const TIMEFRAMES = ['s30', 'm1', 'm2', 'm5', 'm15', 'm30', 'h1', 'h4', 'd1']
const TF_SECONDS: Record<string, number> = {
  s30: 30, m1: 60, m2: 120, m5: 300, m15: 900, m30: 1800,
  h1: 3600, h4: 14400, d1: 86400,
}

export default function CandleChart({ trade, executions }: { trade: Trade; executions: Execution[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const [timeframe, setTimeframe] = useState('m5')
  const [bars, setBars] = useState<MarketDataBar[] | null>(null)
  const [loadingBars, setLoadingBars] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)
  const tz = useTimezone()

  // Try to load real bars for this trade's time window (with 30-min padding).
  useEffect(() => {
    if (!trade) return
    let cancelled = false
    setLoadingBars(true); setBars(null)
    // Pad the bar fetch window so the chart has context on either side of the
    // trade. Higher timeframes need more pad (a 4h chart needs days, a 30s
    // chart only needs minutes).
    const tfSec = TF_SECONDS[timeframe] ?? 300
    const padMin = Math.max(30, Math.min(60 * 24 * 7, Math.round((tfSec * 60) / 60)))
    // CRITICAL: parse the backend's naive-UTC timestamps as UTC.  Without this,
    // the browser interprets them as local time and the fetch window misses the
    // trade by hours (Adelaide is 9.5h off).
    const entryUtcMs = parseBackendTime(trade.entry_time).getTime()
    const exitUtcMs = parseBackendTime(trade.exit_time).getTime()
    const from = new Date(entryUtcMs - padMin * 60_000).toISOString()
    const to = new Date(exitUtcMs + padMin * 60_000).toISOString()
    api.marketData.bars(trade.symbol, from, to, timeframe)
      .then(rows => { if (!cancelled) setBars(rows) })
      .catch(() => { if (!cancelled) setBars([]) })
      .finally(() => { if (!cancelled) setLoadingBars(false) })
    return () => { cancelled = true }
  }, [trade.id, trade.symbol, trade.entry_time, trade.exit_time, timeframe])

  const usingReal = (bars?.length ?? 0) > 0

  useEffect(() => {
    if (!containerRef.current || executions.length === 0) return
    if (bars === null) return  // wait for fetch to resolve

    // Build TZ-aware formatters once. `lightweight-charts` calls these for the
    // crosshair tooltip + the x-axis tick labels — without them, both show in
    // the browser's local zone regardless of our user setting.
    const fmtTooltip = (time: number) => {
      const d = new Date(Number(time) * 1000)
      return d.toLocaleString(undefined, {
        timeZone: tz,
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    }
    const fmtTick = (time: number) => {
      const d = new Date(Number(time) * 1000)
      const showSeconds = TF_SECONDS[timeframe] < 60
      const showDate = TF_SECONDS[timeframe] >= 86400
      if (showDate) {
        return d.toLocaleDateString(undefined, { timeZone: tz, month: 'short', day: 'numeric' })
      }
      return d.toLocaleTimeString(undefined, {
        timeZone: tz, hour: '2-digit', minute: '2-digit',
        ...(showSeconds ? { second: '2-digit' } : {}),
      })
    }

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 460,
      layout: {
        background: { type: ColorType.Solid, color: '#121826' },
        textColor: '#7b8aa8',
        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
      },
      grid: {
        vertLines: { color: '#1a2233' },
        horzLines: { color: '#1a2233' },
      },
      rightPriceScale: { borderColor: '#222b3d', scaleMargins: { top: 0.1, bottom: 0.2 } },
      timeScale: {
        borderColor: '#222b3d',
        timeVisible: true,
        secondsVisible: TF_SECONDS[timeframe] < 60,
        tickMarkFormatter: fmtTick as any,
      },
      localization: {
        timeFormatter: fmtTooltip as any,
      },
      crosshair: {
        mode: 1,
        vertLine: { color: '#3a4a66', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#222b3d' },
        horzLine: { color: '#3a4a66', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#222b3d' },
      },
    })
    const series = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
      priceFormat: { type: 'price', precision: 2, minMove: minMoveForRoot(trade.instrument_root) },
    })
    chartRef.current = chart; candleRef.current = series

    // Build candle data
    const sortedExecs = [...executions].sort((a, b) => +new Date(a.fill_time) - +new Date(b.fill_time))
    const candles: CandlestickData[] = []

    if (usingReal) {
      // Real bars: convert directly (parse as UTC to avoid timezone shift)
      bars!.forEach(b => {
        candles.push({
          time: Math.floor(parseBackendTime(b.ts).getTime() / 1000) as UTCTimestamp,
          open: b.o, high: b.h, low: b.l, close: b.c,
        })
      })
    } else {
      // Synthesized from fills
      const usedTimes = new Set<number>()
      let prevClose = sortedExecs[0].fill_price
      let prevHi = sortedExecs[0].fill_price
      let prevLo = sortedExecs[0].fill_price
      sortedExecs.forEach((e) => {
        let t = Math.floor(parseBackendTime(e.fill_time).getTime() / 1000)
        while (usedTimes.has(t)) t += 1
        usedTimes.add(t)
        const open = prevClose, close = e.fill_price
        const hd = e.high_during_position
        const ld = e.low_during_position
        const high = Math.max(open, close,
          hd != null && hd > 0 ? hd : -Infinity, prevHi)
        const low = Math.min(open, close,
          ld != null && ld > 0 ? ld : Infinity, prevLo)
        candles.push({ time: t as UTCTimestamp, open, high, low, close })
        prevClose = close; prevHi = Math.max(prevHi, high); prevLo = Math.min(prevLo, low)
      })
    }
    series.setData(candles)

    // Execution markers (snap each fill to its closest candle)
    const candleTimes = candles.map(c => c.time as number)
    function snap(ts: number): Time {
      if (!usingReal) return ts as UTCTimestamp
      // Find candle whose time is <= ts and closest
      let chosen = candleTimes[0]
      for (const ct of candleTimes) {
        if (ct <= ts) chosen = ct
        else break
      }
      return chosen as UTCTimestamp
    }
    const markers: SeriesMarker<Time>[] = sortedExecs.map(e => {
      const isBuy = e.side === 'Buy'
      const isClose = e.open_close === 'Close'
      const ts = Math.floor(parseBackendTime(e.fill_time).getTime() / 1000)
      return {
        time: snap(ts),
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#22c55e' : '#ef4444',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${isClose ? 'C' : 'O'} ${e.quantity} @ ${e.fill_price.toFixed(2)}`,
        size: Math.min(2, 1 + e.quantity / 5),
      }
    })
    series.setMarkers(markers)

    series.createPriceLine({
      price: trade.avg_entry_price, color: '#7b8aa8', lineWidth: 1,
      lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Entry',
    })
    series.createPriceLine({
      price: trade.avg_exit_price, color: '#f59e0b', lineWidth: 1,
      lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Exit',
    })

    chart.timeScale().fitContent()

    const resize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.resize(containerRef.current.clientWidth, 460)
      }
    }
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      chart.remove()
      chartRef.current = null; candleRef.current = null
    }
  }, [trade.id, executions.length, bars, timeframe, tz]) // eslint-disable-line

  if (executions.length === 0) return null

  return (
    <div className="bg-panel border border-border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <div>
          <div className="text-sm font-semibold flex items-center gap-2">
            {trade.symbol} · {trade.side} × {trade.quantity}
            <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
              usingReal ? 'bg-win/20 text-win' : 'bg-warn/15 text-warn'
            }`}>
              {usingReal ? 'real bars' : 'synthesized'}
            </span>
          </div>
          <div className="text-[10px] text-muted">
            Entry {trade.avg_entry_price.toFixed(2)} → Exit {trade.avg_exit_price.toFixed(2)} ·
            Net {fmtUsd(trade.net_pnl, { signed: true })}
            {usingReal && ` · ${bars!.length} bars cached`}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex bg-panel2 border border-border rounded text-xs overflow-hidden">
            {TIMEFRAMES.map(tf => (
              <button key={tf} onClick={() => setTimeframe(tf)}
                className={`px-2 py-1 ${timeframe === tf ? 'bg-accent text-bg font-medium' : 'text-muted hover:text-text'}`}>
                {tf}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div ref={containerRef} className="w-full" />
      {loadingBars && <div className="text-[10px] text-muted mt-2">Loading bars…</div>}
      {!usingReal && !loadingBars && (
        <div className="text-[10px] text-muted mt-2">
          No cached bars for this symbol / timeframe / window. Showing synthesized candles
          built from fill prices. Upload NinjaTrader tick exports on the{' '}
          <a href="/market-data" className="text-accent hover:underline">Market Data</a> page.
        </div>
      )}
      {feedback && <div className="text-[10px] text-accent mt-2">{feedback}</div>}
    </div>
  )
}

function minMoveForRoot(root: string): number {
  const r = (root || '').toUpperCase()
  // Common futures tick sizes. Defaults to 0.01 for anything unknown so the
  // chart doesn't snap candles to a misleading grid.
  if (['MNQ', 'NQ', 'MES', 'ES', 'M2K', 'RTY'].includes(r)) return 0.25
  if (['MYM', 'YM'].includes(r)) return 1.0
  if (['MGC', 'GC'].includes(r)) return 0.1
  if (['SI', 'SIL'].includes(r)) return 0.005
  if (['CL', 'MCL'].includes(r)) return 0.01
  if (r === 'NG') return 0.001
  if (['FGBL', 'FGBM', 'FGBS', 'FGBX'].includes(r)) return 0.01
  return 0.01
}
