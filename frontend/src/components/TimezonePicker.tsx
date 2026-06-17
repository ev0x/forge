import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { TIMEZONE_GROUPS, timezoneShortLabel, timezoneOffsetLabel, useTimezone } from '../lib/timezone'

export default function TimezonePicker({ onChanged }: { onChanged: (tz: string) => void }) {
  const tz = useTimezone()
  const [open, setOpen] = useState(false)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) setOpen(false)
    }
    if (open) document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  async function pick(next: string) {
    if (next !== tz) {
      onChanged(next)
      try { await api.settings.update({ timezone: next }) } catch {}
    }
    setOpen(false)
  }

  const short = timezoneShortLabel(tz)
  const offset = timezoneOffsetLabel(tz)

  return (
    <div className="relative" ref={popRef}>
      <button onClick={() => setOpen(v => !v)}
        title={`Display timezone: ${tz} (${offset})`}
        className="text-xs px-2 py-1 rounded border border-border bg-panel hover:border-accent/40 flex items-center gap-1.5">
        <span className="font-mono">{short}</span>
        <span className="text-muted">{offset}</span>
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-72 max-h-96 overflow-y-auto bg-panel border border-border rounded-lg shadow-lg z-50 text-sm">
          <div className="px-3 py-2 border-b border-border text-[10px] uppercase tracking-wider text-muted">
            Display timezone — affects every timestamp shown
          </div>
          {TIMEZONE_GROUPS.map(g => (
            <div key={g.label}>
              <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted bg-panel2/40">
                {g.label}
              </div>
              {g.zones.map(z => {
                const active = z === tz
                return (
                  <button key={z} onClick={() => pick(z)}
                    className={`w-full text-left px-3 py-1.5 flex items-center justify-between hover:bg-panel2 ${
                      active ? 'bg-accent/10 text-accent' : 'text-text'
                    }`}>
                    <span>{z}</span>
                    <span className="text-[10px] text-muted font-mono">{timezoneOffsetLabel(z)}</span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
