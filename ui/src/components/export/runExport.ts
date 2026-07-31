import { createRoot, type Root } from 'react-dom/client'
import { createElement } from 'react'
import { toPng } from 'html-to-image'
import { useStore } from '@/store'
import {
  exportAsPng,
  exportAsJpg,
  exportAsSvg,
  exportAsPdf,
  exportAsDrawio,
} from '@/lib/export'
import { DiagramExportRoot, type NodePos } from './DiagramExportRoot'
import type { DrawioAtom, DrawioEdge, ExportOptions } from './types'

interface Rect { x: number; y: number; width: number; height: number }

// Produce two pass-through points for a bezier-style edge between two
// group rects. Mirrors React Flow's bezier path: control points sit a
// short distance perpendicular to whichever side of the source/target
// the edge naturally exits/enters. With `curved=1` in the drawio style,
// the edge interpolates a smooth S-curve through these points.
function computeBezierWaypoints(source: Rect, target: Rect): { x: number; y: number }[] {
  const scx = source.x + source.width / 2
  const scy = source.y + source.height / 2
  const tcx = target.x + target.width / 2
  const tcy = target.y + target.height / 2
  const dx = tcx - scx
  const dy = tcy - scy
  const vertical = Math.abs(dy) >= Math.abs(dx)

  if (vertical) {
    // Source exits its bottom edge (or top edge if target is above).
    const goDown = dy >= 0
    const exitY = goDown ? source.y + source.height : source.y
    const entryY = goDown ? target.y : target.y + target.height
    const gap = Math.abs(entryY - exitY)
    const offset = Math.max(24, gap * 0.4)
    return [
      { x: scx, y: exitY + (goDown ? offset : -offset) },
      { x: tcx, y: entryY - (goDown ? offset : -offset) },
    ]
  }

  // Horizontal layout: source exits right (or left if target is to its left).
  const goRight = dx >= 0
  const exitX = goRight ? source.x + source.width : source.x
  const entryX = goRight ? target.x : target.x + target.width
  const gap = Math.abs(entryX - exitX)
  const offset = Math.max(24, gap * 0.4)
  return [
    { x: exitX + (goRight ? offset : -offset), y: scy },
    { x: entryX - (goRight ? offset : -offset), y: tcy },
  ]
}

function themeColors(theme: 'dark' | 'light') {
  if (theme === 'light') {
    return {
      background: '#ffffff',
      groupFill: '#ffffff',
      groupStroke: '#d1d5db',
      fontColor: '#1f2937',
      mutedFontColor: '#6b7280',
      edgeStroke: '#9ca3af',
    }
  }
  return {
    background: '#0a0a0a',
    groupFill: '#1f2937',
    groupStroke: '#374151',
    fontColor: '#e5e7eb',
    mutedFontColor: '#9ca3af',
    edgeStroke: '#6b7280',
  }
}

function parsePx(s: string): number {
  const n = parseFloat(s)
  return Number.isFinite(n) ? n : 12
}

function rgbToHex(s: string, fallback: string): string {
  if (!s) return fallback
  const m = s.match(/rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)/)
  if (!m) return fallback
  const r = parseInt(m[1], 10)
  const g = parseInt(m[2], 10)
  const b = parseInt(m[3], 10)
  const hex = (x: number) => x.toString(16).padStart(2, '0')
  return `#${hex(r)}${hex(g)}${hex(b)}`
}

async function imgElementToDataUrl(img: HTMLImageElement): Promise<string> {
  if (img.src.startsWith('data:')) return img.src
  // Bypass canvas-tainting for non-data URLs by going through html-to-image.
  try {
    return await toPng(img, { cacheBust: true, pixelRatio: 1 })
  } catch {
    return ''
  }
}

