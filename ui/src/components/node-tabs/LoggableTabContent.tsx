import type { NodeTab } from '@/store'
import { NodeText } from './NodeText'
import { NodeMetrics } from './NodeMetrics'
import { NodeImages } from './NodeImages'
import { NodeAudio } from './NodeAudio'

interface LoggableTabContentProps {
  runId: string
  loggableId: string
  tab: NodeTab
  comparisonRunIds?: string[]
  // When true, every tab body fills its parent's height instead of
  // stacking naturally with its own internal cap. Used inside DAG
  // nodes which now have a fixed height — without this the tab
  // bodies would leave empty space below their content.
  fillParent?: boolean
}

export function LoggableTabContent({ runId, loggableId, tab, comparisonRunIds, fillParent }: LoggableTabContentProps) {
  switch (tab) {
    case 'text':
      return <NodeText runId={runId} loggableId={loggableId} comparisonRunIds={comparisonRunIds} fillParent={fillParent} />
    case 'metrics':
      return <NodeMetrics runId={runId} loggableId={loggableId} comparisonRunIds={comparisonRunIds} fillParent={fillParent} />
    case 'images':
      return <NodeImages runId={runId} loggableId={loggableId} comparisonRunIds={comparisonRunIds} fillParent={fillParent} />
    case 'audio':
      return <NodeAudio runId={runId} loggableId={loggableId} comparisonRunIds={comparisonRunIds} fillParent={fillParent} />
  }
}
