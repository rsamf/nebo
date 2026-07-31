export type ConfigStyle = 'none' | 'chips' | 'complete'
export type TabbedContent = 'none' | 'active'
export type Theme = 'dark' | 'light'
export type Padding = 'tight' | 'comfortable'
export type ExportFormat = 'png' | 'jpg' | 'pdf' | 'svg' | 'drawio'

export interface ExportOptions {
  format: ExportFormat
  autoLayout: boolean
  configStyle: ConfigStyle
  showExecCount: boolean
  showDocstring: boolean
  tabbedContent: TabbedContent
  // Cap on how many entries (text, images, audio, charts) appear per node.
  // 1 keeps each node compact; higher values let through more content.
  entriesPerNode: number
  theme: Theme
  showColorHints: boolean
  padding: Padding
  transparentBackground: boolean
  pixelRatio: 1 | 2 | 3 | 4
}

export const DEFAULT_EXPORT_OPTIONS: ExportOptions = {
  format: 'png',
  autoLayout: true,
  configStyle: 'chips',
  showExecCount: false,
  showDocstring: false,
  tabbedContent: 'active',
  entriesPerNode: 1,
  theme: 'dark',
  showColorHints: false,
  padding: 'comfortable',
  transparentBackground: false,
  pixelRatio: 2,
}

export type DrawioAtomKind =
  | 'group'
  | 'title'
  | 'exec-count'
  | 'docstring'
  | 'config-row'
  | 'text-line'
  | 'chart'
  | 'image'
  | 'audio'

export interface DrawioAtom {
  id: string
  parentId: string
  kind: DrawioAtomKind
  x: number
  y: number
  width: number
  height: number
  text?: string
  imageDataUrl?: string
  fontSize?: number
  fontColor?: string
  fillColor?: string
  strokeColor?: string
}

export interface DrawioEdge {
  source: string
  target: string
  // Optional pass-through points the edge curve interpolates through.
  // Combined with `curved=1` in the drawio style this produces a smooth
  // bezier-style arc between source and target instead of a straight line.
  waypoints?: { x: number; y: number }[]
}
