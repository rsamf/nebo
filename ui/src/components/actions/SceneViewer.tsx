// Imperative three.js renderer for one action scene.
//
// LAZY BY CONTRACT: this module is the only place that imports `three`, and
// it is only ever reached through `React.lazy`, so the ~170 KB gzip 3D stack
// lands in its own chunk and never downloads for a run without robotics data.
// Do not import this file eagerly from anywhere.
//
// The renderer is deliberately dumb: it is handed a fully-resolved list of
// instances (which GLB, where each body is, how to tint it) and owns nothing
// about steps, scenes or runs.

import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { instantiate } from './glbCache'
import { POSE_STRIDE, parseBodyNode, parseGeomKind } from './sceneNodes'
import { gridOffset } from './sceneSources'

export interface SceneInstance {
  /** Stable identity within the scene; changing it rebuilds the instance. */
  key: string
  /** URL of the instance's GLB (the media endpoint). */
  mediaUrl: string
  /** Flat pose array: 7 floats per body, in the model's body order. */
  pose: number[]
  /** CSS color to tint the whole instance with, or null to keep its own. */
  tint: string | null
  visible: boolean
}

export interface SceneViewerProps {
  instances: SceneInstance[]
  bodyOpacity: number
  showCollision: boolean
  collisionOpacity: number
  /** Grid spacing (metres) between instances; 0 keeps logged positions. */
  offsetX: number
  offsetY: number
  className?: string
}

interface Loaded {
  root: THREE.Group
  mediaUrl: string
  bodies: Map<number, THREE.Object3D>
  visual: THREE.Mesh[]
  collision: THREE.Mesh[]
  /** Meshes belonging to body 0 — the world frame's static scenery. */
  world: Set<THREE.Mesh>
  /** Base colors captured before any tint, so tinting is reversible. */
  baseColors: Map<THREE.Material, THREE.Color>
}

function meshMaterials(mesh: THREE.Mesh): THREE.Material[] {
  return Array.isArray(mesh.material) ? mesh.material : [mesh.material]
}

