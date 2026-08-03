// Renders a single loggable's tab; works for node- and global-kind loggables.
import { useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useStore, type ImageEntry } from '@/store'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { useMedia } from '@/hooks/useMedia'
import { formatTimestamp } from '@/lib/utils'
import { ComparisonGrid } from '@/components/shared/ComparisonGrid'
import { ImageWithLabels } from '@/components/shared/ImageWithLabels'
import { Modal } from '@/components/ui/modal'
import { HeaderActions } from '@/components/node-tabs/HeaderActions'
import { downloadImageElement } from '@/components/node-tabs/downloadHelpers'
import { buildEmbeddedUrl } from '@/hooks/useEmbeddedView'

interface NodeImagesProps {
  runId: string
  loggableId: string
  comparisonRunIds?: string[]
  // When true, the image list fills the parent's height instead of
  // capping at a fixed pixel value. Used inside fixed-height DAG nodes.
  fillParent?: boolean
}

export function NodeImages({ runId, loggableId, comparisonRunIds, fillParent }: NodeImagesProps) {
  if (comparisonRunIds) {
    return (
      <ComparisonGrid runIds={comparisonRunIds} fillParent={fillParent}>
        {(cellRunId) => <ComparisonImageCell runId={cellRunId} loggableId={loggableId} fillParent={fillParent} />}
      </ComparisonGrid>
    )
  }

  return <SingleRunImages runId={runId} loggableId={loggableId} fillParent={fillParent} />
}

export function ImageItem({ runId, loggableId, img, showTimestamp }: { runId: string; loggableId: string; img: ImageEntry; showTimestamp?: boolean }) {
  const src = useMedia(runId, img.mediaId)
  const [modalOpen, setModalOpen] = useState(false)
  const imgWrapperRef = useRef<HTMLDivElement>(null)

  const iframeUrl = buildEmbeddedUrl({ runId, node: loggableId, image: img.name })

  return (
    <div data-export-atom="image" className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <span className={`${showTimestamp ? 'text-xs' : 'text-[10px]'} font-medium truncate`}>{img.name}</span>
          <span className="text-[10px] text-muted-foreground shrink-0">
            {img.step != null && `step ${img.step}`}
            {showTimestamp && img.step != null && ' · '}
            {showTimestamp && formatTimestamp(img.timestamp)}
          </span>
        </div>
        <HeaderActions
          onExpand={() => setModalOpen(true)}
          onDownloadPng={() => {
            const el = imgWrapperRef.current?.querySelector('img') as HTMLImageElement | null
            downloadImageElement(el, img.name)
          }}
          iframeUrl={iframeUrl}
        />
      </div>
      <div ref={imgWrapperRef}>
        <ImageWithLabels
          src={src}
          labels={img.labels}
          loggableName={loggableId}
          imageName={img.name ?? ''}
          alt={img.name}
        />
      </div>
      {modalOpen && (
        <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={img.name} widthClass="max-w-6xl">
          <ImageWithLabels
            src={src}
            labels={img.labels}
            loggableName={loggableId}
            imageName={img.name ?? ''}
            alt={img.name}
          />
        </Modal>
      )}
    </div>
  )
}

// Only the items inside the viewport (plus overscan) are mounted, so a tab with
// thousands of images stays cheap to switch into and to scroll through. Without
// virtualization, mounting every ImageItem creates one useMedia subscription per
// image plus a real <img>+SVG tree.
export function VirtualizedImageList({
  runId,
  loggableId,
  images,
  showTimestamp,
  maxHeight,
  itemGap = 12,
  estimateSize = 240,
}: {
  runId: string
  loggableId: string
  images: ImageEntry[]
  showTimestamp?: boolean
  // When set, caps the scroll container at this pixel height. When
  // omitted, the list uses `h-full` instead so it fills its flex
  // parent (used in fixed-height DAG nodes).
  maxHeight?: number
  itemGap?: number
  estimateSize?: number
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: images.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimateSize + itemGap,
    overscan: 3,
    getItemKey: (index) => images[index].mediaId,
  })

  return (
    <div
      ref={scrollRef}
      className={maxHeight !== undefined ? 'overflow-auto' : 'overflow-auto h-full'}
      style={maxHeight !== undefined ? { maxHeight } : undefined}
    >
      <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
        {virtualizer.getVirtualItems().map(vi => {
          const img = images[vi.index]
          return (
            <div
              key={vi.key}
              data-index={vi.index}
              ref={virtualizer.measureElement}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${vi.start}px)`,
                paddingBottom: itemGap,
              }}
            >
              <ImageItem runId={runId} loggableId={loggableId} img={img} showTimestamp={showTimestamp} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

export function ComparisonImageCell({ runId, loggableId, name, fillParent }: {
  runId: string
  loggableId: string
  // When set, narrows the cell to one image stream (the flat comparison
  // view cards up per name; the node tab shows the whole loggable).
  name?: string
  fillParent?: boolean
}) {
  const allImages = useStore(s => s.runs.get(runId)?.loggableImages[loggableId]) ?? []
  const timelineFilter = useTimelineFilter()

  const images = useMemo(() => {
    let out = name != null ? allImages.filter(img => img.name === name) : allImages
    if (timelineFilter) out = out.filter(img => timelineFilter.matchEntry(img))
    return out
  }, [allImages, name, timelineFilter])

  if (images.length === 0) {
    return <p className="text-xs text-muted-foreground p-2">No images</p>
  }

  return (
    <VirtualizedImageList
      runId={runId}
      loggableId={loggableId}
      images={images}
      maxHeight={fillParent ? undefined : 280}
    />
  )
}

function SingleRunImages({ runId, loggableId, fillParent }: { runId: string; loggableId: string; fillParent?: boolean }) {
  const allImages = useStore(s => s.runs.get(runId)?.loggableImages[loggableId]) ?? []
  const exportLimit = useStore(s => s.exportEntryLimit)
  const timelineFilter = useTimelineFilter()

  const images = useMemo(() => {
    let out = timelineFilter ? allImages.filter(img => timelineFilter.matchEntry(img)) : allImages
    if (exportLimit) out = out.slice(0, exportLimit)
    return out
  }, [allImages, timelineFilter, exportLimit])

  if (images.length === 0) {
    return <p className="text-xs text-muted-foreground">No images for this node</p>
  }

  return (
    <VirtualizedImageList
      runId={runId}
      loggableId={loggableId}
      images={images}
      showTimestamp
      // In fillParent mode (fixed-height DAG node) drop the cap so the
      // list grows to the flex-allotted height instead of leaving empty
      // space below.
      maxHeight={fillParent ? undefined : 380}
    />
  )
}