async function walkDrawioAtoms(
  root: HTMLElement,
  options: ExportOptions,
): Promise<{ atoms: DrawioAtom[]; nodeIds: Set<string> }> {
  const rootRect = root.getBoundingClientRect()
  const atoms: DrawioAtom[] = []
  const nodeIds = new Set<string>()
  const colors = themeColors(options.theme)

  const groupEls = Array.from(
    root.querySelectorAll<HTMLElement>('[data-export-atom="node"]'),
  )

  for (const groupEl of groupEls) {
    const nodeId = groupEl.getAttribute('data-node-id')
    if (!nodeId) continue
    nodeIds.add(nodeId)
    const groupRect = groupEl.getBoundingClientRect()
    const gx = groupRect.left - rootRect.left
    const gy = groupRect.top - rootRect.top

    // Group cell — strokeColor honors the ui_hints.color border the
    // ExportNode applies inline when `showColorHints` is on.
    const groupComputedStroke = rgbToHex(
      window.getComputedStyle(groupEl).borderColor,
      colors.groupStroke,
    )
    const useGroupStroke =
      options.showColorHints ? groupComputedStroke : colors.groupStroke

    atoms.push({
      id: nodeId,
      parentId: '1',
      kind: 'group',
      x: gx,
      y: gy,
      width: groupRect.width,
      height: groupRect.height,
      fillColor: colors.groupFill,
      strokeColor: useGroupStroke,
    })

    // applyEntryLimit hides atoms beyond the limit via display:none.
    // Those have a 0×0 bounding rect, so the per-element rect guard
    // below skips them naturally.

    // Document-order walk of every annotated atom inside the group.
    const atomEls = Array.from(
      groupEl.querySelectorAll<HTMLElement>('[data-export-atom]'),
    )
    let atomIdx = 0
    for (const el of atomEls) {
      const rawKind = el.getAttribute('data-export-atom')
      // 'node' is the sentinel for the per-card outer wrapper, not an
      // atom in its own right — skip it here.
      if (!rawKind || rawKind === 'node') continue
      const kind = rawKind as DrawioAtom['kind']

      const r = el.getBoundingClientRect()
      // Skip elements that have collapsed to zero size (e.g. virtualizer
      // not yet measured). They contribute nothing to the export.
      if (r.width <= 0 || r.height <= 0) continue
      const rx = r.left - groupRect.left
      const ry = r.top - groupRect.top
      const id = `${nodeId}/${kind}/${atomIdx++}`

      if (
        kind === 'title' ||
        kind === 'exec-count' ||
        kind === 'docstring' ||
        kind === 'config-row' ||
        kind === 'text-line'
      ) {
        const cs = window.getComputedStyle(el)
        atoms.push({
          id,
          parentId: nodeId,
          kind,
          x: rx,
          y: ry,
          width: r.width,
          height: r.height,
          text: (el.textContent ?? '').trim(),
          fontSize: parsePx(cs.fontSize),
          fontColor: rgbToHex(cs.color, colors.fontColor),
        })
      } else if (kind === 'chart') {
        // The metric block contains a name row + tag/label chip rows + a
        // chart canvas. Rasterizing the whole element preserves all of
        // those pieces with their relative layout, instead of cropping
        // to just the canvas bitmap.
        try {
          const dataUrl = await toPng(el, { backgroundColor: undefined, pixelRatio: 1 })
          if (dataUrl) {
            atoms.push({
              id,
              parentId: nodeId,
              kind,
              x: rx,
              y: ry,
              width: r.width,
              height: r.height,
              imageDataUrl: dataUrl,
            })
          }
        } catch { /* ignore render failure for a single chart */ }
      } else if (kind === 'audio') {
        // Drawio has no playable audio cell. Emit a small placeholder
        // text so the user sees that an audio attachment was here.
        const cs = window.getComputedStyle(el)
        const name = el.querySelector<HTMLElement>('.font-medium')?.textContent?.trim() ?? 'audio'
        atoms.push({
          id,
          parentId: nodeId,
          kind,
          x: rx,
          y: ry,
          width: r.width,
          height: r.height,
          text: `[audio] ${name}`,
          fontSize: parsePx(cs.fontSize),
          fontColor: rgbToHex(cs.color, colors.fontColor),
        })
      } else if (kind === 'image') {
        const img = el.querySelector('img')
        if (img) {
          const dataUrl = await imgElementToDataUrl(img)
          if (dataUrl) {
            atoms.push({
              id,
              parentId: nodeId,
              kind,
              x: rx,
              y: ry,
              width: r.width,
              height: r.height,
              imageDataUrl: dataUrl,
            })
          }
        }
      }
    }

  }

  return { atoms, nodeIds }
}

