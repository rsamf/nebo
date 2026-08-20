import { ChevronRight, FileText, Image as ImageIcon, AudioLines, Boxes } from 'lucide-react'
import type { FlatRow, StreamModality } from '@/lib/streams'

const ICON: Record<StreamModality, typeof FileText> = { text: FileText, image: ImageIcon, audio: AudioLines, action: Boxes }

// Presentational tree column. The flattened rows + collapse/selection state
// are owned by Tracker so this column shares one scroll container (and stays
// row-aligned) with the canvas. Leaf click → onSelect; branch click → onToggle.
interface Props {
  rows: FlatRow[]
  rowHeight: number
  collapsed: Set<string>
  selectedPath: string | null
  onSelect: (path: string) => void
  onToggle: (path: string) => void
}

export function StreamTree({ rows, rowHeight, collapsed, selectedPath, onSelect, onToggle }: Props) {
  if (rows.length === 0) {
    return <div className="p-3 text-[11px] text-muted-foreground">No streams</div>
  }
  return (
    <div>
      {rows.map(row => {
        // Branch rows that are themselves a stream (/a/b logged alongside
        // /a/b/c) show their modality icon too.
        const Icon = row.leaf ? ICON[row.leaf.modality] : null
        const isSel = row.isLeaf && row.path === selectedPath
        return (
          <div
            key={row.key}
            style={{ height: rowHeight, paddingLeft: 6 + row.depth * 12 }}
            className={`flex items-center gap-1 pr-2 text-[11px] cursor-pointer select-none ${isSel ? 'bg-primary/15 text-foreground' : 'text-muted-foreground hover:bg-muted/50'}`}
            onClick={() => (row.isLeaf ? onSelect(row.path) : onToggle(row.path))}
          >
            {!row.isLeaf ? (
              <ChevronRight size={12} className={`shrink-0 transition-transform ${collapsed.has(row.path) ? '' : 'rotate-90'}`} />
            ) : (
              <span className="w-3 shrink-0" />
            )}
            {Icon && <Icon size={11} className="shrink-0" />}
            <span className="truncate">{row.label}{!row.isLeaf ? '/' : ''}</span>
          </div>
        )
      })}
    </div>
  )
}
