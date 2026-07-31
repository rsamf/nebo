import { useEffect, useRef } from 'react'
import { useStore, type RunState, type ImageEntry, type AudioEntry } from '@/store'
import { api, type TextEntry, type MetricEntry, type LoggableMetricSeries } from '@/lib/api'

// ─── Non-destructive hydration ────────────────────────────────────────────
//
// REST hydration and the live WebSocket both write the same run slices. If a
// run is opened while still live, the WS appends entries to the store *while*
// the REST fetch is in flight; a plain `set` of the REST snapshot would then
// clobber those WS entries and lose them permanently (`loaded` flips true, so
// there's no refetch). For a finished run there's no WS activity and the merge
// degenerates to "use the REST snapshot", so this is safe in both cases.
//
// Every merge is keyed so the same emission seen on both paths dedupes to one.

function textKey(t: TextEntry): string {
  return `${t.timestamp}|${t.node ?? ''}|${t.step ?? ''}|${t.message}`
}

function mergeTexts(rest: TextEntry[], live: TextEntry[]): TextEntry[] {
  const seen = new Set(rest.map(textKey))
  // REST is the authoritative ordered prefix; append any live-only tail.
  return [...rest, ...live.filter(t => !seen.has(textKey(t)))]
}

function mergeByMediaId<T extends { mediaId: string }>(
  rest: Record<string, T[]>, live: Record<string, T[]>,
): Record<string, T[]> {
  const out: Record<string, T[]> = { ...rest }
  for (const [node, liveEntries] of Object.entries(live)) {
    const base = out[node] ?? []
    const seen = new Set(base.map(e => e.mediaId))
    out[node] = [...base, ...liveEntries.filter(e => !seen.has(e.mediaId))]
  }
  return out
}

function mergeMetricSeries(rest: LoggableMetricSeries, live: LoggableMetricSeries): LoggableMetricSeries {
  const accumulates = rest.type === 'line' || rest.type === 'scatter'
  if (!accumulates) {
    // Snapshot (bar/pie/histogram): exactly one logical value — keep whichever
    // emission is newer rather than unioning stale + fresh.
    const restT = rest.entries[rest.entries.length - 1]?.timestamp ?? 0
    const liveT = live.entries[live.entries.length - 1]?.timestamp ?? 0
    return liveT > restT ? live : rest
  }
  // Accumulating: union by step (steps are unique per accumulating series).
  const byStep = new Map<number | string, MetricEntry>()
  for (const e of rest.entries) byStep.set(e.step ?? `t:${e.timestamp}`, e)
  for (const e of live.entries) {
    const k = e.step ?? `t:${e.timestamp}`
    if (!byStep.has(k)) byStep.set(k, e)
  }
  const entries = [...byStep.values()].sort((a, b) => (a.step ?? 0) - (b.step ?? 0))
  return { ...rest, entries }
}

function mergeMetrics(
  rest: Record<string, Record<string, LoggableMetricSeries>>,
  live: Record<string, Record<string, LoggableMetricSeries>>,
): Record<string, Record<string, LoggableMetricSeries>> {
  const out: Record<string, Record<string, LoggableMetricSeries>> = {}
  for (const lid of new Set([...Object.keys(rest), ...Object.keys(live)])) {
    const r = rest[lid] ?? {}
    const l = live[lid] ?? {}
    const merged: Record<string, LoggableMetricSeries> = { ...r }
    for (const [name, lSeries] of Object.entries(l)) {
      merged[name] = r[name] ? mergeMetricSeries(r[name], lSeries) : lSeries
    }
    out[lid] = merged
  }
  return out
}