export async function runExport(
  options: ExportOptions,
  runId: string,
  livePositions?: Map<string, NodePos>,
): Promise<void> {
  const colors = themeColors(options.theme)

  // Mount DiagramExportRoot offscreen.
  const host = document.createElement('div')
  host.style.position = 'fixed'
  host.style.left = '-100000px'
  host.style.top = '0px'
  host.style.pointerEvents = 'none'
  host.style.zIndex = '-1'
  document.body.appendChild(host)

  // Tell the panels to slice down to N entries while the offscreen tree
  // is rendering. The live UI shares the same store but is hidden behind
  // the export modal during this window.
  useStore.getState().setExportEntryLimit(options.entriesPerNode)

  const root: Root = createRoot(host)
  try {
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const onReady = (rootEl: HTMLElement) => {
        if (settled) return
        settled = true
        ;(host as HTMLElement & { __rootEl?: HTMLElement }).__rootEl = rootEl
        resolve()
      }
      try {
        root.render(
          createElement(DiagramExportRoot, {
            runId,
            options,
            livePositions: options.autoLayout ? undefined : livePositions,
            onReady,
          }),
        )
      } catch (err) {
        reject(err)
      }
      // Hard timeout so a busted render never deadlocks the modal.
      window.setTimeout(() => {
        if (!settled) {
          settled = true
          reject(new Error('Export timed out before the diagram finished rendering.'))
        }
      }, 8000)
    })

    const rootEl = (host as HTMLElement & { __rootEl?: HTMLElement }).__rootEl
    if (!rootEl) throw new Error('Export root element missing.')

    const filename = `nebo-dag.${options.format === 'drawio' ? 'drawio' : options.format}`

    if (options.format === 'drawio') {
      const { atoms, nodeIds } = await walkDrawioAtoms(rootEl, options)
      const graph = useStore.getState().runs.get(runId)?.graph
      // Index group atoms by id so we can derive bezier waypoints from
      // each edge's source/target rectangle.
      const groupRects = new Map<string, { x: number; y: number; width: number; height: number }>()
      for (const atom of atoms) {
        if (atom.kind === 'group') {
          groupRects.set(atom.id, { x: atom.x, y: atom.y, width: atom.width, height: atom.height })
        }
      }
      const edges: DrawioEdge[] = (graph?.edges ?? [])
        .filter(e => nodeIds.has(e.source) && nodeIds.has(e.target))
        .map(e => {
          const s = groupRects.get(e.source)
          const t = groupRects.get(e.target)
          return {
            source: e.source,
            target: e.target,
            waypoints: s && t ? computeBezierWaypoints(s, t) : undefined,
          }
        })
      exportAsDrawio(atoms, edges, filename, colors.edgeStroke)
      return
    }

    const transparentForFormat =
      options.transparentBackground &&
      (options.format === 'png' || options.format === 'pdf')

    if (options.format === 'png') {
      await exportAsPng(rootEl, {
        filename,
        pixelRatio: options.pixelRatio,
        transparent: transparentForFormat,
        backgroundColor: colors.background,
      })
    } else if (options.format === 'jpg') {
      await exportAsJpg(rootEl, {
        filename,
        pixelRatio: options.pixelRatio,
        backgroundColor: colors.background,
      })
    } else if (options.format === 'svg') {
      await exportAsSvg(rootEl, { filename, backgroundColor: colors.background })
    } else if (options.format === 'pdf') {
      await exportAsPdf(rootEl, {
        filename,
        pixelRatio: options.pixelRatio,
        transparent: transparentForFormat,
        backgroundColor: colors.background,
      })
    }
  } finally {
    try { root.unmount() } catch { /* ignore */ }
    if (host.parentNode) {
      host.parentNode.removeChild(host)
    }
    useStore.getState().setExportEntryLimit(null)
  }
}
