import { useMemo } from 'react'
import { useStore } from '@/store'
import type { TextEntry } from '@/lib/api'
import {
  buildStreamPath, buildStreamTree, streamPrefixFor,
  type StreamLeaf, type StreamModality, type StreamModel,
} from '@/lib/streams'

// Resolve each loggableId to its stream prefix using kind + func_name.
function prefixMap(
  graphNodes: Record<string, { func_name: string }> | undefined,
  globalId: string | undefined,
  agentId: string | undefined,
): Map<string, string> {
  const m = new Map<string, string>()
  if (graphNodes) {
    for (const [id, n] of Object.entries(graphNodes)) {
      m.set(id, streamPrefixFor('node', n.func_name, id))
    }
  }
  if (globalId) m.set(globalId, streamPrefixFor('global', '', globalId))
  if (agentId) m.set(agentId, streamPrefixFor('agent', '', agentId))
  return m
}

const EMPTY_MODEL: StreamModel = {
  tree: [], leaves: [], byPath: new Map(),
}

// Incremental accumulator per run. The store appends immutably
// (`[...texts, ...newTexts]` preserves entry identity), so when the new
// texts array extends the processed one we only push the tail instead of
// re-walking every entry on every WS batch. Counter-based and therefore
// idempotent — a memo re-invocation with the same array appends nothing.
interface StreamCache {
  prefixKey: string
  textsProcessed: number
  lastText: TextEntry | undefined
  imagesRef: unknown
  audioRef: unknown
  actionsRef: unknown
  acc: Map<string, StreamLeaf>
}
const cacheByRun = new Map<string, StreamCache>()

export function useStreams(runId: string | null, enabled = true): StreamModel {
  // Select per-field refs rather than the run object so the stream
  // model only recomputes when a field it actually reads changes (the
  // run object itself is cloned on every mutation, including ones —
  // like metric appends — that streams don't care about).
  const texts = useStore(s => (runId ? s.runs.get(runId)?.texts : undefined))
  const loggableImages = useStore(s => (runId ? s.runs.get(runId)?.loggableImages : undefined))
  const loggableAudio = useStore(s => (runId ? s.runs.get(runId)?.loggableAudio : undefined))
  const loggableActions = useStore(s => (runId ? s.runs.get(runId)?.loggableActions : undefined))
  const graphNodes = useStore(s => (runId ? s.runs.get(runId)?.graph?.nodes : undefined))
  const globalId = useStore(s => (runId ? s.runs.get(runId)?.globalLoggable?.loggableId : undefined))
  const agentId = useStore(s => (runId ? s.runs.get(runId)?.agentLoggable?.loggableId : undefined))

  return useMemo(() => {
    if (!enabled || !runId) return EMPTY_MODEL
    const prefixes = prefixMap(graphNodes, globalId, agentId)
    // loggables that emitted before register fall back to their id as prefix
    const prefixFor = (id: string | null): string => {
      if (!id) return ''
      if (prefixes.has(id)) return prefixes.get(id)!
      if (id === '__global__') return ''
      if (id === '__agent__') return 'agent'
      return id
    }
    // Prefix resolution feeds the stream paths; a graph change (new node
    // registered) can re-route a fallback prefix, so it invalidates the
    // incremental cache along with image/audio slice changes.
    const prefixKey = [...prefixes.entries()].map(([k, v]) => `${k}→${v}`).join('|')

    let cache = cacheByRun.get(runId)
    const extendsTexts =
      cache !== undefined
      && cache.prefixKey === prefixKey
      && cache.imagesRef === loggableImages
      && cache.audioRef === loggableAudio
      && cache.actionsRef === loggableActions
      && texts !== undefined
      && texts.length >= cache.textsProcessed
      && (cache.textsProcessed === 0 || texts[cache.textsProcessed - 1] === cache.lastText)

    if (!cache || !extendsTexts) {
      cache = {
        prefixKey,
        textsProcessed: 0,
        lastText: undefined,
        imagesRef: loggableImages,
        audioRef: loggableAudio,
        actionsRef: loggableActions,
        acc: new Map(),
      }
      cacheByRun.set(runId, cache)
    }
    const acc = cache.acc

    const push = (loggableId: string, modality: StreamModality, rawName: string | null, step: number | null, timestamp: number) => {
      const name = rawName && rawName.length ? rawName : modality
      const path = buildStreamPath(prefixFor(loggableId), name)
      let leaf = acc.get(path)
      if (!leaf) {
        leaf = {
          path, segments: path.split('/').filter(Boolean),
          loggableId, modality, name,
          datapoints: [], minStep: null, maxStep: null,
          minTime: Infinity, maxTime: -Infinity,
        }
        acc.set(path, leaf)
      }
      leaf.datapoints.push({ step, timestamp })
      if (step != null) {
        leaf.minStep = leaf.minStep == null ? step : Math.min(leaf.minStep, step)
        leaf.maxStep = leaf.maxStep == null ? step : Math.max(leaf.maxStep, step)
      }
      leaf.minTime = Math.min(leaf.minTime, timestamp)
      leaf.maxTime = Math.max(leaf.maxTime, timestamp)
    }

    if (cache.textsProcessed === 0) {
      // Fresh accumulator: walk everything once.
      if (loggableImages) for (const [id, imgs] of Object.entries(loggableImages)) for (const img of imgs) push(id, 'image', img.name, img.step ?? null, img.timestamp)
      if (loggableAudio) for (const [id, entries] of Object.entries(loggableAudio)) for (const a of entries) push(id, 'audio', a.name, a.step ?? null, a.timestamp)
      if (loggableActions) for (const [id, frames] of Object.entries(loggableActions)) for (const f of frames) push(id, 'action', f.name, f.step ?? null, f.timestamp)
    }
    if (texts) {
      for (let i = cache.textsProcessed; i < texts.length; i++) {
        const t = texts[i]
        push(t.node ?? '__global__', 'text', t.name, t.step ?? null, t.timestamp)
      }
      cache.textsProcessed = texts.length
      cache.lastText = texts[texts.length - 1]
    }

    const leaves = [...acc.values()]
    const byPath = new Map(leaves.map(l => [l.path, l]))
    return { tree: buildStreamTree(leaves), leaves, byPath }
  }, [enabled, runId, texts, loggableImages, loggableAudio, loggableActions, graphNodes, globalId, agentId])
}
