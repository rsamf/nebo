// Dependency-free polyline sparkline for run cards, DAG node previews and
// collapsed feed rows. Chart.js is deliberately avoided here — dozens of
// these render in scrolling lists.
export function Sparkline({
  values,
  color,
  width = 72,
  height = 24,
  strokeWidth = 2,
  className,
}: {
  values: number[]
  color: string
  width?: number
  height?: number
  strokeWidth?: number
  className?: string
}) {
  if (values.length < 2) return null
  let min = Infinity
  let max = -Infinity
  for (const v of values) {
    if (v < min) min = v
    if (v > max) max = v
  }
  const range = max - min || 1
  // 2px vertical inset so the stroke never clips at the extremes.
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100
      const y = 2 + (1 - (v - min) / range) * 24
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 100 28"
      preserveAspectRatio="none"
      className={className}
      aria-hidden
    >
      <polyline points={pts} fill="none" stroke={color} strokeWidth={strokeWidth} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
