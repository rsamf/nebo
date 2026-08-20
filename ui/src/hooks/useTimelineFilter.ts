import { useMemo } from 'react'
import { useStore } from '@/store'
import { buildStreamPath, streamPrefixFor, type StreamModality } from '@/lib/streams'

interface Filterable {
  timestamp: number
  step?: number | null
}

export interface TimelineFilter {
  matchEntry: (entry: Filterable) => boolean
}

// Playhead filter. Both modes resolve to a single point:
//  - step mode: entries whose step equals the playhead step.
//  - time mode: entries at or before the playhead time ("current frame").
//
// While playback is running the step is PINNED to where playback started.
// Every image and audio clip is its own HTTP fetch, so tracking a 30 fps
// playhead would issue 30 requests per second per panel and render a
// flicker of half-loaded media. 3D scenes are unaffected — their poses are
// already in memory — so they animate while media holds still, and media
// catches up the moment playback stops. Buffering media ahead of the
// playhead is deliberately out of scope for now.
export function useTimelineFilter(): TimelineFilter | null {
  const timeline = useStore(s => s.timeline)

  return useMemo(() => {
    if (timeline.mode === 'time') {
      if (timeline.time == null) return null
      return { matchEntry: (e: Filterable) => e.timestamp <= timeline.time! }
    }
    const step = timeline.playing ? timeline.playbackFrom : timeline.step
    if (step == null) return null
    return { matchEntry: (e: Filterable) => e.step === step }
  }, [timeline.mode, timeline.time, timeline.step, timeline.playing, timeline.playbackFrom])
}

// Stream-selection narrowing. When a stream is selected, content panels keep
// only entries belonging to that exact stream. When nothing is selected,
// isSelected() returns true (no narrowing).
export function useStreamSelection(runId: string | null): {
  selectedStream: string | null
  isSelected: (loggableId: string | null, modality: StreamModality, name: string | null) => boolean
} {
  const selectedStream = useStore(s => s.timeline.selectedStream)
  const run = useStore(s => (runId ? s.runs.get(runId) : undefined))
  const graphNodes = run?.graph?.nodes
  const globalId = run?.globalLoggable?.loggableId
  const agentId = run?.agentLoggable?.loggableId

  return useMemo(() => {
    const prefixFor = (id: string | null): string => {
      if (!id) return ''
      if (id === globalId || id === '__global__') return ''
      if (id === agentId || id === '__agent__') return 'agent'
      const n = graphNodes?.[id]
      return streamPrefixFor('node', n?.func_name ?? '', id)
    }
    return {
      selectedStream,
      isSelected: (loggableId, modality, name) => {
        if (!selectedStream) return true
        const resolved = name && name.length ? name : modality
        return buildStreamPath(prefixFor(loggableId), resolved) === selectedStream
      },
    }
  }, [selectedStream, graphNodes, globalId, agentId])
}
