import { memo } from 'react'
import { getBezierPath, type EdgeProps } from '@xyflow/react'

export const NeboEdge = memo(function NeboEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
}: EdgeProps) {
  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  })

  return (
    <path
      id={id}
      d={edgePath}
      fill="none"
      stroke="oklch(0.556 0 0)"
      strokeWidth={2}
    />
  )
})
