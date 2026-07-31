import { useCallback } from 'react'
import { Link as LinkIcon } from 'lucide-react'
import { ContextMenu } from '@/components/shared/ContextMenu'
import { ContextMenuItem } from '@/components/shared/ContextMenuItem'
import { buildEmbeddedUrl } from '@/hooks/useEmbeddedView'

/**
 * What the card represents in the grid view. Used to pick the right
 * URL-param slice when building an iframe URL via `buildEmbeddedUrl`.
 */
export type GridCardKind = 'text' | 'metric' | 'image' | 'audio'

interface GridCardContextMenuProps {
  isOpen: boolean
  position: { x: number; y: number }
  onClose: () => void
  runId: string
  kind: GridCardKind
  loggableId: string
  // The stream/metric/media name this card shows.
  name?: string
}

export function GridCardContextMenu({
  isOpen,
  position,
  onClose,
  runId,
  kind,
  loggableId,
  name,
}: GridCardContextMenuProps) {
  const handleCopyIframeUrl = useCallback(() => {
    // The Global loggable carries logs/metrics emitted outside any @nb.fn
    // context; embedding it without a node= filter gives the unfiltered
    // panel, which matches what users see in the grid card.
    const node = loggableId === '__global__' ? undefined : loggableId
    const spec = (() => {
      switch (kind) {
        case 'text':
          return { runId, node, text: name ?? true }
        case 'metric':
          return { runId, node, metric: name }
        case 'image':
          return { runId, node, image: name }
        case 'audio':
          return { runId, node, audio: name }
      }
    })()
    void navigator.clipboard?.writeText(buildEmbeddedUrl(spec))
    onClose()
  }, [runId, kind, loggableId, name, onClose])

  return (
    <ContextMenu isOpen={isOpen} position={position} onClose={onClose}>
      <ContextMenuItem
        label="Copy iframe URL"
        icon={<LinkIcon className="w-4 h-4" />}
        onClick={handleCopyIframeUrl}
      />
    </ContextMenu>
  )
}