function fetchSingleRun(runId: string, store: ReturnType<typeof useStore.getState>) {
  const { setRunGraph, setRunTexts, setRunMetrics, setRunImages, setRunAudio, setRunAlerts, setRunHydrating } = store
  setRunHydrating(runId, true)
  // Read the *current* live slice inside each `.then()` (not from the captured
  // `store` snapshot) so WS entries that landed during the fetch are merged in.
  const current = () => useStore.getState().runs.get(runId)
  return Promise.all([
    api.getRunGraph(runId).then(g => setRunGraph(runId, g)),
    // Daemon returns text entries with `loggable_id` while the UI's
    // TextEntry stores it as `node` (mirroring the WS path). Normalize here
    // so REST-loaded entries match the WS shape.
    api.getRunText(runId, { limit: 500 }).then(d => {
      const normalized: TextEntry[] = d.texts.map((l: unknown) => {
        const e = l as Record<string, unknown>
        return {
          timestamp: e.timestamp as number,
          node: (e.node ?? e.loggable_id ?? null) as string | null,
          name: (e.name as string) ?? 'text',
          message: (e.message ?? '') as string,
          step: (e.step ?? null) as number | null,
        }
      })
      setRunTexts(runId, mergeTexts(normalized, current()?.texts ?? []))
    }),
    // Two-phase metrics hydration: the decimated default paints charts
    // immediately (a million-point run would otherwise be a ~70 MB first
    // response), then EVERY datapoint is delivered — if anything was
    // decimated, a full-fidelity (`points: 0`) fetch replaces the series
    // wholesale. The per-chart "N of M pts" badge only shows during the
    // window between the two, and `hydratingRuns` stays set until the
    // full payload has landed.
    api.getRunMetrics(runId).then(d => {
      setRunMetrics(runId, mergeMetrics(d.metrics, current()?.loggableMetrics ?? {}))
      const anyDownsampled = Object.values(d.metrics).some(byName =>
        Object.values(byName).some(s => s.downsampled),
      )
      if (!anyDownsampled) return
      return api.getRunMetrics(runId, { points: 0 }).then(full =>
        setRunMetrics(runId, mergeMetrics(full.metrics, current()?.loggableMetrics ?? {})),
      )
    }),
    api.getRunImages(runId).then(d => {
      const mapped: Record<string, ImageEntry[]> = {}
      for (const [nodeId, entries] of Object.entries(d.images)) {
        mapped[nodeId] = entries.map(e => ({
          node: e.node,
          mediaId: e.media_id,
          name: e.name,
          step: e.step,
          timestamp: e.timestamp,
          labels: e.labels ?? null,
        }))
      }
      setRunImages(runId, mergeByMediaId(mapped, current()?.loggableImages ?? {}))
    }),
    api.getRunAudio(runId).then(d => {
      const mapped: Record<string, AudioEntry[]> = {}
      for (const [nodeId, entries] of Object.entries(d.audio)) {
        mapped[nodeId] = entries.map(e => ({ node: e.node, mediaId: e.media_id, name: e.name, sr: e.sr, step: e.step, timestamp: e.timestamp }))
      }
      setRunAudio(runId, mergeByMediaId(mapped, current()?.loggableAudio ?? {}))
    }),
    // setRunAlerts merges non-destructively itself (keyed dedupe against
    // WS-appended entries), matching the other slices' merge semantics.
    api.getRunAlerts(runId).then(d => setRunAlerts(runId, d.alerts)),
  ])
    .catch((err) => { console.warn(`[useRunData] Failed to fetch data for run ${runId}:`, err) })
    .finally(() => setRunHydrating(runId, false))
}

export function useRunData(runId: string | null): RunState | null {
  const runs = useStore(s => s.runs)
  const comparisonGroups = useStore(s => s.comparisonGroups)
  const fetchedRef = useRef(new Set<string>())

  const isComparison = runId?.startsWith('cmp:') ?? false
  const group = isComparison && runId ? comparisonGroups.get(runId) : null
  const run = runId && !isComparison ? runs.get(runId) : undefined

  // Fetch data for a single run.
  //
  // Wait until `run` is in the store before fetching: setRunGraph
  // mutates the existing run entry (`runs.get(runId)`), so if we fetch
  // before the WS-driven summaries arrive, the resulting setRunGraph
  // call is a no-op and we'd never retry (fetchedRef would already
  // contain the id). Listing `run` itself in the deps re-runs the
  // effect once it appears.
  useEffect(() => {
    if (!runId || isComparison) return
    if (!run) return
    if (run.loaded) return
    if (fetchedRef.current.has(runId)) return
    fetchedRef.current.add(runId)
    fetchSingleRun(runId, useStore.getState())
  }, [runId, isComparison, run, run?.loaded])

  // Fetch data for all runs in a comparison group
  useEffect(() => {
    if (!group) return
    for (const rid of group.runIds) {
      const r = runs.get(rid)
      if (!r?.loaded && !fetchedRef.current.has(rid)) {
        fetchedRef.current.add(rid)
        fetchSingleRun(rid, useStore.getState())
      }
    }
  }, [group, runs])

  return run ?? null
}
