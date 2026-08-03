// Renders a single loggable's tab; works for node- and global-kind loggables.
import { useMemo } from 'react'
import { useStore, type AudioEntry } from '@/store'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { useMedia } from '@/hooks/useMedia'
import { formatTimestamp } from '@/lib/utils'
import { ComparisonGrid } from '@/components/shared/ComparisonGrid'
import { cn } from '@/lib/utils'

interface NodeAudioProps {
  runId: string
  loggableId: string
  comparisonRunIds?: string[]
  // When true, fill the parent's height and scroll internally instead
  // of letting the audio list stack at its natural intrinsic height.
  // Used inside fixed-height DAG nodes.
  fillParent?: boolean
}

export function NodeAudio({ runId, loggableId, comparisonRunIds, fillParent }: NodeAudioProps) {
  if (comparisonRunIds) {
    return (
      <ComparisonGrid runIds={comparisonRunIds} fillParent={fillParent}>
        {(cellRunId) => <ComparisonAudioCell runId={cellRunId} loggableId={loggableId} fillParent={fillParent} />}
      </ComparisonGrid>
    )
  }

  return <SingleRunAudio runId={runId} loggableId={loggableId} fillParent={fillParent} />
}

export function AudioItem({ runId, entry, showTimestamp }: { runId: string; entry: AudioEntry; showTimestamp?: boolean }) {
  const src = useMedia(runId, entry.mediaId)

  return (
    <div data-export-atom="audio" className="space-y-1">
      <div className="flex items-center justify-between">
        <span className={`${showTimestamp ? 'text-xs' : 'text-[10px]'} font-medium`}>{entry.name}</span>
        <span className="text-[10px] text-muted-foreground">
          {entry.sr}Hz
          {showTimestamp && ` · ${formatTimestamp(entry.timestamp)}`}
        </span>
      </div>
      <audio
        controls
        src={src}
        className="w-full h-8"
      />
    </div>
  )
}

export function ComparisonAudioCell({ runId, loggableId, name, fillParent }: {
  runId: string
  loggableId: string
  // When set, narrows the cell to one audio stream (the flat comparison
  // view cards up per name; the node tab shows the whole loggable).
  name?: string
  fillParent?: boolean
}) {
  const allAudioEntries = useStore(s => s.runs.get(runId)?.loggableAudio[loggableId]) ?? []
  const timelineFilter = useTimelineFilter()

  const audioEntries = useMemo(() => {
    let out = name != null ? allAudioEntries.filter(e => e.name === name) : allAudioEntries
    if (timelineFilter) out = out.filter(entry => timelineFilter.matchEntry(entry))
    return out
  }, [allAudioEntries, name, timelineFilter])

  if (audioEntries.length === 0) {
    return <p className="text-xs text-muted-foreground p-2">No audio</p>
  }

  return (
    <div className={cn('p-1', fillParent && 'h-full overflow-auto')}>
      <div className="space-y-2">
        {audioEntries.map((entry) => (
          <AudioItem key={entry.mediaId} runId={runId} entry={entry} />
        ))}
      </div>
    </div>
  )
}

function SingleRunAudio({ runId, loggableId, fillParent }: { runId: string; loggableId: string; fillParent?: boolean }) {
  const allAudioEntries = useStore(s => s.runs.get(runId)?.loggableAudio[loggableId]) ?? []
  const exportLimit = useStore(s => s.exportEntryLimit)
  const timelineFilter = useTimelineFilter()

  const audioEntries = useMemo(() => {
    let out = timelineFilter ? allAudioEntries.filter(e => timelineFilter.matchEntry(e)) : allAudioEntries
    if (exportLimit) out = out.slice(0, exportLimit)
    return out
  }, [allAudioEntries, timelineFilter, exportLimit])

  if (audioEntries.length === 0) {
    return <p className="text-xs text-muted-foreground">No audio for this node</p>
  }

  // In fillParent mode the list scrolls inside its own flex-1 box so
  // the parent's overflow-auto doesn't double up on us.
  return (
    <div className={cn(fillParent ? 'h-full overflow-auto' : undefined)}>
      <div className="space-y-3">
        {audioEntries.map((entry) => (
          <AudioItem key={entry.mediaId} runId={runId} entry={entry} showTimestamp />
        ))}
      </div>
    </div>
  )
}
