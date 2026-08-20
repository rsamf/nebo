import { useEffect, useRef } from 'react'
import { useStore } from '@/store'

/**
 * Drives the shared tracker playhead forward while `timeline.playing`.
 *
 * Playback advances `timeline.step` at `timeline.fps` steps per second, so a
 * 3D scene, its metric guidelines, its images and its text all move together
 * — the whole reason playback lives on the tracker rather than on one card.
 *
 * Time mode has a different domain (wall-clock timestamps, unevenly spaced),
 * so pressing play flips to step mode. That is the same flip clicking a chart
 * datapoint already performs (`selectTimelineStep`).
 *
 * A rAF accumulator is used rather than setInterval so the step rate stays
 * honest when the tab throttles: a long frame advances several steps rather
 * than silently slowing the episode down.
 */
export function usePlayback(minStep: number, maxStep: number, hasSteps: boolean) {
  const playing = useStore(s => s.timeline.playing)
  const fps = useStore(s => s.timeline.fps)
  const setStep = useStore(s => s.setTimelineStep)
  const setPlaying = useStore(s => s.setPlaying)
  const carry = useRef(0)

  useEffect(() => {
    if (!playing) return
    if (!hasSteps || maxStep <= minStep) {
      setPlaying(false)
      return
    }

    // Replaying from the end restarts rather than sitting on the last frame.
    const at = useStore.getState().timeline.step
    if (at == null || at >= maxStep) setStep(minStep)

    let last = performance.now()
    carry.current = 0
    let frame = requestAnimationFrame(function tick(now) {
      carry.current += ((now - last) / 1000) * fps
      last = now
      const advance = Math.floor(carry.current)
      if (advance > 0) {
        carry.current -= advance
        const current = useStore.getState().timeline.step ?? minStep
        const next = current + advance
        if (next >= maxStep) {
          setStep(maxStep)
          setPlaying(false)
          return
        }
        setStep(next)
      }
      frame = requestAnimationFrame(tick)
    })
    return () => cancelAnimationFrame(frame)
  }, [playing, fps, minStep, maxStep, hasSteps, setStep, setPlaying])
}
