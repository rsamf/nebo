// Process-wide GLB cache keyed by media URL.
//
// A body model is content-addressed and served with an immutable ETag, so
// the same model is byte-identical across every scene, card and run that
// references it. Parsing it once and cloning per instance keeps a
// two-instance comparison of a 40 MB humanoid to one parse and one set of
// GPU buffers.

import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import type { Group } from 'three'

type LoadedModel = { scene: Group }

const cache = new Map<string, Promise<LoadedModel>>()

export function loadGlb(url: string): Promise<LoadedModel> {
  const hit = cache.get(url)
  if (hit) return hit

  // Static import: this module is only reachable from the lazily-loaded
  // SceneViewer, so three stays out of the main bundle either way.
  const promise = new GLTFLoader()
    .loadAsync(url)
    .then(gltf => ({ scene: gltf.scene as Group }))

  // A failed load must not poison the cache — a retry (the daemon was
  // still writing the frame, a transient 404) should be able to succeed.
  promise.catch(() => cache.delete(url))
  cache.set(url, promise)
  return promise
}

/**
 * An independent copy of a loaded model.
 *
 * `clone(true)` shares geometry between instances (the expensive part) but
 * also shares materials, so tinting or fading one instance would bleed into
 * every other. Materials are therefore cloned per instance.
 */
export async function instantiate(url: string): Promise<Group> {
  const { scene } = await loadGlb(url)
  const copy = scene.clone(true)
  copy.traverse(obj => {
    const mesh = obj as unknown as { material?: unknown; isMesh?: boolean }
    if (!mesh.isMesh || !mesh.material) return
    const material = mesh.material as { clone: () => unknown }
    mesh.material = Array.isArray(mesh.material)
      ? (mesh.material as { clone: () => unknown }[]).map(m => m.clone())
      : material.clone()
  })
  return copy
}
