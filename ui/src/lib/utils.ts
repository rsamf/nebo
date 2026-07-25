import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  if (totalSeconds < 60) return `${totalSeconds}s`
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return `${minutes}m ${seconds}s`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return `${hours}h ${remainingMinutes}m`
}

export function formatTimestamp(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

export function timeSince(date: Date): string {
  const now = Date.now()
  const diffMs = now - date.getTime()
  return formatDuration(diffMs)
}

/** React list key for a media entry. media_id alone is NOT unique —
 *  it's content-addressed, so identical frames logged at different
 *  steps share an id. Qualify by list position. */
export function mediaEntryKey(entry: { mediaId: string }, index: number): string {
  return `${entry.mediaId}:${index}`
}

/** Config value → display string (chips, run-info sheet). */
export function formatConfigValue(v: unknown): string {
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}
