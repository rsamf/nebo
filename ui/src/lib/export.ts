import { toPng, toJpeg, toSvg } from 'html-to-image'
import jsPDF from 'jspdf'
import type { DrawioAtom, DrawioEdge } from '@/components/export/types'

// ─── Download helper ──────────────────────────────────────────────────────

export function trigger(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function dataUrlToBlob(dataUrl: string): Blob {
  const [meta, b64] = dataUrl.split(',', 2)
  const mime = meta.match(/data:([^;]+)/)?.[1] ?? 'application/octet-stream'
  const bytes = atob(b64)
  const arr = new Uint8Array(bytes.length)
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i)
  return new Blob([arr], { type: mime })
}

// ─── Raster paths ─────────────────────────────────────────────────────────

export interface ExportRasterOptions {
  filename: string
  pixelRatio: 1 | 2 | 3 | 4
  transparent: boolean
  backgroundColor: string
}

export async function exportAsPng(el: HTMLElement, opts: ExportRasterOptions): Promise<void> {
  const url = await toPng(el, {
    backgroundColor: opts.transparent ? undefined : opts.backgroundColor,
    pixelRatio: opts.pixelRatio,
    cacheBust: false,
  })
  trigger(dataUrlToBlob(url), opts.filename)
}

export async function exportAsJpg(
  el: HTMLElement,
  opts: Omit<ExportRasterOptions, 'transparent'>,
): Promise<void> {
  const url = await toJpeg(el, {
    backgroundColor: opts.backgroundColor,
    pixelRatio: opts.pixelRatio,
    quality: 0.95,
    cacheBust: false,
  })
  trigger(dataUrlToBlob(url), opts.filename)
}

export async function exportAsSvg(
  el: HTMLElement,
  opts: { filename: string; backgroundColor?: string },
): Promise<void> {
  const url = await toSvg(el, {
    backgroundColor: opts.backgroundColor,
    cacheBust: false,
  })
  trigger(dataUrlToBlob(url), opts.filename)
}

export async function exportAsPdf(el: HTMLElement, opts: ExportRasterOptions): Promise<void> {
  const url = await toPng(el, {
    backgroundColor: opts.transparent ? undefined : opts.backgroundColor,
    pixelRatio: opts.pixelRatio,
    cacheBust: false,
  })
  // Match the PDF page to the captured pixel dimensions so the diagram
  // doesn't get cropped or letterboxed.
  const img = new Image()
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve()
    img.onerror = reject
    img.src = url
  })
  const orientation = img.width >= img.height ? 'landscape' : 'portrait'
  const pdf = new jsPDF({ unit: 'px', format: [img.width, img.height], orientation })
  pdf.addImage(url, 'PNG', 0, 0, img.width, img.height)
  pdf.save(opts.filename)
}

// ─── Drawio path (atom-based) ─────────────────────────────────────────────

function escapeXml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
}

