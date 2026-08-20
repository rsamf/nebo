import { useMemo } from 'react'
import { useStore } from '@/store'
import type { ActionFrame } from '@/lib/api'

const EMPTY: ActionFrame[] = []

/**
 * Every scene name a loggable has logged frames for, in first-seen order.
 */
export function useActionScenes(runId: string, loggableId: string): string[] {
  const frames = useStore(s => s.runs.get(runId)?.loggableActions[loggableId])
  return useMemo(() => {
    const names: string[] = []
    const seen = new Set<string>()
    for (const frame of frames ?? EMPTY) {
      const name = frame.name || 'scene'
      if (seen.has(name)) continue
      seen.add(name)
      names.push(name)
    }
    return names
  }, [frames])
}

/** One scene's frames, in step order. Pure — safe to call in a loop. */
export function sceneFrames(
  all: ActionFrame[] | undefined, name: string,
): ActionFrame[] {
  const mine = (all ?? EMPTY).filter(f => (f.name || 'scene') === name)
  // The daemon returns frames in step order and WS appends in emission
  // order, but a decimated-then-full hydration can interleave, so sort
  // defensively — the picker below binary-searches this array.
  return mine.every((f, i) => i === 0 || (f.step ?? 0) >= (mine[i - 1].step ?? 0))
    ? mine
    : [...mine].sort((a, b) => (a.step ?? 0) - (b.step ?? 0) || a.timestamp - b.timestamp)
}

/** One scene's frames, in step order. */
export function useSceneFrames(
  runId: string, loggableId: string, name: string,
): ActionFrame[] {
  const frames = useStore(s => s.runs.get(runId)?.loggableActions[loggableId])
  return useMemo(() => sceneFrames(frames, name), [frames, name])
}

/** Index of the last entry whose key is <= target, or -1. */
function lastAtOrBefore(frames: ActionFrame[], key: (f: ActionFrame) => number, target: number): number {
  let lo = 0
  let hi = frames.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (key(frames[mid]) <= target) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/**
 * The frame a scene should render right now.
 *
 * Follows the shared tracker playhead — the step (or time) at or nearest
 * *below* it, so a scene logged every 10 steps still shows its most recent
 * pose when the playhead sits between frames. With no playhead set, the
 * latest frame is the run's current state.
 *
 * Returns null only while the scene genuinely has no frames yet.
 */
export function pickFrame(
  frames: ActionFrame[],
  mode: 'step' | 'time',
  step: number | null,
  time: number | null,
): ActionFrame | null {
  if (frames.length === 0) return null
  const target = mode === 'step' ? step : time
  if (target == null) return frames[frames.length - 1]
  const key = mode === 'step'
    ? (f: ActionFrame) => f.step ?? 0
    : (f: ActionFrame) => f.timestamp
  const found = mode === 'step'
    ? lastAtOrBefore(frames, key, target)
    : lastAtOrBefore([...frames].sort((a, b) => a.timestamp - b.timestamp), key, target)
  // A playhead before the scene's first frame shows its first pose
  // rather than an empty viewport.
  return found < 0 ? frames[0] : frames[found]
}

export function useActionFrame(
  runId: string, loggableId: string, name: string,
): ActionFrame | null {
  const frames = useSceneFrames(runId, loggableId, name)
  const mode = useStore(s => s.timeline.mode)
  const step = useStore(s => s.timeline.step)
  const time = useStore(s => s.timeline.time)
  return useMemo(
    () => pickFrame(frames, mode, step, time), [frames, mode, step, time],
  )
}
