import { useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '@/store'
import type { RunSummary } from '@/lib/api'
import { isRunLive } from '@/lib/api'
import { byStartedDesc, childGroupsOf, runDisplayName } from '@/lib/runTree'
import { Sparkline } from './Sparkline'
import { MOBILE_CARD_CLASS, firstLineSeriesValues, shortId, timeAgo } from './util'
import { ArrowLeft, ChevronRight, Folder, Search } from 'lucide-react'
import { cn } from '@/lib/utils'

// Mobile home screen: top-level groups and root runs as uniform cards.
// Tapping a group opens its group page (which lists subgroups, runs and
// notes). Focusing the search field segues to a dedicated search screen.
export function MobileRunList() {
  const runs = useStore(s => s.runs)
  const runTree = useStore(s => s.runTree)
  const connected = useStore(s => s.connected)
  const [searching, setSearching] = useState(false)

  if (searching) {
    return <MobileSearchScreen onClose={() => setSearching(false)} />
  }

  const summaries = Array.from(runs.values(), r => r.summary)
  const topGroups = Object.keys(runTree.groups)
    .filter(g => !g.includes('/'))
    .sort()
  const rootRuns = summaries
    .filter(s => !(s.id in runTree.runs))
    .sort(byStartedDesc)
  const empty = runs.size === 0 && topGroups.length === 0

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-end justify-between px-5 pb-1 pt-4">
        <span className="text-[26px] font-bold tracking-tight">Runs</span>
        <span
          className={cn(
            'mb-1.5 flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px]',
            connected
              ? 'border-green-500/25 bg-green-500/10 text-green-500'
              : 'border-border bg-muted text-muted-foreground',
          )}
        >
          <span className={cn('h-1.5 w-1.5 rounded-full', connected ? 'bg-green-500' : 'bg-muted-foreground')} />
          {connected ? 'connected' : 'offline'}
        </span>
      </div>

      <div className="px-4 pb-1 pt-2">
        {/* A button, not an input: tapping segues to the search screen,
            which owns the real (autofocused) input at the very top. */}
        <button
          onClick={() => setSearching(true)}
          className="flex w-full items-center gap-2 rounded-[10px] border border-border bg-muted/60 px-3 py-2.5"
        >
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="text-[13px] text-muted-foreground">Search runs</span>
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-10 pt-2">
        {empty && (
          <div className="flex h-full flex-col items-center justify-center text-muted-foreground">
            <p className="text-sm">No runs yet</p>
            <p className="mt-1 text-xs">
              Start a pipeline with <code className="rounded bg-muted px-1 py-0.5 text-xs">nebo run</code>
            </p>
          </div>
        )}

        {topGroups.map(g => (
          <MobileGroupCard key={g} path={g} />
        ))}
        {rootRuns.map(s => (
          <MobileRunCard key={s.id} run={s} />
        ))}
      </div>
    </div>
  )
}

// Dedicated search screen: bar pinned to the very top, flat results
// (groups matched by path, runs by name/id). Empty query lists nothing.
function MobileSearchScreen({ onClose }: { onClose: () => void }) {
  const runs = useStore(s => s.runs)
  const runTree = useStore(s => s.runTree)
  const runNames = useStore(s => s.runNames)
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const q = query.trim().toLowerCase()
  const summaries = useMemo(() => Array.from(runs.values(), r => r.summary), [runs])

  const matchedGroups = q
    ? Object.keys(runTree.groups).filter(g => g.toLowerCase().includes(q)).sort()
    : []
  const matchedRuns = q
    ? summaries
        .filter(s =>
          runDisplayName(s, runNames.get(s.id)).toLowerCase().includes(q) ||
          s.id.toLowerCase().includes(q),
        )
        .sort(byStartedDesc)
    : []

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 pb-2.5 pt-3">
        <button
          onClick={onClose}
          aria-label="Back to runs"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="flex flex-1 items-center gap-2 rounded-[10px] border border-border bg-muted/60 px-3">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            type="text"
            placeholder="Search groups and runs"
            value={query}
            onChange={e => setQuery(e.target.value)}
            className="w-full border-none bg-transparent py-2.5 text-[13px] outline-none placeholder:text-muted-foreground"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-10 pt-3">
        {q && matchedGroups.length === 0 && matchedRuns.length === 0 && (
          <div className="px-1 py-2 text-xs text-muted-foreground">No matches</div>
        )}
        {matchedGroups.map(g => (
          <MobileGroupCard key={g} path={g} showFullPath />
        ))}
        {matchedRuns.map(s => (
          <MobileRunCard key={s.id} run={s} />
        ))}
      </div>
    </div>
  )
}

export function MobileGroupCard({
  path,
  showFullPath = false,
}: {
  path: string
  showFullPath?: boolean
}) {
  const runTree = useStore(s => s.runTree)
  const runsMap = useStore(s => s.runs)
  const selectGroup = useStore(s => s.selectGroup)

  const label = showFullPath ? path : (path.split('/').pop() ?? path)
  const docs = runTree.groups[path]?.docs ?? []
  const subgroupCount = childGroupsOf(runTree.groups, path).length
  const runCount = Object.entries(runTree.runs).filter(
    ([id, g]) => g === path && runsMap.has(id),
  ).length
  const metaParts = [`${runCount} run${runCount === 1 ? '' : 's'}`]
  if (subgroupCount > 0) metaParts.push(`${subgroupCount} group${subgroupCount === 1 ? '' : 's'}`)
  if (docs.length > 0) metaParts.push(`${docs.length} note${docs.length === 1 ? '' : 's'}`)

  return (
    <button onClick={() => selectGroup(path)} className={MOBILE_CARD_CLASS}>
      <Folder className="h-[18px] w-[18px] shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className={cn('truncate text-sm font-medium', showFullPath && 'font-mono text-[13px]')}>
          {label}
        </div>
        <div className="mt-px text-[11px] text-muted-foreground">{metaParts.join(' · ')}</div>
      </div>
      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    </button>
  )
}

export function MobileRunCard({ run }: { run: RunSummary }) {
  const customName = useStore(s => s.runNames.get(run.id))
  const runColor = useStore(s => s.runColors.get(run.id))
  const getOrAssignRunColor = useStore(s => s.getOrAssignRunColor)
  const runState = useStore(s => s.runs).get(run.id)
  const selectRun = useStore(s => s.selectRun)

  useEffect(() => {
    getOrAssignRunColor(run.id)
  }, [run.id, getOrAssignRunColor])

  const live = isRunLive(run)
  const color = runColor ?? 'transparent'
  const spark = firstLineSeriesValues(runState?.loggableMetrics)

  return (
    <button onClick={() => selectRun(run.id)} className={MOBILE_CARD_CLASS}>
      <span
        className={cn('h-2.5 w-2.5 shrink-0 rounded-full', live && 'animate-pulse-running')}
        style={{ background: color }}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{runDisplayName(run, customName)}</div>
        <div className="mt-px truncate font-mono text-[11px] text-muted-foreground">
          {shortId(run.id)} · {live ? 'live' : timeAgo(run.last_event_at)}
        </div>
      </div>
      {spark && <Sparkline values={spark} color={runColor ?? '#60a5fa'} className="shrink-0" />}
      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    </button>
  )
}
