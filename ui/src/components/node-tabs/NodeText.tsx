// Renders a loggable's text streams as one card per stream name — the same
// shape as metrics (one MetricBlock per name) and images (grouped by name).
// Text is payload data, not a log console: there is no level and no
// message-content search.
import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useStore } from '@/store'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { formatTimestamp } from '@/lib/utils'
import { cn } from '@/lib/utils'
import { ComparisonGrid } from '@/components/shared/ComparisonGrid'
import { HeaderActions } from './HeaderActions'
import { Modal } from '@/components/ui/modal'
import { buildEmbeddedUrl } from '@/hooks/useEmbeddedView'
import type { TextEntry } from '@/lib/api'

interface NodeTextProps {
  runId: string
  loggableId: string
  comparisonRunIds?: string[]
  // When true, fill the parent's height instead of capping each stream's
  // entry list. Used inside fixed-height DAG nodes.
  fillParent?: boolean
}

/** Group a loggable's entries by stream name, preserving emission order. */
function groupByName(texts: TextEntry[], loggableId: string): Map<string, TextEntry[]> {
  const byName = new Map<string, TextEntry[]>()
  for (const t of texts) {
    if (t.node !== loggableId) continue
    const arr = byName.get(t.name)
    if (arr) arr.push(t)
    else byName.set(t.name, [t])
  }
  return byName
}

export function NodeText({ runId, loggableId, comparisonRunIds, fillParent }: NodeTextProps) {
  if (comparisonRunIds) {
    return <ComparisonText loggableId={loggableId} runIds={comparisonRunIds} fillParent={fillParent} />
  }
  return <SingleRunText runId={runId} loggableId={loggableId} fillParent={fillParent} />
}

function SingleRunText({ runId, loggableId, fillParent }: { runId: string; loggableId: string; fillParent?: boolean }) {
  const texts = useStore(s => s.runs.get(runId)?.texts ?? [])
  const byName = useMemo(() => groupByName(texts, loggableId), [texts, loggableId])

  if (byName.size === 0) {
    return <p className="text-xs text-muted-foreground">No text for this node</p>
  }

  const single = byName.size === 1
  return (
    <div className={cn('space-y-3', fillParent && 'h-full min-h-0 overflow-auto')}>
      {[...byName.entries()].map(([name, entries]) => (
        <TextBlock
          key={name}
          name={name}
          entries={entries}
          runId={runId}
          loggableId={loggableId}
          fill={fillParent && single}
        />
      ))}
    </div>
  )
}

/**
 * One text entry, shaped exactly like `NodeImages`'s `ImageItem`: a single
 * header row (name, `step N · timestamp`, then the actions) with the
 * payload underneath. Expanding opens the full message — the text
 * equivalent of viewing an image full-size.
 */
function TextItem({ name, entry, runId, loggableId }: {
  name: string
  entry: TextEntry
  runId?: string
  loggableId?: string
}) {
  const [modalOpen, setModalOpen] = useState(false)

  const iframeUrl = runId && loggableId
    ? buildEmbeddedUrl({ runId, node: loggableId, text: name })
    : undefined

  return (
    <div data-export-atom="text-line" className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="truncate text-xs font-medium">{name}</span>
          <span className="shrink-0 text-[10px] text-muted-foreground">
            {entry.step != null && `step ${entry.step}`}
            {entry.step != null && ' · '}
            {formatTimestamp(entry.timestamp)}
          </span>
        </div>
        <HeaderActions onExpand={() => setModalOpen(true)} iframeUrl={iframeUrl} />
      </div>
      <div className="whitespace-pre-wrap break-words font-mono text-sm">{entry.message}</div>
      {modalOpen && (
        <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={name} widthClass="max-w-4xl">
          <div className="whitespace-pre-wrap break-words font-mono text-sm">{entry.message}</div>
        </Modal>
      )}
    </div>
  )
}

/**
 * One named text stream: a list of entries, each carrying its own header
 * and actions. No block-level header — the entries label themselves, the
 * way image items do. Exported for the flat view's per-name cards.
 */
export function TextBlock({ name, entries, runId, loggableId, fill }: {
  name: string
  entries: TextEntry[]
  runId?: string
  loggableId?: string
  fill?: boolean
}) {
  const exportLimit = useStore(s => s.exportEntryLimit)
  const timelineFilter = useTimelineFilter()

  const visible = useMemo(() => {
    let out = entries
    if (timelineFilter) out = out.filter(t => timelineFilter.matchEntry(t))
    if (exportLimit) out = out.slice(0, exportLimit)
    return out
  }, [entries, timelineFilter, exportLimit])

  return (
    <div data-export-atom="text" className={cn(fill && 'flex h-full min-h-0 flex-col')}>
      <TextEntryList
        name={name}
        entries={visible}
        runId={runId}
        loggableId={loggableId}
        maxHeightClass={fill ? undefined : 'max-h-[200px]'}
      />
    </div>
  )
}

