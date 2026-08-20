// Parser for the node-naming contract that nebo's GLB exporter writes.
//
// TWIN PARSER: `nebo/extras/robotics/gltf.py` produces exactly these names.
// The two must be kept in lockstep, the same way `nebo/core/refs.py` and
// `ui/src/lib/refs.ts` are.
//
//   nebo:body:<index>:<body name>     one node per body, in model body order
//     nebo:geom:visual:<n>            child geoms, posed in the body frame
//     nebo:geom:collision:<n>
//
// Body nodes are exported at identity and are placeholders: the viewer
// overwrites their transform every frame from the logged pose array. Geom
// nodes carry fixed body-local poses and are never touched at runtime.

export const BODY_PREFIX = 'nebo:body:'
export const GEOM_PREFIX = 'nebo:geom:'

export type GeomKind = 'visual' | 'collision'

/** "nebo:body:3:pelvis" -> { index: 3, name: 'pelvis' }; null if not a body. */
export function parseBodyNode(nodeName: string): { index: number; name: string } | null {
  if (!nodeName.startsWith(BODY_PREFIX)) return null
  const rest = nodeName.slice(BODY_PREFIX.length)
  const sep = rest.indexOf(':')
  if (sep < 0) return null
  const index = Number(rest.slice(0, sep))
  if (!Number.isInteger(index) || index < 0) return null
  // A body name may itself contain ':' — everything after the first
  // separator is the name.
  return { index, name: rest.slice(sep + 1) }
}

/** "nebo:geom:collision:2" -> 'collision'; null if not a geom node. */
export function parseGeomKind(nodeName: string): GeomKind | null {
  if (!nodeName.startsWith(GEOM_PREFIX)) return null
  const rest = nodeName.slice(GEOM_PREFIX.length)
  if (rest.startsWith('visual:')) return 'visual'
  if (rest.startsWith('collision:')) return 'collision'
  return null
}

/** Floats per body in a logged pose array: [x, y, z, qx, qy, qz, qw]. */
export const POSE_STRIDE = 7