function styleString(parts: Record<string, string | number | undefined>): string {
  return Object.entries(parts)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${v}`)
    .join(';') + ';'
}

function renderAtom(atom: DrawioAtom): string {
  const geom = `<mxGeometry x="${Math.round(atom.x)}" y="${Math.round(atom.y)}" width="${Math.round(atom.width)}" height="${Math.round(atom.height)}" as="geometry"/>`
  const idAttr = `id="${escapeXml(atom.id)}"`
  const parentAttr = `parent="${escapeXml(atom.parentId)}"`

  if (atom.kind === 'group') {
    const style = styleString({
      rounded: 1,
      whiteSpace: 'wrap',
      html: 1,
      fillColor: atom.fillColor ?? '#ffffff',
      strokeColor: atom.strokeColor ?? '#374151',
      strokeWidth: 2,
      verticalAlign: 'top',
      collapsible: 0,
    })
    return `<mxCell ${idAttr} value="" style="${style}" vertex="1" ${parentAttr}>${geom}</mxCell>`
  }

  // Audio atoms become text placeholders in drawio (no playable cell type).
  // We branch on whether the atom carries text vs an image so 'audio' can
  // pick either path — drawio always treats it as text in practice.
  const isText =
    atom.kind === 'title' ||
    atom.kind === 'exec-count' ||
    atom.kind === 'docstring' ||
    atom.kind === 'config-row' ||
    atom.kind === 'text-line' ||
    (atom.kind === 'audio' && atom.text !== undefined)
  if (isText) {
    const fontSize = atom.fontSize ?? 12
    const style = styleString({
      text: undefined,  // marker — drawio uses "text" shape via shape= attr below
      shape: 'text',
      html: 1,
      align: 'left',
      verticalAlign: 'middle',
      whiteSpace: 'wrap',
      strokeColor: 'none',
      fillColor: 'none',
      fontSize: fontSize,
      fontColor: atom.fontColor ?? '#1f2937',
      // Title gets bold; everything else stays normal.
      fontStyle: atom.kind === 'title' ? 1 : 0,
    })
    const value = escapeXml(atom.text ?? '')
    return `<mxCell ${idAttr} value="${value}" style="${style}" vertex="1" ${parentAttr}>${geom}</mxCell>`
  }

  // chart / image / audio — embedded raster.
  //
  // Drawio's style parser splits attributes on `;`, so a literal
  // `data:image/png;base64,...` value would be cut off after the first
  // `;` and the cell renders blank. drawio's own `convertDataUri`
  // helper strips `;base64` from the MIME parameters before storing
  // image styles — and re-adds it when feeding the SVG <image href>.
  // Mirror that here:
  //   `data:image/png;base64,iVBOR…`  →  `data:image/png,iVBOR…`
  // Verified by inspecting drawio's source and visually confirming that
  // cells using the stripped form render correctly in app.diagrams.net.
  const rawImage = atom.imageDataUrl ?? ''
  const drawioImage = rawImage.replace(/^(data:[^;,]+);base64,/, '$1,')
  // `imageAspect=1` preserves the original PNG aspect ratio (drawio's
  // default). The previous `imageAspect=0` stretched images to fill the
  // cell — fine for square nodes but disfiguring for wide chart panels.
  const style = styleString({
    shape: 'image',
    imageAspect: 1,
    image: drawioImage,
    verticalLabelPosition: 'bottom',
    labelBackgroundColor: 'none',
  })
  return `<mxCell ${idAttr} value="" style="${style}" vertex="1" ${parentAttr}>${geom}</mxCell>`
}

function renderEdge(id: string, edge: DrawioEdge, strokeColor: string): string {
  // `curved=1` smooths the line through any pass-through waypoints
  // we emit; without those points it would render as a straight line.
  const style = styleString({
    endArrow: 'classic',
    html: 1,
    strokeColor,
    rounded: 0,
    curved: 1,
  })
  const points = edge.waypoints && edge.waypoints.length > 0
    ? `<Array as="points">` +
      edge.waypoints
        .map(p => `<mxPoint x="${Math.round(p.x)}" y="${Math.round(p.y)}"/>`)
        .join('') +
      `</Array>`
    : ''
  return (
    `<mxCell id="${escapeXml(id)}" edge="1" parent="1" source="${escapeXml(edge.source)}" target="${escapeXml(edge.target)}" style="${style}">` +
    `<mxGeometry relative="1" as="geometry">${points}</mxGeometry>` +
    `</mxCell>`
  )
}

export function buildDrawioXml(
  atoms: DrawioAtom[],
  edges: DrawioEdge[],
  edgeStrokeColor = '#6b7280',
): string {
  const cells: string[] = [
    '<mxCell id="0" />',
    '<mxCell id="1" parent="0" />',
  ]
  for (const atom of atoms) {
    cells.push(renderAtom(atom))
  }
  let edgeIdx = 0
  for (const edge of edges) {
    cells.push(renderEdge(`edge-${edgeIdx++}`, edge, edgeStrokeColor))
  }
  return (
    `<?xml version="1.0" encoding="UTF-8"?>` +
    `<mxfile host="nebo">` +
    `<diagram name="nebo-dag" id="nebo-dag">` +
    `<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100" math="0" shadow="0">` +
    `<root>${cells.join('')}</root>` +
    `</mxGraphModel>` +
    `</diagram>` +
    `</mxfile>`
  )
}

export function exportAsDrawio(
  atoms: DrawioAtom[],
  edges: DrawioEdge[],
  filename = 'nebo-dag.drawio',
  edgeStrokeColor?: string,
): void {
  const xml = buildDrawioXml(atoms, edges, edgeStrokeColor)
  trigger(new Blob([xml], { type: 'application/xml' }), filename)
}
