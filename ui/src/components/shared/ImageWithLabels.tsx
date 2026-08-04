import { useEffect, useState } from 'react'
import { useStore } from '@/store'
import type { LabelsPayload } from '@/lib/api'

// Fallback only — the SDK now requires a color on every label group via
// nb.labels.* dataclasses. Used defensively if a group somehow ships
// without a color (legacy file, hand-built event).
const DEFAULT_COLORS: Record<string, string> = {
  points: '#facc15',
  boxes: '#22d3ee',
  circles: '#f472b6',
  polygons: '#86efac',
  bitmasks: '#a78bfa',
}

type LabelKey = 'points' | 'boxes' | 'circles' | 'polygons' | 'bitmasks'

const ALL_KEYS: LabelKey[] = ['points', 'boxes', 'circles', 'polygons', 'bitmasks']

export function ImageWithLabels({
  src,
  labels,
  loggableName,
  imageName,
  alt,
  className,
  imgClassName,
}: {
  src: string
  labels?: LabelsPayload | null
  loggableName: string
  imageName: string
  alt?: string
  className?: string
  // Replaces the img's default sizing/chrome classes (thumbnail callers
  // need their own constraints). Overlays are stretched over the img's
  // border box, so any override must keep the box at the image's
  // intrinsic aspect ratio — no object-cover/object-contain cropping.
  imgClassName?: string
}) {
  const labelKeySettings = useStore(s => s.labelKeySettings)
  const registerLabelKey = useStore(s => s.registerLabelKey)
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null)

  // Register any keys that have labels present on this image so the
  // Settings pane can surface them.
  useEffect(() => {
    if (!labels) return
    for (const key of ALL_KEYS) {
      const v = labels[key]
      if (v && v.length > 0) {
        registerLabelKey(loggableName, imageName, key)
      }
    }
  }, [labels, loggableName, imageName, registerLabelKey])

  function setting(k: LabelKey) {
    return labelKeySettings[`${loggableName}|${imageName}|${k}`]
  }
  function visible(k: LabelKey): boolean {
    return setting(k)?.visible ?? true
  }
  function opacity(k: LabelKey): number {
    return (setting(k)?.opacity ?? 70) / 100
  }
  function colorFor(k: LabelKey, group: { color?: string }): string {
    return group.color || DEFAULT_COLORS[k]
  }

  const stroke = Math.max(1, (dims?.w ?? 200) / 200)
  const pointR = Math.max(1.5, (dims?.w ?? 200) / 200)

  return (
    <div className={`relative inline-block ${className ?? ''}`}>
      <img
        src={src}
        alt={alt ?? imageName}
        className={imgClassName ?? 'max-w-full block rounded border border-border'}
        onLoad={(e) => {
          const el = e.currentTarget
          setDims({ w: el.naturalWidth, h: el.naturalHeight })
        }}
      />
      {labels && dims && (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none"
          viewBox={`0 0 ${dims.w} ${dims.h}`}
          preserveAspectRatio="none"
        >
          {labels.points && visible('points') && labels.points.map((group, gi) => (
            <g key={`p-${gi}`} opacity={opacity('points')} fill={colorFor('points', group)}>
              {group.data.map(([x, y], i) => (
                <circle key={i} cx={x} cy={y} r={pointR} />
              ))}
            </g>
          ))}
          {labels.boxes && visible('boxes') && labels.boxes.map((group, gi) => (
            <g
              key={`b-${gi}`}
              opacity={opacity('boxes')}
              stroke={colorFor('boxes', group)}
              fill="none"
              strokeWidth={stroke}
            >
              {group.data.map(([x1, y1, x2, y2], i) => (
                <rect
                  key={i}
                  x={x1}
                  y={y1}
                  width={x2 - x1}
                  height={y2 - y1}
                />
              ))}
            </g>
          ))}
          {labels.circles && visible('circles') && labels.circles.map((group, gi) => (
            <g
              key={`c-${gi}`}
              opacity={opacity('circles')}
              stroke={colorFor('circles', group)}
              fill="none"
              strokeWidth={stroke}
            >
              {group.data.map(([x, y, r], i) => (
                <circle key={i} cx={x} cy={y} r={r} />
              ))}
            </g>
          ))}
          {labels.polygons && visible('polygons') && labels.polygons.map((group, gi) => {
            const c = colorFor('polygons', group)
            const filled = group.fill !== false
            return (
              <g
                key={`pg-${gi}`}
                opacity={opacity('polygons')}
                stroke={c}
                fill={filled ? c : 'none'}
                strokeWidth={stroke}
              >
                {group.data.map((poly, i) => (
                  <polygon
                    key={i}
                    points={poly.map(p => `${p[0]},${p[1]}`).join(' ')}
                  />
                ))}
              </g>
            )
          })}
        </svg>
      )}
      {labels?.bitmasks && visible('bitmasks') && labels.bitmasks.flatMap((group, gi) =>
        group.data.map((m, i) => {
          const url = `url(data:image/png;base64,${m.data})`
          // The wire bitmask is a single-channel grayscale PNG (PIL mode
          // "L"): 0 where the mask is off, 255 where it's on. CSS
          // `mask-image` defaults to `mask-mode: match-source` which for
          // a PNG resolves to `alpha` — and a grayscale PNG has no alpha
          // channel, so the browser treats every pixel as fully opaque
          // and the background color covers the whole image. Force
          // `mask-mode: luminance` so 0=hide and 255=show.
          // `maskMode` is missing from React's CSSProperties typing
          // even though every modern browser (and Safari ≥15.4)
          // implements it; the `as React.CSSProperties` cast keeps the
          // rest of the style object strictly typed while letting the
          // mask-mode declarations land in the DOM.
          const style: React.CSSProperties = {
            opacity: opacity('bitmasks'),
            backgroundColor: colorFor('bitmasks', group),
            WebkitMaskImage: url,
            maskImage: url,
            WebkitMaskSize: '100% 100%',
            maskSize: '100% 100%',
            WebkitMaskRepeat: 'no-repeat',
            maskRepeat: 'no-repeat',
            ...({ WebkitMaskMode: 'luminance', maskMode: 'luminance' } as React.CSSProperties),
          }
          return (
            <div
              key={`bm-${gi}-${i}`}
              className="absolute inset-0 w-full h-full pointer-events-none"
              style={style}
            />
          )
        })
      )}
    </div>
  )
}
