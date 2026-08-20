// Parser for the node-naming contract that nebo's GLB exporter writes.
//
// TWIN PARSER: `nebo/extras/robotics/gltf.py` produces exactly these names.
// The two must be kept in lockstep, the same way `nebo/core/refs.py` and
// `ui/src/lib/refs.ts` are.
//
//   nebo__body__<index>__<name>       one node per body, in model body order
//     nebo__geom__visual__<n>         child geoms, posed in the body frame
//     nebo__geom__collision__<n>
//
// The separator is `__` because three's GLTFLoader runs every node name
// through PropertyBinding.sanitizeNodeName, which DELETES the characters
// `[ ] . : /`. A colon-delimited name would arrive here as
// "nebobody1pelvis" and nothing would ever be posed.
//
// Body nodes are exported at identity and are placeholders: the viewer
// overwrites their transform every frame from the logged pose array. Geom
// nodes carry fixed body-local poses and are never touched at runtime.

export const BODY_PREFIX = 'nebo__body__'
export const GEOM_PREFIX = 'nebo__geom__'

export type GeomKind = 'visual' | 'collision'

/** "nebo__body__3__pelvis" -> { index: 3, name: 'pelvis' }; null otherwise. */
export function parseBodyNode(nodeName: string): { index: number; name: string } | null {
  if (!nodeName.startsWith(BODY_PREFIX)) return null
  const rest = nodeName.slice(BODY_PREFIX.length)
  const sep = rest.indexOf('__')
  if (sep < 0) return null
  const index = Number(rest.slice(0, sep))
  if (!Number.isInteger(index) || index < 0) return null
  // A body name may itself contain '__' — everything after the first
  // separator is the name.
  return { index, name: rest.slice(sep + 2) }
}

/** "nebo__geom__collision__2" -> 'collision'; null if not a geom node. */
export function parseGeomKind(nodeName: string): GeomKind | null {
  if (!nodeName.startsWith(GEOM_PREFIX)) return null
  const rest = nodeName.slice(GEOM_PREFIX.length)
  if (rest.startsWith('visual__')) return 'visual'
  if (rest.startsWith('collision__')) return 'collision'
  return null
}

/** Floats per body in a logged pose array: [x, y, z, qx, qy, qz, qw]. */
export const POSE_STRIDE = 7
