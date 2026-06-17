import { createContext, useContext, useMemo, ReactNode } from 'react'
import { parseBackendTime } from './api'

/**
 * Global display-timezone for trade timestamps. Backend stores everything as
 * naive-UTC; on the way out we format into whatever IANA zone the user picks.
 * The setting lives in `user_settings.timezone` on the backend.
 */
const TimezoneContext = createContext<string>('UTC')

export function TimezoneProvider({ tz, children }: { tz: string; children: ReactNode }) {
  return <TimezoneContext.Provider value={tz || 'UTC'}>{children}</TimezoneContext.Provider>
}

export function useTimezone(): string {
  return useContext(TimezoneContext)
}

export type FmtOpts = { dateOnly?: boolean; timeOnly?: boolean; second?: boolean }

function formatInTz(ts: string | Date | null | undefined, tz: string, opts?: FmtOpts): string {
  if (ts === null || ts === undefined) return ''
  const d = typeof ts === 'string' ? parseBackendTime(ts) : ts
  if (Number.isNaN(d.getTime())) return ''
  if (opts?.dateOnly) {
    return d.toLocaleDateString(undefined, { timeZone: tz })
  }
  if (opts?.timeOnly) {
    return d.toLocaleTimeString(undefined, {
      timeZone: tz,
      hour: '2-digit', minute: '2-digit',
      ...(opts?.second ? { second: '2-digit' } : {}),
    })
  }
  return d.toLocaleString(undefined, {
    timeZone: tz,
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
    ...(opts?.second ? { second: '2-digit' } : {}),
  })
}

/** Memoised formatter bound to the active timezone. */
export function useDateFmt() {
  const tz = useTimezone()
  return useMemo(() => (ts: string | Date | null | undefined, opts?: FmtOpts) => formatInTz(ts, tz, opts), [tz])
}

/** Format `ts` in `tz` outside a component (e.g. inside utility code). */
export function formatInTimezone(ts: string | Date, tz: string, opts?: FmtOpts) {
  return formatInTz(ts, tz, opts)
}

/** Curated IANA timezone list, grouped for the picker. */
export const TIMEZONE_GROUPS: { label: string; zones: string[] }[] = [
  {
    label: 'UTC',
    zones: ['UTC'],
  },
  {
    label: 'Australia / Pacific',
    zones: [
      'Australia/Sydney',
      'Australia/Melbourne',
      'Australia/Brisbane',
      'Australia/Adelaide',
      'Australia/Perth',
      'Australia/Darwin',
      'Australia/Hobart',
      'Pacific/Auckland',
      'Pacific/Fiji',
      'Pacific/Honolulu',
    ],
  },
  {
    label: 'Asia',
    zones: [
      'Asia/Tokyo',
      'Asia/Singapore',
      'Asia/Hong_Kong',
      'Asia/Shanghai',
      'Asia/Seoul',
      'Asia/Taipei',
      'Asia/Bangkok',
      'Asia/Jakarta',
      'Asia/Kolkata',
      'Asia/Dubai',
      'Asia/Tehran',
    ],
  },
  {
    label: 'Europe / Africa',
    zones: [
      'Europe/London',
      'Europe/Dublin',
      'Europe/Paris',
      'Europe/Berlin',
      'Europe/Madrid',
      'Europe/Amsterdam',
      'Europe/Zurich',
      'Europe/Stockholm',
      'Europe/Athens',
      'Europe/Istanbul',
      'Europe/Moscow',
      'Africa/Johannesburg',
      'Africa/Cairo',
      'Africa/Lagos',
    ],
  },
  {
    label: 'Americas',
    zones: [
      'America/New_York',
      'America/Chicago',
      'America/Denver',
      'America/Phoenix',
      'America/Los_Angeles',
      'America/Anchorage',
      'America/Toronto',
      'America/Vancouver',
      'America/Mexico_City',
      'America/Sao_Paulo',
      'America/Buenos_Aires',
      'America/Bogota',
      'America/Caracas',
    ],
  },
]

/** Short display label for the current TZ — e.g. "ACST" / "ET" / "UTC". Falls
 * back to the IANA name if `Intl.DateTimeFormat` can't resolve an abbreviation. */
export function timezoneShortLabel(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZone: tz, timeZoneName: 'short' })
      .formatToParts(new Date())
    const tzPart = parts.find(p => p.type === 'timeZoneName')
    if (tzPart?.value) return tzPart.value
  } catch {}
  return tz
}

/** Current UTC offset for a TZ as e.g. "+09:30" or "-05:00". */
export function timezoneOffsetLabel(tz: string): string {
  try {
    const now = new Date()
    const utc = new Date(now.toLocaleString('en-US', { timeZone: 'UTC' }))
    const local = new Date(now.toLocaleString('en-US', { timeZone: tz }))
    const offsetMin = Math.round((local.getTime() - utc.getTime()) / 60000)
    const sign = offsetMin >= 0 ? '+' : '-'
    const abs = Math.abs(offsetMin)
    const hh = String(Math.floor(abs / 60)).padStart(2, '0')
    const mm = String(abs % 60).padStart(2, '0')
    return `${sign}${hh}:${mm}`
  } catch {
    return ''
  }
}