export default function SceneViewer({
  instances, bodyOpacity, showCollision, collisionOpacity,
  offsetX, offsetY, className,
}: SceneViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const controlsRef = useRef<OrbitControls | null>(null)
  const loadedRef = useRef(new Map<string, Loaded>())
  const framedRef = useRef(false)
  const mountedRef = useRef(true)
  // Bumped whenever a GLB finishes loading. Models arrive asynchronously,
  // usually AFTER the last `instances` change, so without this the pose
  // and appearance effects would never re-run for them and the model would
  // sit at its rest pose with default materials.
  const [loadedVersion, setLoadedVersion] = useState(0)

  // --- renderer lifecycle (mount once) ---
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 500)
    // MJCF and URDF are both Z-up. Telling the camera and controls that is
    // the whole adaptation — the logged transforms stay untouched.
    camera.up.set(0, 0, 1)
    camera.position.set(1.6, -1.6, 1.1)

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    container.appendChild(renderer.domElement)
    renderer.domElement.style.width = '100%'
    renderer.domElement.style.height = '100%'
    renderer.domElement.style.display = 'block'

    scene.add(new THREE.HemisphereLight(0xffffff, 0x334155, 2.0))
    const key = new THREE.DirectionalLight(0xffffff, 1.6)
    key.position.set(3, -4, 6)
    scene.add(key)

    const grid = new THREE.GridHelper(10, 20, 0x64748b, 0x334155)
    // GridHelper lies in XZ; rotate it into the XY plane for a Z-up world.
    grid.rotation.x = Math.PI / 2
    const gridMat = grid.material as THREE.Material | THREE.Material[]
    for (const m of Array.isArray(gridMat) ? gridMat : [gridMat]) {
      m.transparent = true
      m.opacity = 0.25
    }
    scene.add(grid)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.12
    controls.target.set(0, 0, 0.4)

    sceneRef.current = scene
    cameraRef.current = camera
    controlsRef.current = controls

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = container
      if (w === 0 || h === 0) return
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h, false)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(container)

    // A grid of scenes should not each burn a rAF while scrolled away.
    let onScreen = true
    const io = new IntersectionObserver(
      ([entry]) => { onScreen = entry.isIntersecting },
      { rootMargin: '100px' },
    )
    io.observe(container)

    // Captured for the cleanup below: the ref's identity is stable for the
    // component's lifetime, but reading it in cleanup trips the lint rule.
    const loadedInstances = loadedRef.current
    mountedRef.current = true

    let frame = 0
    const tick = () => {
      frame = requestAnimationFrame(tick)
      if (!onScreen) return
      controls.update()
      renderer.render(scene, camera)
    }
    tick()

    return () => {
      mountedRef.current = false
      cancelAnimationFrame(frame)
      ro.disconnect()
      io.disconnect()
      controls.dispose()
      // Instance geometry is shared through the GLB cache and must NOT be
      // disposed here; only this viewer's own materials and the context.
      for (const loaded of loadedInstances.values()) {
        for (const mesh of [...loaded.visual, ...loaded.collision]) {
          for (const m of meshMaterials(mesh)) m.dispose()
        }
      }
      loadedInstances.clear()
      renderer.dispose()
      renderer.domElement.remove()
      sceneRef.current = null
      cameraRef.current = null
      controlsRef.current = null
    }
  }, [])

  // --- reconcile instances against the scene graph ---
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene) return
    const wanted = new Map(instances.map(i => [i.key, i]))

    for (const [key, loaded] of [...loadedRef.current]) {
      const want = wanted.get(key)
      // A url change means a different model under the same label; rebuild.
      if (want && want.mediaUrl === loaded.mediaUrl) continue
      scene.remove(loaded.root)
      for (const mesh of [...loaded.visual, ...loaded.collision]) {
        for (const m of meshMaterials(mesh)) m.dispose()
      }
      loadedRef.current.delete(key)
    }

    for (const instance of instances) {
      if (loadedRef.current.has(instance.key)) continue
      // Claim the slot synchronously so a re-render mid-load does not
      // start a second instantiation of the same instance.
      loadedRef.current.set(instance.key, {
        root: new THREE.Group(), mediaUrl: instance.mediaUrl,
        bodies: new Map(), visual: [], collision: [],
        world: new Set(), baseColors: new Map(),
      })
      void instantiate(instance.mediaUrl).then(root => {
        // Deliberately NOT guarded by an effect-scoped "cancelled" flag:
        // this effect re-runs on every frame, and cancelling an in-flight
        // load would strand the slot claimed above as an empty placeholder
        // forever. The slot's own identity is the correct guard.
        const slot = loadedRef.current.get(instance.key)
        if (!mountedRef.current || !slot || slot.mediaUrl !== instance.mediaUrl) return

        const bodies = new Map<number, THREE.Object3D>()
        const visual: THREE.Mesh[] = []
        const collision: THREE.Mesh[] = []
        const world = new Set<THREE.Mesh>()
        const baseColors = new Map<THREE.Material, THREE.Color>()
        root.traverse(obj => {
          const body = parseBodyNode(obj.name)
          if (body) {
            bodies.set(body.index, obj)
            // Body nodes are exported as direct children of the GLB root
            // (see gltf.py), so writing a body's local transform IS its
            // world transform — no parent chain to compensate for.
            obj.matrixAutoUpdate = true
          }
          const mesh = obj as THREE.Mesh
          if (!mesh.isMesh) return
          // A geom node may be the mesh itself or its parent, and its
          // owning body is further up, so resolve both from the nearest
          // tagged ancestor.
          let scan: THREE.Object3D | null = obj
          let resolved: 'visual' | 'collision' | null = null
          let bodyIndex: number | null = null
          while (scan) {
            if (!resolved) resolved = parseGeomKind(scan.name)
            if (bodyIndex == null) {
              const owner = parseBodyNode(scan.name)
              if (owner) bodyIndex = owner.index
            }
            scan = scan.parent
          }
          ;(resolved === 'collision' ? collision : visual).push(mesh)
          if (bodyIndex === 0) world.add(mesh)
          for (const m of meshMaterials(mesh)) {
            const colored = m as THREE.Material & { color?: THREE.Color }
            if (colored.color) baseColors.set(m, colored.color.clone())
          }
        })

        slot.root = root
        slot.bodies = bodies
        slot.visual = visual
        slot.collision = collision
        slot.world = world
        slot.baseColors = baseColors
        scene.add(root)
        framedRef.current = false
        setLoadedVersion(v => v + 1)
      })
    }
  }, [instances])

  // --- write poses (every frame change) ---
  useEffect(() => {
    const visibleCount = instances.filter(i => i.visible).length
    let slot = 0
    for (const instance of instances) {
      const loaded = loadedRef.current.get(instance.key)
      if (!loaded) continue
      loaded.root.visible = instance.visible
      // Grid slots are assigned over VISIBLE instances only, so hiding one
      // from the legend re-packs the grid instead of leaving a hole.
      const [dx, dy] = instance.visible
        ? gridOffset(slot++, visibleCount, offsetX, offsetY)
        : [0, 0]
      const pose = instance.pose
      for (const [index, node] of loaded.bodies) {
        const at = index * POSE_STRIDE
        // A pose array shorter than the model's body count (a truncated or
        // mismatched frame) leaves the remaining bodies where they were
        // rather than collapsing them onto the origin.
        if (at + POSE_STRIDE > pose.length) continue
        // Body 0 is the world frame — the ground and other scenery. It
        // stays where it was logged so the grid sits ON the floor rather
        // than dragging a copy of it around.
        const shift = index === 0 ? 0 : 1
        node.position.set(
          pose[at] + dx * shift, pose[at + 1] + dy * shift, pose[at + 2],
        )
        node.quaternion.set(
          pose[at + 3], pose[at + 4], pose[at + 5], pose[at + 6],
        )
      }
    }

    // Frame the scene once, on the first frame that has real geometry —
    // re-framing on every step would yank the camera during playback.
    if (framedRef.current) return
    const camera = cameraRef.current
    const controls = controlsRef.current
    if (!camera || !controls) return
    // Frame on the ARTICULATED bodies, not the whole model. Body 0 is the
    // world/base frame, which typically holds the ground plane and other
    // static scenery — a 20 m floor would shrink a 0.5 m arm to a speck.
    // A model whose only body is the world falls back to framing that.
    const box = new THREE.Box3()
    let any = false
    for (const instance of instances) {
      const loaded = loadedRef.current.get(instance.key)
      if (!loaded || loaded.bodies.size === 0 || !instance.visible) continue
      const movable = [...loaded.bodies.entries()].filter(([i]) => i !== 0)
      const framed = movable.length > 0
        ? movable.map(([, node]) => node)
        : [loaded.root]
      for (const node of framed) box.expandByObject(node)
      any = true
    }
    if (!any || box.isEmpty()) return
    const sphere = box.getBoundingSphere(new THREE.Sphere())
    if (!Number.isFinite(sphere.radius) || sphere.radius <= 0) return
    const distance = sphere.radius / Math.sin((camera.fov * Math.PI) / 360)
    controls.target.copy(sphere.center)
    camera.position.copy(sphere.center).add(
      new THREE.Vector3(1, -1, 0.7).normalize().multiplyScalar(distance * 1.25),
    )
    camera.near = Math.max(sphere.radius / 500, 0.005)
    camera.far = distance * 12
    camera.updateProjectionMatrix()
    controls.update()
    framedRef.current = true
  }, [instances, loadedVersion, offsetX, offsetY])

  // --- appearance: opacity, collision visibility, per-instance tint ---
  useEffect(() => {
    // Body 0 is the world frame: the ground plane and any other static
    // scenery. It belongs to the SCENE, not to an instance — so only the
    // first visible instance draws it (otherwise two robots mean two
    // coincident floors that z-fight) and it is never tinted.
    let primary = true
    for (const instance of instances) {
      const loaded = loadedRef.current.get(instance.key)
      if (!loaded) continue
      const drawsWorld = primary
      if (instance.visible) primary = false
      const apply = (
        meshes: THREE.Mesh[], opacity: number, shown: boolean,
        collision: boolean,
      ) => {
        for (const mesh of meshes) {
          const isWorld = loaded.world.has(mesh)
          // The ground is scenery, not a body. Fading it with the model
          // opacity slider only makes the scene murky and lets you see
          // through the floor, so visual world geometry stays fully
          // opaque and visible regardless of the slider.
          const scenery = isWorld && !collision
          const effective = scenery ? 1 : opacity
          mesh.visible = isWorld ? (drawsWorld && (scenery || shown)) : shown
          for (const m of meshMaterials(mesh)) {
            m.opacity = effective
            // Transparency is enabled only when it is actually needed:
            // an always-transparent material sorts badly and costs fill.
            m.transparent = effective < 1
            m.depthWrite = effective >= 1
            const colored = m as THREE.Material & { color?: THREE.Color }
            if (!colored.color) continue
            const base = loaded.baseColors.get(m)
            if (instance.tint && !isWorld) colored.color.set(instance.tint)
            else if (base) colored.color.copy(base)
          }
        }
      }
      apply(loaded.visual, bodyOpacity, bodyOpacity > 0, false)
      apply(
        loaded.collision, collisionOpacity,
        showCollision && collisionOpacity > 0, true,
      )
    }
  }, [instances, bodyOpacity, showCollision, collisionOpacity, loadedVersion])

  // The canvas is always mounted, even with no instances: remounting it
  // would strand the WebGL context created by the effect above.
  return <div ref={containerRef} className={className} />
}
