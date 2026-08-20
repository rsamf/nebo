// Pure helpers for deriving stream paths and assembling the stream tree.
// A "stream" is a named series of datapoints within one loggable. Its full
// path is /<prefix>/<name> where prefix is the function name (nodes),
// "agent" (the __agent__ loggable), or "" (global, root-level).
//
// Metrics are deliberately absent from the stream tree; an action scene is
// present because each of its frames is a datapoint at a step, exactly like
// an image.

export type StreamModality = 'text' | 'image' | 'audio' | 'action'

export const STREAM_MODALITIES: StreamModality[] = ['text', 'image', 'audio', 'action']

// Single source of the per-modality accent color — the desktop tracker
// (chips + dot rows) and the mobile tracker sheet must agree.
export const MODALITY_COLORS: Record<StreamModality, string> = {
  text: '#3b82f6',
  image: '#22c55e',
  audio: '#f97316',
  action: '#a855f7',
}

export interface StreamDatapoint {
  step: number | null
  timestamp: number
}

export interface StreamLeaf {
  path: string           // "/foo/train/loss"
  segments: string[]     // ["foo","train","loss"]
  loggableId: string
  modality: StreamModality
  name: string           // entry name within the loggable, e.g. "train/loss"
  datapoints: StreamDatapoint[]
  minStep: number | null
  maxStep: number | null
  minTime: number
  maxTime: number
}

export interface StreamTreeNode {
  key: string                  // segment label at this depth
  path: string                 // full path through this segment ("/foo", "/foo/train")
  children: StreamTreeNode[]
  leaf: StreamLeaf | null      // set when a stream terminates exactly here
}

export interface StreamModel {
  tree: StreamTreeNode[]
  leaves: StreamLeaf[]
  byPath: Map<string, StreamLeaf>
}

export function streamPrefixFor(
  kind: 'node' | 'global' | 'agent',
  funcName: string,
  loggableId: string,
): string {
  if (kind === 'global') return ''
  if (kind === 'agent') return 'agent'
  return funcName || loggableId
}

export function buildStreamPath(prefix: string, name: string): string {
  const segs = [
    ...(prefix ? prefix.split('/') : []),
    ...(name ? name.split('/') : []),
  ].filter(Boolean)
  return '/' + segs.join('/')
}

// Assemble a nested tree from flat leaves. Intermediate path segments become
// branch nodes (leaf === null); a leaf attaches at the node whose path equals
// the leaf's path. Children are sorted: branches before leaves, then by key.
export function buildStreamTree(leaves: StreamLeaf[]): StreamTreeNode[] {
  const roots: StreamTreeNode[] = []
  const index = new Map<string, StreamTreeNode>()

  const getNode = (segments: string[]): StreamTreeNode => {
    let path = ''
    let siblings = roots
    let node: StreamTreeNode | undefined
    for (const seg of segments) {
      path = path + '/' + seg
      node = index.get(path)
      if (!node) {
        node = { key: seg, path, children: [], leaf: null }
        index.set(path, node)
        siblings.push(node)
      }
      siblings = node.children
    }
    return node!
  }

  for (const leaf of leaves) {
    const node = getNode(leaf.segments)
    node.leaf = leaf
  }

  const sortRec = (nodes: StreamTreeNode[]) => {
    nodes.sort((a, b) => {
      const aBranch = a.children.length > 0
      const bBranch = b.children.length > 0
      if (aBranch !== bBranch) return aBranch ? -1 : 1
      return a.key.localeCompare(b.key)
    })
    for (const n of nodes) sortRec(n.children)
  }
  sortRec(roots)
  return roots
}

// A single flattened row shared by the tree column and the canvas, so both
// render the same ordered list at the same row height (keeping them aligned).
export interface FlatRow {
  key: string          // unique row key (the node path)
  label: string        // display label (this node's path segment, e.g. "loss")
  path: string
  depth: number
  isLeaf: boolean
  leaf: StreamLeaf | null
  // Collapsed branch rows carry every visible descendant leaf here; the
  // canvas merges their datapoints onto the single branch row instead of
  // dropping them with the hidden child rows.
  mergedLeaves?: StreamLeaf[]
}

// Flatten the tree into display rows, honoring collapse + search query +
// active modalities. A leaf row shows when its modality is active and (no
// query OR its path matches). A branch shows when it has any visible
// descendant leaf; a collapsed branch hides its children but renders their
// merged datapoints on its own row. A node can be both a stream and a parent
// (/a/b logged alongside /a/b/c): its own leaf renders on the branch row.
export function flattenRows(
  nodes: StreamTreeNode[],
  collapsed: Set<string>,
  query: string,
  activeModalities: Set<StreamModality>,
): FlatRow[] {
  const q = query.trim().toLowerCase()
  const leafVisible = (leaf: StreamLeaf) =>
    activeModalities.has(leaf.modality) && (!q || leaf.path.toLowerCase().includes(q))
  const hasVisibleLeaf = (node: StreamTreeNode): boolean => {
    if (node.leaf && leafVisible(node.leaf)) return true
    return node.children.some(hasVisibleLeaf)
  }
  const collectVisible = (node: StreamTreeNode, into: StreamLeaf[]) => {
    if (node.leaf && leafVisible(node.leaf)) into.push(node.leaf)
    for (const c of node.children) collectVisible(c, into)
  }
  const out: FlatRow[] = []
  const walk = (ns: StreamTreeNode[], depth: number) => {
    for (const n of ns) {
      const isLeaf = n.leaf != null && n.children.length === 0
      if (isLeaf) {
        if (leafVisible(n.leaf!)) out.push({ key: n.path, label: n.key, path: n.path, depth, isLeaf: true, leaf: n.leaf })
        continue
      }
      if (!hasVisibleLeaf(n)) continue
      if (collapsed.has(n.path)) {
        const merged: StreamLeaf[] = []
        collectVisible(n, merged)
        out.push({ key: n.path, label: n.key, path: n.path, depth, isLeaf: false, leaf: null, mergedLeaves: merged })
      } else {
        const ownLeaf = n.leaf && leafVisible(n.leaf) ? n.leaf : null
        out.push({ key: n.path, label: n.key, path: n.path, depth, isLeaf: false, leaf: ownLeaf })
        walk(n.children, depth + 1)
      }
    }
  }
  walk(nodes, 0)
  return out
}
