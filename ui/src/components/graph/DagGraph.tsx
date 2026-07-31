import { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useNodesInitialized,
  type Node,
  type Edge,
  type NodeTypes,
  BackgroundVariant,
} from '@xyflow/react'
import dagre from '@dagrejs/dagre'
import '@xyflow/react/dist/style.css'
import { useStore } from '@/store'
import { NeboNode } from './NeboNode'
import { GroupNode } from './GroupNode'
import { NeboEdge } from './NeboEdge'
import { GraphToolbar } from './GraphToolbar'

interface DagGraphProps {
  runId: string
}

const nodeTypes: NodeTypes = {
  nebo: NeboNode,
  classGroup: GroupNode,
}

const edgeTypes = {
  nebo: NeboEdge,
}

export const DragContext = createContext<string | null>(null)

const DEFAULT_WIDTH = 280
const DEFAULT_HEIGHT = 100

function runDagreLayout(
  nodes: Node[],
  edges: Edge[],
  measured?: Map<string, { width: number; height: number }>,
  rankdir: 'TB' | 'LR' = 'TB',
): Node[] {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir, nodesep: 50, ranksep: 60 })
  g.setDefaultEdgeLabel(() => ({}))

  for (const node of nodes) {
    const dims = measured?.get(node.id)
    g.setNode(node.id, {
      width: dims?.width ?? DEFAULT_WIDTH,
      height: dims?.height ?? DEFAULT_HEIGHT,
    })
  }

  for (const edge of edges) {
    g.setEdge(edge.source, edge.target)
  }

  dagre.layout(g)

  return nodes.map(node => {
    const pos = g.node(node.id)
    // dagre returns center coordinates; ReactFlow uses top-left
    return {
      ...node,
      position: {
        x: pos.x - pos.width / 2,
        y: pos.y - pos.height / 2,
      },
    }
  })
}

function getMeasuredDimensions(nodes: Node[]): Map<string, { width: number; height: number }> {
  const dims = new Map<string, { width: number; height: number }>()
  for (const n of nodes) {
    if (n.measured?.width && n.measured?.height) {
      dims.set(n.id, { width: n.measured.width, height: n.measured.height })
    }
  }
  return dims
}

function computeGroupNodes(
  layoutedNodes: Node[],
  graphNodes: Record<string, { group: string | null; [key: string]: unknown }>,
  measured?: Map<string, { width: number; height: number }>,
): Node[] {
  const groups = new Map<string, string[]>()
  for (const [nodeId, nodeData] of Object.entries(graphNodes)) {
    if (nodeData.group) {
      if (!groups.has(nodeData.group)) {
        groups.set(nodeData.group, [])
      }
      groups.get(nodeData.group)!.push(nodeId)
    }
  }

  const groupNodes: Node[] = []
  for (const [groupName, memberIds] of groups) {
    const memberNodes = layoutedNodes.filter(n => memberIds.includes(n.id))
    if (memberNodes.length === 0) continue

    const padding = 24
    const headerHeight = 28
    const minX = Math.min(...memberNodes.map(n => n.position.x)) - padding
    const minY = Math.min(...memberNodes.map(n => n.position.y)) - padding - headerHeight
    const maxX = Math.max(...memberNodes.map(n => {
      const dims = measured?.get(n.id)
      return n.position.x + (dims?.width ?? n.measured?.width ?? DEFAULT_WIDTH)
    })) + padding
    const maxY = Math.max(...memberNodes.map(n => {
      const dims = measured?.get(n.id)
      return n.position.y + (dims?.height ?? n.measured?.height ?? DEFAULT_HEIGHT)
    })) + padding

    const w = maxX - minX
    const h = maxY - minY
    groupNodes.push({
      id: `group-${groupName}`,
      type: 'classGroup',
      position: { x: minX, y: minY },
      data: {
        label: groupName,
        width: w,
        height: h,
      },
      style: { width: w, height: h, zIndex: -1, background: 'transparent', border: 'none' },
      selectable: false,
      draggable: false,
    })
  }

  return groupNodes
}