/**
 * Virtualized entry list for one stream. Each entry mirrors the image-item
 * shape: a meta header (name + step/timestamp) with the message underneath.
 * Sticks to the latest entry until the user scrolls up.
 */
function TextEntryList({ name, entries, runId, loggableId, maxHeightClass }: {
  name: string
  entries: TextEntry[]
  runId?: string
  loggableId?: string
  maxHeightClass?: string
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Whether to stick to the newest entry. A ref (not state) so the
  // auto-scroll effect reads the live value without re-running on it.
  const autoScrollRef = useRef(true)
  // "Jump to latest" is shown only when the list actually overflows AND
  // the user has scrolled away from the bottom. Without the overflow
  // check it lingers after a step filter shrinks the list to a couple of
  // entries — a button that scrolls nothing.
  const [showJump, setShowJump] = useState(false)

  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 8,
  })

  const sync = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const { scrollTop, scrollHeight, clientHeight } = el
    const overflows = scrollHeight - clientHeight > 1
    const atBottom = scrollHeight - scrollTop - clientHeight < 30
    autoScrollRef.current = !overflows || atBottom
    setShowJump(overflows && !atBottom)
  }, [])

  useEffect(() => {
    if (autoScrollRef.current && entries.length > 0) {
      virtualizer.scrollToIndex(entries.length - 1, { align: 'end' })
    }
  }, [entries.length, virtualizer])

  // Content height changes (step filter narrowing the list, new entries
  // streaming in) re-evaluate scrollability. The observer callback is
  // async, so this never sets state synchronously during an effect.
  useEffect(() => {
    const el = contentRef.current
    if (!el) return
    const ro = new ResizeObserver(() => sync())
    ro.observe(el)
    return () => ro.disconnect()
  }, [sync])

  if (entries.length === 0) {
    return <p className="text-xs text-muted-foreground">No entries in current range</p>
  }

  return (
    <div className="flex h-full flex-col">
      <div
        ref={scrollRef}
        className={cn('overflow-auto', maxHeightClass ?? 'flex-1 min-h-0')}
        onScroll={sync}
      >
        <div ref={contentRef} style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
          {virtualizer.getVirtualItems().map(virtualItem => {
            const t = entries[virtualItem.index]
            return (
              <div
                key={virtualItem.index}
                data-index={virtualItem.index}
                ref={virtualizer.measureElement}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${virtualItem.start}px)`,
                  paddingBottom: 12,
                }}
              >
                <TextItem name={name} entry={t} runId={runId} loggableId={loggableId} />
              </div>
            )
          })}
        </div>
      </div>
      {showJump && (
        <button
          className="shrink-0 self-start text-[10px] text-blue-400 hover:underline"
          onClick={() => {
            autoScrollRef.current = true
            setShowJump(false)
            if (entries.length > 0) {
              virtualizer.scrollToIndex(entries.length - 1, { align: 'end' })
            }
          }}
        >
          Jump to latest
        </button>
      )}
    </div>
  )
}

function ComparisonText({ loggableId, runIds, fillParent }: {
  loggableId: string
  runIds: string[]
  fillParent?: boolean
}) {
  const runs = useStore(s => s.runs)

  // Union of stream names across the compared runs, first-seen order.
  const names = useMemo(() => {
    const out: string[] = []
    const seen = new Set<string>()
    for (const rid of runIds) {
      const texts = runs.get(rid)?.texts ?? []
      for (const t of texts) {
        if (t.node !== loggableId || seen.has(t.name)) continue
        seen.add(t.name)
        out.push(t.name)
      }
    }
    return out
  }, [runs, runIds, loggableId])

  if (names.length === 0) {
    return <p className="text-xs text-muted-foreground p-2">No text</p>
  }

  return (
    <div className={cn('space-y-4', fillParent && 'h-full min-h-0 overflow-auto')}>
      {names.map(name => (
        <div key={name}>
          <div className="text-xs font-medium text-foreground truncate mb-1">{name}</div>
          <ComparisonGrid runIds={runIds}>
            {(cellRunId) => (
              <ComparisonTextCell runId={cellRunId} loggableId={loggableId} name={name} />
            )}
          </ComparisonGrid>
        </div>
      ))}
    </div>
  )
}

function ComparisonTextCell({ runId, loggableId, name }: { runId: string; loggableId: string; name: string }) {
  const texts = useStore(s => s.runs.get(runId)?.texts ?? [])
  const timelineFilter = useTimelineFilter()

  const entries = useMemo(() => {
    let out = texts.filter(t => t.node === loggableId && t.name === name)
    if (timelineFilter) out = out.filter(t => timelineFilter.matchEntry(t))
    return out
  }, [texts, loggableId, name, timelineFilter])

  if (entries.length === 0) {
    return <p className="text-xs text-muted-foreground p-2">No entries</p>
  }

  return (
    <div className="max-h-[200px] space-y-2 overflow-auto p-1">
      {entries.map((t, i) => (
        <TextItem key={i} name={name} entry={t} runId={runId} loggableId={loggableId} />
      ))}
    </div>
  )
}
