// Where a `nebo://` deep link that names a loggable (and optionally a
// stream) should land. Desktop (LoggableGridView) and mobile (MobileFeed)
// render different card models but MUST agree on which modality owns a
// given name, so the priority order lives here.

import type { RunState } from '@/store'

export type NavModality = 'text' | 'metric' | 'image' | 'audio' | 'action'

/** Tie-break order when the same name exists in several modalities, and
 *  the fallback order when a link names a loggable but no stream. */
export const NAV_MODALITY_ORDER: NavModality[] = ['text', 'metric', 'image', 'audio', 'action']

/** Every stream name a loggable carries, per modality. Names go through
 *  the same `name || 'media'` fallback the feeds use to group cards, so a
 *  resolved name always reconstructs the card's id exactly. */
function namesByModality(run: RunState, loggableId: string): Record<NavModality, string[]> {
  const dedupe = (entries: { name: string }[] | undefined) => {
    const out: string[] = []
    const seen = new Set<string>()
    for (const e of entries ?? []) {
      const n = e.name || 'media'
      if (seen.has(n)) continue
      seen.add(n)
      out.push(n)
    }
    return out
  }
  return {
    text: dedupe(run.texts.filter(t => (t.node ?? '__global__') === loggableId)),
    metric: Object.keys(run.loggableMetrics[loggableId] ?? {}),
    image: dedupe(run.loggableImages[loggableId]),
    audio: dedupe(run.loggableAudio[loggableId]),
    // Scene frames repeat their name once per step, so the dedupe here is
    // doing real work — unlike the media slices where it is a formality.
    action: dedupe(
      (run.loggableActions[loggableId] ?? []).map(f => ({ name: f.name || 'scene' })),
    ),
  }
}

/**
 * Resolve a link target to the modality + stream name that actually holds
 * it. An exact `name` match wins (searched in NAV_MODALITY_ORDER); a
 * loggable-only link — or a name that no longer exists — falls back to the
 * loggable's first stream so the link still lands somewhere useful.
 * Returns null while the run has no content for that loggable yet.
 */
export function resolveNavModality(
  run: RunState,
  loggableId: string,
  name: string | null,
): { modality: NavModality; name: string } | null {
  const byModality = namesByModality(run, loggableId)
  if (name) {
    for (const modality of NAV_MODALITY_ORDER) {
      if (byModality[modality].includes(name)) return { modality, name }
    }
  }
  for (const modality of NAV_MODALITY_ORDER) {
    const first = byModality[modality][0]
    if (first != null) return { modality, name: first }
  }
  return null
}