function DagGraphInner({ runId }: DagGraphProps) {
  const runState = useStore(s => s.runs.get(runId))
  const updateNodePosition = useStore(s => s.updateNodePosition)
  const showMinimap = useStore(s => s.settings.showMinimap)
  const showControls = useStore(s => s.settings.showControls)
  const dagDirection = useStore(s => s.dagDirection)
  const graph = runState?.graph

  const { fitView, getNodes } = useReactFlow()
  const nodesInitialized = useNodesInitialized()
  const [nodes, setNodes, onNodesChange] = useNodesState([] as Node[])
  const [edges, setEdges, onEdgesChange] = useEdgesState([] as Edge[])

  const initialLayoutDone = useRef(false)
  // fitView is a one-shot per run: re-fitting on every structural change
  // would yank the viewport while a live run streams new nodes in.
  const fittedRunRef = useRef<string | null>(null)
  const edgesRef = useRef<Edge[]>([])

  // Keep a ref to graph so the memo can read latest data without depending on the object ref
  const graphRef = useRef(graph)
  graphRef.current = graph

  // Stable key that only changes when graph topology (node set or edge set) changes
  const structureKey = useMemo(() => {
    if (!graph) return ''
    const nodeKeys = Object.keys(graph.nodes).sort().join(',')
    const edgeKeys = graph.edges.map(e => `${e.source}->${e.target}`).sort().join(',')
    const groupKey = Object.entries(graph.nodes)
      .filter(([, n]) => n.group)
      .map(([id, n]) => `${id}@${n.group}`)
      .sort().join(',')
    return `${nodeKeys}|${edgeKeys}|${groupKey}`
  }, [graph])

  // Build base nodes and edges — only recomputes on structural changes, not exec_count/progress
  const { baseNodes, baseEdges } = useMemo(() => {
    const g = graphRef.current
    if (!g) return { baseNodes: [] as Node[], baseEdges: [] as Edge[] }
    const targetSet = new Set(g.edges.map(e => e.target))
    const sourceSet = new Set(g.edges.map(e => e.source))

    const baseNodes: Node[] = Object.keys(g.nodes).map(id => ({
      id,
      type: 'nebo' as const,
      position: { x: 0, y: 0 },
      data: {
        nodeId: id,
        runId,
        inDag: sourceSet.has(id) || targetSet.has(id) || g.nodes[id].is_source,
      },
    }))

    const baseEdges: Edge[] = g.edges.map(e => ({
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      type: 'nebo',
      data: { runId },
    }))

    return { baseNodes, baseEdges }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structureKey, runId])

  // Set initial nodes/edges with default-size dagre layout
  useEffect(() => {
    if (baseNodes.length === 0) {
      setNodes([])
      setEdges([])
      return
    }
    const laid = runDagreLayout(baseNodes, baseEdges, undefined, dagDirection)
    const g = graphRef.current
    const groups = g ? computeGroupNodes(laid, g.nodes) : []
    setNodes([...groups, ...laid])
    setEdges(baseEdges)
    edgesRef.current = baseEdges
    initialLayoutDone.current = false
  }, [baseNodes, baseEdges, setNodes, setEdges, dagDirection])

  // After initial measurement, re-layout with real dimensions. Depends on
  // `nodes` (identity), not `nodes.length`: measurement updates and
  // same-count structural swaps change identity but not length, and this
  // effect must keep retrying until every node has reported real
  // dimensions — otherwise the graph is stuck on the default-size layout
  // with no fit (the old `?run=` deep-link bug).
  useEffect(() => {
    if (!nodesInitialized || initialLayoutDone.current || nodes.length === 0) return

    const currentNodes = getNodes()
    // Filter out group nodes for layout — they aren't in dagre
    const regularNodes = currentNodes.filter(n => n.type !== 'classGroup')
    const dims = getMeasuredDimensions(regularNodes)
    if (dims.size === regularNodes.length) {
      initialLayoutDone.current = true
      const laid = runDagreLayout(regularNodes, edgesRef.current, dims, dagDirection)
      const g = graphRef.current
      const groups = g ? computeGroupNodes(laid, g.nodes, dims) : []
      setNodes([...groups, ...laid])
      if (fittedRunRef.current !== runId) {
        fittedRunRef.current = runId
        requestAnimationFrame(() => fitView({ duration: 200 }))
      }
    }
  }, [nodesInitialized, nodes, getNodes, setNodes, fitView, dagDirection, runId])

  // Relayout only when explicitly triggered (expand/collapse/reset), not on content growth
  const layoutTrigger = useStore(s => s.layoutTrigger)

  useEffect(() => {
    if (!initialLayoutDone.current || layoutTrigger === 0) return

    // Delay for React to measure new dimensions after expand/collapse
    const timer = setTimeout(() => {
      const currentNodes = getNodes()
      const regularNodes = currentNodes.filter(n => n.type !== 'classGroup')
      const dims = getMeasuredDimensions(regularNodes)
      if (dims.size > 0) {
        const laid = runDagreLayout(regularNodes, edgesRef.current, dims, dagDirection)
        const g = graphRef.current
        const groups = g ? computeGroupNodes(laid, g.nodes, dims) : []
        setNodes([...groups, ...laid])
      }
    }, 100)

    return () => clearTimeout(timer)
  }, [layoutTrigger, getNodes, setNodes, dagDirection])

  const onResetLayout = useCallback(() => {
    const currentNodes = getNodes()
    const regularNodes = currentNodes.filter(n => n.type !== 'classGroup')
    const dims = getMeasuredDimensions(regularNodes)
    if (dims.size > 0) {
      const laid = runDagreLayout(regularNodes, edgesRef.current, dims, dagDirection)
      const g = graphRef.current
      const groups = g ? computeGroupNodes(laid, g.nodes, dims) : []
      setNodes([...groups, ...laid])
      requestAnimationFrame(() => fitView({ duration: 200 }))
    }
  }, [getNodes, setNodes, fitView, dagDirection])

  const resizingNodeId = useStore(s => s.resizingNodeId)
  const toggleNodeResize = useStore(s => s.toggleNodeResize)

  // Escape key clears resizing state
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && resizingNodeId !== null) {
        toggleNodeResize(resizingNodeId)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [resizingNodeId, toggleNodeResize])

  const onPaneClick = useCallback(() => {
    if (resizingNodeId !== null) {
      toggleNodeResize(resizingNodeId)
    }
  }, [resizingNodeId, toggleNodeResize])

  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null)

  const onNodeDragStart = useCallback((_: unknown, node: Node) => {
    setDraggingNodeId(node.id)
  }, [])

  const onNodeDragStop = useCallback((_: unknown, node: Node) => {
    setDraggingNodeId(null)
    updateNodePosition(runId, node.id, node.position)
  }, [runId, updateNodePosition])

  if (!graph) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">Loading graph...</p>
      </div>
    )
  }

  return (
    <DragContext.Provider value={draggingNodeId}>
      <div className="h-full w-full relative">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeDragStart={onNodeDragStart}
          onNodeDragStop={onNodeDragStop}
          onPaneClick={onPaneClick}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          minZoom={0.1}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1} className="!bg-background" />
          {showControls && <Controls className="!bg-card !border-border !shadow-sm" />}
          {showMinimap && (
            <MiniMap
              className="!bg-card !border-border"
              nodeColor={() => 'oklch(0.556 0 0)'}
              maskColor="rgba(0, 0, 0, 0.3)"
            />
          )}
          <GraphToolbar onResetLayout={onResetLayout} runId={runId} />
        </ReactFlow>
      </div>
    </DragContext.Provider>
  )
}

export function DagGraph({ runId }: DagGraphProps) {
  return (
    <ReactFlowProvider>
      <DagGraphInner runId={runId} />
    </ReactFlowProvider>
  )
}
