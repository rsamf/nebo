// Pure helpers shared by every surface that renders a scene: what a run
// contributes to it, and how those contributions become viewer instances.
// Kept out of ActionCard.tsx so that file exports components only.

import { api } from '@/lib/api'
import type { ActionFrame, BodyModelManifest } from '@/lib/api'
import { assignColor } from '@/lib/colors'
import type { SceneInstance } from './SceneViewer'

/** One run's contribution to a scene: which frame, and how to label it. */
export interface SceneSource {
  runId: string
  frame: ActionFrame | null
  bodyModels: Record<string, BodyModelManifest>
  /** Prefix for instance labels; set in comparison views, empty otherwise. */
  prefix?: string
  /** Forced tint for every instance from this run (comparison: run color). */
  runColor?: string
}

/**
 * Build the viewer's instance list from one or more runs' frames.
 *
 * In comparison views every run contributes to the SAME scene — one WebGL
 * context, robots side by side — and instances are tinted by run identity,
 * which is what RUN_COLOR_PALETTE is reserved for. Within a single run,
 * instances are tinted from the palette only when there is more than one:
 * a lone model keeps the colors its author gave it.
 */
export function buildInstances(
  sources: SceneSource[], tintInstances: boolean, hidden: Set<string>,
): SceneInstance[] {
  const out: SceneInstance[] = []
  const singleUntinted =
    sources.length === 1
    && Object.keys(sources[0].frame?.instances ?? {}).length <= 1
  let paletteIndex = 0

  for (const source of sources) {
    const entries = Object.entries(source.frame?.instances ?? {})
    for (const [label, instance] of entries) {
      const model = source.bodyModels[instance.model]
      if (!model?.media_id) continue
      const key = source.prefix ? `${source.prefix}·${label}` : label
      const tint =
        source.runColor
          ?? (tintInstances && !singleUntinted ? assignColor(paletteIndex) : null)
      paletteIndex += 1
      out.push({
        key,
        mediaUrl: api.mediaUrl(source.runId, model.media_id),
        pose: instance.pos_quat_xyzw ?? [],
        tint,
        visible: !hidden.has(key),
      })
    }
  }
  return out
}


/**
 * Where instance `i` of `n` sits on the display grid.
 *
 * Two columns, filled left-to-right then top-to-bottom, centred on the
 * origin: n=2 is a horizontal pair, n=3 is a pair plus one on its own row,
 * n=4 is a 2x2. A lone instance is never moved.
 *
 * This is a DISPLAY transform — the logged world poses are untouched, and
 * body 0 (the world frame's scenery) never moves, so the ground stays put
 * under the grid.
 */
export function gridOffset(
  index: number, count: number, offsetX: number, offsetY: number,
): [number, number] {
  if (count <= 1) return [0, 0]
  const cols = Math.min(count, 2)
  const rows = Math.ceil(count / cols)
  const col = index % cols
  const row = Math.floor(index / cols)
  return [
    (col - (cols - 1) / 2) * offsetX,
    (row - (rows - 1) / 2) * offsetY,
  ]
}
