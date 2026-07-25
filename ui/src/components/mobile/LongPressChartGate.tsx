import { useEffect, useRef, useState } from 'react'
import { Chart } from 'chart.js'
import { cn } from '@/lib/utils'

const LONG_PRESS_MS = 350
const MOVE_SLOP_PX = 10

// Touch gate for Chart.js canvases in scrolling mobile lists. The canvas
// itself never receives pointer events (so a swipe over a chart scrolls
// the feed); holding for LONG_PRESS_MS focuses the chart. While focused
// and still pressing, the finger drives the tooltip AND the step filter
// live (onScrubX per animation frame); lifting the finger unfocuses the
// chart and clears the tooltip — the last scrubbed step stays.
export function LongPressChartGate({
  onScrubX,
  className,
  children,
}: {
  // Called (rAF-throttled) with the x of the point under the finger
  // while the chart is focused. Omit for tooltip-only gating
  // (snapshot chart types).
  onScrubX?: (x: number) => void
  className?: string
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [armed, setArmed] = useState(false)
  // Latest-callback ref so the one-time listener effect below never has
  // to re-bind when the parent re-renders with a new closure.
  const onScrubRef = useRef(onScrubX)
  useEffect(() => {
    onScrubRef.current = onScrubX
  }, [onScrubX])

  useEffect(() => {
    const el = ref.current
    if (!el) return

    const armedRef = { current: false }
    const start = { x: 0, y: 0 }
    let timer = 0
    let raf = 0
    let pendingX: number | null = null

    const chartOf = () => {
      const canvas = el.querySelector('canvas')
      return canvas ? Chart.getChart(canvas) : undefined
    }

    // One scrub commit per animation frame — pointermove outpaces the
    // display and every commit re-renders the step-subscribed charts.
    const scheduleScrub = (x: number) => {
      pendingX = x
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        if (pendingX != null) onScrubRef.current?.(pendingX)
      })
    }

    // Highlight the data point(s) under the finger and show the tooltip,
    // exactly what Chart.js would do for a hover it never receives.
    const drive = (ev: TouchEvent) => {
      const chart = chartOf()
      const touch = ev.touches[0]
      if (!chart || !touch) return
      const els = chart.getElementsAtEventForMode(ev, 'index', { intersect: false }, true)
      const active = els.map(e => ({ datasetIndex: e.datasetIndex, index: e.index }))
      chart.setActiveElements(active)
      if (active.length > 0) {
        const d = chart.data.datasets[active[0].datasetIndex]?.data[active[0].index]
        if (d != null && typeof d === 'object' && 'x' in d && typeof d.x === 'number') {
          scheduleScrub(d.x)
        }
        const rect = chart.canvas.getBoundingClientRect()
        chart.tooltip?.setActiveElements(active, {
          x: touch.clientX - rect.left,
          y: touch.clientY - rect.top,
        })
      }
      chart.update('none')
    }

    // Unfocus: drop the highlight and hide the tooltip.
    const clearFocus = () => {
      const chart = chartOf()
      if (!chart) return
      chart.setActiveElements([])
      chart.tooltip?.setActiveElements([], { x: 0, y: 0 })
      chart.update('none')
    }

    const onStart = (ev: TouchEvent) => {
      if (ev.touches.length !== 1) return
      start.x = ev.touches[0].clientX
      start.y = ev.touches[0].clientY
      clearTimeout(timer)
      timer = window.setTimeout(() => {
        armedRef.current = true
        setArmed(true)
        navigator.vibrate?.(10)
        drive(ev)
      }, LONG_PRESS_MS)
    }

    const onMove = (ev: TouchEvent) => {
      if (armedRef.current) {
        // Non-passive listener — this is what keeps the page from
        // scrolling while the user scrubs the chart.
        ev.preventDefault()
        drive(ev)
        return
      }
      const t = ev.touches[0]
      if (t && Math.hypot(t.clientX - start.x, t.clientY - start.y) > MOVE_SLOP_PX) {
        clearTimeout(timer) // the gesture is a scroll — never arm
      }
    }

    const onEnd = () => {
      clearTimeout(timer)
      if (!armedRef.current) return
      armedRef.current = false
      setArmed(false)
      // Flush the last position so the step lands exactly where the
      // finger lifted, then drop focus + tooltip.
      cancelAnimationFrame(raf)
      raf = 0
      if (pendingX != null) {
        onScrubRef.current?.(pendingX)
        pendingX = null
      }
      clearFocus()
    }

    // The browser's long-press context menu (Android) / callout (iOS)
    // fires right on top of our arm gesture — suppress it entirely.
    const onContextMenu = (ev: Event) => ev.preventDefault()

    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchmove', onMove, { passive: false })
    el.addEventListener('touchend', onEnd)
    el.addEventListener('touchcancel', onEnd)
    el.addEventListener('contextmenu', onContextMenu)
    return () => {
      clearTimeout(timer)
      cancelAnimationFrame(raf)
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchmove', onMove)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onEnd)
      el.removeEventListener('contextmenu', onContextMenu)
    }
  }, [])

  return (
    <div
      ref={ref}
      className={cn(
        'relative select-none rounded-md [&_canvas]:pointer-events-none',
        armed && 'ring-1 ring-primary/50',
        className,
      )}
      style={{ WebkitTouchCallout: 'none' }}
    >
      {children}
    </div>
  )
}
