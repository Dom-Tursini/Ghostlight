<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { useSensor } from '../composables/useSensor'

const props = defineProps({
  mode:     { type: String, default: 'empty' },   // empty | scanning | cloud | mesh
  coverage: { type: Number, default: 0 },
  showBox:  { type: Boolean, default: false }
})

const host = ref(null)
const { sensor, onFrame, onModel, onMesh, setView } = useSensor()

/* Kinect v1 depth intrinsics at 640x480. Microsoft's nominal focal length is
   NUI_CAMERA_DEPTH_NOMINAL_FOCAL_LENGTH_IN_PIXELS = 285.63 at 320x240, so
   571.26 here, with the principal point at the sensor centre.

   These MUST be scaled to whatever resolution actually arrives. The display
   stream is sent at half resolution to keep bandwidth down, and back-projecting
   320x240 pixels with 640x480 intrinsics makes (u - 320) negative for every
   pixel, which shifted the whole live cloud up and to the left, outside the
   sensor's own frustum. */
const FX_FULL = 571.26
const CX_FULL = 320
const CY_FULL = 240

const MAX_POINTS = 640 * 480
const MAX_MODEL = 260000
const SCENE_Z = -2                     // where we park the subject

let renderer, scene, camera, controls, points, box, raf, ro
let positions, colors, geom
let mPoints, mPositions, mColors, mGeom
let rig
let unsub = null, unsubModel = null, unsubMesh = null
let meshObj = null
let live = false
let hasModel = false
let surfaceMode = false

/* ---- depth ramp: near = accent mint, far = deep blue ------------------- */
const RAMP = (() => {
  // Multi-stop so depth separates across a room, not just the first metre.
  const STOPS = [
    [0.00, [0.00, 1.00, 0.68]],   // near  mint
    [0.30, [0.00, 0.83, 0.85]],   //       cyan
    [0.55, [0.12, 0.50, 0.92]],   //       blue
    [0.78, [0.42, 0.26, 0.78]],   //       violet
    [1.00, [0.20, 0.08, 0.30]]    // far   deep plum
  ]
  const r = new Float32Array(256 * 3)
  for (let i = 0; i < 256; i++) {
    const t = i / 255
    let a = STOPS[0], b = STOPS[STOPS.length - 1]
    for (let k = 0; k < STOPS.length - 1; k++) {
      if (t >= STOPS[k][0] && t <= STOPS[k + 1][0]) { a = STOPS[k]; b = STOPS[k + 1]; break }
    }
    const f = (t - a[0]) / Math.max(1e-6, b[0] - a[0])
    for (let c = 0; c < 3; c++) r[i * 3 + c] = a[1][c] + (b[1][c] - a[1][c]) * f
  }
  return r
})()

/* ---- the mock subject, used when no sensor is attached ----------------- */
function mockCloud () {
  let seed = 1337
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296)
  const profile = (y) => {
    const t = (y + 1) / 2
    const g = (c, w) => Math.exp(-Math.pow((t - c) / w, 2))
    return 0.085 + 0.40 * g(0.30, 0.26) + 0.12 * g(0.02, 0.07) + 0.14 * g(0.98, 0.075)
  }
  let n = 0
  for (let i = 0; i < 90000; i++) {
    const y = rnd() * 2 - 1
    const th = rnd() * Math.PI * 2
    const r = profile(y) + (rnd() - 0.5) * 0.010
    positions[n * 3]     = Math.cos(th) * r
    positions[n * 3 + 1] = y * 0.55
    positions[n * 3 + 2] = SCENE_Z + Math.sin(th) * r
    const c = Math.round((0.5 + Math.sin(th) * 0.5) * 255)
    colors[n * 3]     = RAMP[c * 3]
    colors[n * 3 + 1] = RAMP[c * 3 + 1]
    colors[n * 3 + 2] = RAMP[c * 3 + 2]
    n++
  }
  geom.setDrawRange(0, n)
  geom.attributes.position.needsUpdate = true
  geom.attributes.color.needsUpdate = true
}

/* Whether the sensor's own view belongs on screen.

   Prepare needs it to aim the box and Record needs it to see what is being
   swept. Fuse, Reconstruct, Refine and Export are all looking at something
   already captured, and the sensor carrying on behind it is just the room
   drifting about over the result. It also invited the reading that the faint
   cloud was part of the scan. */
const liveVisible = () => props.mode === 'empty' || props.mode === 'scanning'

/* ---- live depth -> point cloud ---------------------------------------- */
function onDepth (f) {
  if (!points.visible) return          // hidden behind a mesh; skip the work
  const d = f.data
  const w = f.w, h = f.h
  const near = 600, span = 3200
  let n = 0

  const sc = w / 640                   // whatever resolution actually arrived
  const FX = FX_FULL * sc
  const CX = CX_FULL * sc
  const CY = CY_FULL * sc

  // Everything outside the scan volume is drawn dim: it is what the sensor can
  // see, but it is not what gets scanned. Without this the live cloud spilling
  // past the box reads as the box being in the wrong place.
  const v = sensor.vol
  let bx0 = -Infinity, bx1 = Infinity, by0 = -Infinity, by1 = Infinity, bz0 = -Infinity, bz1 = Infinity
  if (v) {
    const [ox, oy, oz] = v.origin, s3 = v.size
    bx0 = ox; bx1 = ox + s3
    by0 = -(oy + s3); by1 = -oy
    bz0 = -(oz + s3); bz1 = -oz
  }

  for (let v = 0; v < h; v++) {
    const rowY = (v - CY)
    for (let u = 0; u < w; u++) {
      const mm = d[v * w + u]
      if (mm === 0) continue
      const z = mm * 0.001
      positions[n * 3]     = (u - CX) * z / FX
      positions[n * 3 + 1] = -rowY * z / FX
      positions[n * 3 + 2] = -z

      const px = positions[n * 3], py = positions[n * 3 + 1], pz = positions[n * 3 + 2]
      const inside = px >= bx0 && px <= bx1 && py >= by0 && py <= by1 && pz >= bz0 && pz <= bz1

      let t = (mm - near) / span
      t = t < 0 ? 0 : t > 1 ? 1 : t
      const c = (t * 255) | 0
      const k = inside ? 1.0 : 0.16
      colors[n * 3]     = RAMP[c * 3] * k
      colors[n * 3 + 1] = RAMP[c * 3 + 1] * k
      colors[n * 3 + 2] = RAMP[c * 3 + 2] * k
      n++
    }
  }

  geom.setDrawRange(0, n)
  geom.attributes.position.needsUpdate = true
  geom.attributes.color.needsUpdate = true
}

/* ---- accumulated model ------------------------------------------------ */
function onModelCloud (m) {
  const n = Math.min(m.count, MAX_MODEL)
  hasModel = n > 0
  if (n) {
    mPositions.set(m.xyz.subarray(0, n * 3))

    // Surface mode shades each point by its own normal, so the splats read as
    // a lit surface rather than a cloud. Cheaper than meshing every second and
    // it updates at the same rate the model does.
    if (surfaceMode && m.nrm) {
      const L = [-0.35, 0.78, 0.52]
      for (let i = 0; i < n; i++) {
        const k = i * 3
        let d = m.nrm[k] * L[0] + m.nrm[k + 1] * L[1] + m.nrm[k + 2] * L[2]
        d = Math.abs(d)                       // light both faces; scans are open
        const v = 0.18 + 0.82 * d * d         // squared falls off like a matte surface
        mColors[k]     = v * 0.86
        mColors[k + 1] = v * 0.98
        mColors[k + 2] = v * 0.93
      }
    } else {
      let lo = Infinity, hi = -Infinity
      for (let i = 1; i < n * 3; i += 3) {
        const y = mPositions[i]
        if (y < lo) lo = y
        if (y > hi) hi = y
      }
      const span = Math.max(1e-3, hi - lo)
      for (let i = 0; i < n; i++) {
        let t = 1 - (mPositions[i * 3 + 1] - lo) / span
        const c = ((t < 0 ? 0 : t > 1 ? 1 : t) * 255) | 0
        mColors[i * 3]     = RAMP[c * 3]
        mColors[i * 3 + 1] = RAMP[c * 3 + 1]
        mColors[i * 3 + 2] = RAMP[c * 3 + 2]
      }
    }
  }
  mGeom.setDrawRange(0, n)
  mGeom.attributes.position.needsUpdate = true
  mGeom.attributes.color.needsUpdate = true
  mPoints.visible = hasModel

  // Sizing splats to the voxel makes them tile into a continuous surface
  // instead of looking like scattered dots, but only if the points really are
  // one voxel apart. The preview is decimated on the way over (260k surface
  // voxels arrive as 65k), and a surface is two dimensional, so dropping three
  // points in four leaves the survivors twice as far apart. Splats cut for the
  // voxel pitch then cover a quarter of what they need to and the model reads
  // as loose dots however dense the scan is.
  const mm = sensor.track?.voxel_mm || 6
  const total = sensor.track?.points || n
  const spread = Math.sqrt(Math.max(1, total / Math.max(1, n)))
  mPoints.material.size = (mm / 1000) * (surfaceMode ? 1.7 : 0.9) * spread

  if (!meshObj) {
    points.visible = liveVisible()
    points.material.opacity = hasModel ? 0.14 : 0.95
  }
}

/* ---- reconstructed mesh ------------------------------------------------ */
function onMeshData (m) {
  if (meshObj) {
    scene.remove(meshObj)
    meshObj.geometry.dispose()
    meshObj.material.dispose()
    meshObj = null
  }

  if (!m) {
    mPoints.visible = hasModel
    points.visible = liveVisible()
    points.material.opacity = hasModel ? 0.22 : 0.95
    return
  }

  const g = new THREE.BufferGeometry()
  g.setAttribute('position', new THREE.BufferAttribute(m.positions, 3))
  g.setAttribute('normal', new THREE.BufferAttribute(m.normals, 3))
  if (m.colors) g.setAttribute('color', new THREE.BufferAttribute(m.colors, 3))
  g.setIndex(new THREE.BufferAttribute(m.indices, 1))

  g.computeBoundingSphere()

  meshObj = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
    color: m.colors ? 0xffffff : 0xd7e2de,
    vertexColors: !!m.colors,
    roughness: 0.75, metalness: 0.0,
    side: THREE.DoubleSide, flatShading: false
  }))
  scene.add(meshObj)

  // The mesh is the subject now. Hide the clouds outright rather than fading
  // them: a transparent material still writes depth, so a nearly invisible
  // point cloud punches holes straight through whatever is behind it.
  mPoints.visible = false
  points.visible = false
}

/* ---- scene ------------------------------------------------------------ */
function build () {
  const el = host.value
  scene = new THREE.Scene()

  camera = new THREE.PerspectiveCamera(50, el.clientWidth / el.clientHeight, 0.05, 60)
  camera.position.set(1.4, 1.0, 0.9)

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setSize(el.clientWidth, el.clientHeight)
  el.appendChild(renderer.domElement)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.target.set(0, 0, SCENE_Z)
  controls.enableDamping = true
  controls.dampingFactor = 0.08
  controls.update()

  scene.add(new THREE.AmbientLight(0xffffff, 1.2))
  scene.add(new THREE.HemisphereLight(0xe8f2ef, 0x101817, 2.4))
  const key = new THREE.DirectionalLight(0xffffff, 3.0)
  key.position.set(2, 3, 1)
  scene.add(key)
  const fill = new THREE.DirectionalLight(0xbfe9dc, 1.4)
  fill.position.set(-2, 1, 2)
  scene.add(fill)
  const rim = new THREE.DirectionalLight(0x00ffae, 1.2)
  rim.position.set(-1.5, 0.5, -3)
  scene.add(rim)

  const grid = new THREE.GridHelper(8, 24, 0x2a3a35, 0x18211e)
  grid.position.set(0, -1.0, SCENE_Z)
  scene.add(grid)

  geom = new THREE.BufferGeometry()
  positions = new Float32Array(MAX_POINTS * 3)
  colors = new Float32Array(MAX_POINTS * 3)
  geom.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geom.setAttribute('color', new THREE.BufferAttribute(colors, 3))
  geom.setDrawRange(0, 0)

  points = new THREE.Points(geom, new THREE.PointsMaterial({
    size: 0.006, sizeAttenuation: true, vertexColors: true,
    transparent: true, opacity: 0.95, depthWrite: false
  }))
  scene.add(points)

  mGeom = new THREE.BufferGeometry()
  mPositions = new Float32Array(MAX_MODEL * 3)
  mColors = new Float32Array(MAX_MODEL * 3)
  mGeom.setAttribute('position', new THREE.BufferAttribute(mPositions, 3))
  mGeom.setAttribute('color', new THREE.BufferAttribute(mColors, 3))
  mGeom.setDrawRange(0, 0)
  mPoints = new THREE.Points(mGeom, new THREE.PointsMaterial({
    size: 0.007, sizeAttenuation: true, vertexColors: true
  }))
  mPoints.visible = false
  scene.add(mPoints)

  // The sensor sits at the world origin looking down -Z (GL). Without drawing
  // it there is no way to tell where the volume sits relative to what the
  // Kinect can actually see, which makes aiming guesswork.
  rig = new THREE.Group()
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(0.28, 0.06, 0.05),
    new THREE.MeshStandardMaterial({ color: 0x2a3330, roughness: 0.9 }))
  rig.add(body)

  const eye = new THREE.Mesh(
    new THREE.SphereGeometry(0.016, 12, 8),
    new THREE.MeshBasicMaterial({ color: 0x00ffae }))
  eye.position.set(0, 0, -0.026)
  rig.add(eye)
  scene.add(rig)

  box = new THREE.Box3Helper(new THREE.Box3(), new THREE.Color(0x00ffae))
  box.material.transparent = true
  box.material.opacity = 0.45
  scene.add(box)
  updateBox()
}

function updateFrustum (far) {
  // Kinect v1 depth FOV: 58.5 x 45.6 degrees, from fx = 571.26 at 640x480.
  const hx = Math.tan(58.5 * Math.PI / 360) * far
  const hy = Math.tan(45.6 * Math.PI / 360) * far
  const z = -far
  const pts = [
    0,0,0,  hx, hy,z,   0,0,0, -hx, hy,z,
    0,0,0,  hx,-hy,z,   0,0,0, -hx,-hy,z,
    hx,hy,z,  -hx,hy,z,   -hx,hy,z, -hx,-hy,z,
    -hx,-hy,z, hx,-hy,z,   hx,-hy,z, hx,hy,z
  ]
  if (rig.userData.frustum) {
    rig.remove(rig.userData.frustum)
    rig.userData.frustum.geometry.dispose()
  }
  const g = new THREE.BufferGeometry()
  g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3))
  const f = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: 0x00ffae, transparent: true, opacity: 0.22 }))
  rig.userData.frustum = f
  rig.add(f)
}

function updateBox () {
  const v = sensor.vol
  if (!v) { box.visible = false; return }
  updateFrustum(v.dmax || 2.0)
  // Volume geometry arrives in the CV convention; negating Y and Z puts it in
  // the same GL space as everything else, which swaps min and max on those axes.
  const [ox, oy, oz] = v.origin
  const s3 = v.size
  box.box.set(
    new THREE.Vector3(ox, -(oy + s3), -(oz + s3)),
    new THREE.Vector3(ox + s3, -oy, -oz)
  )
  box.visible = props.showBox
}

let lastViewSent = 0

function publishView () {
  // The service raycasts the model from this, so the image trails the camera
  // by whatever this rate is. At 4 Hz orbiting looked detached from the
  // overlays drawn on top of it, so it goes out ten times a second; the render
  // itself costs about three milliseconds.
  const now = performance.now()
  if (now - lastViewSent < 250) return
  lastViewSent = now

  // Three is Y-up / -Z-forward; the volume is CV (Y-down, +Z-forward). Flipping
  // Y and Z of the basis and the position converts between them.
  const m = camera.matrixWorld.elements     // column-major
  const c2w = [
     m[0], -m[4], -m[8],   m[12],
    -m[1],  m[5],  m[9],  -m[13],
    -m[2],  m[6],  m[10], -m[14],
     0,     0,     0,      1
  ]
  // Size and field of view travel with it, so the raycast matches this camera
  // exactly and the returned image lines up with everything drawn over it.
  const el = renderer.domElement
  setView(c2w, el.clientWidth, el.clientHeight,
          camera.fov * Math.PI / 180)
}

function tick () {
  controls.update()
  camera.updateMatrixWorld()
  renderer.render(scene, camera)
  raf = requestAnimationFrame(tick)
}

function resize () {
  const el = host.value
  if (!el || !renderer) return
  camera.aspect = el.clientWidth / el.clientHeight
  camera.updateProjectionMatrix()
  renderer.setSize(el.clientWidth, el.clientHeight)
}

/* Swap between live sensor data and the mock subject. */
function source () {
  const wantLive = sensor.online
  if (wantLive === live && (live || geom.drawRange.count)) return
  live = wantLive
  if (unsub) { unsub(); unsub = null }
  if (live) {
    unsub = onFrame(onDepth)
  } else {
    mockCloud()
  }
}

onMounted(() => {
  build()
  source()
  unsubModel = onModel(onModelCloud)
  unsubMesh = onMesh(onMeshData)
  ro = new ResizeObserver(resize)
  ro.observe(host.value)
  raf = requestAnimationFrame(tick)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  ro && ro.disconnect()
  if (unsub) unsub()
  if (unsubModel) unsubModel()
  if (unsubMesh) unsubMesh()
  controls && controls.dispose()
  mGeom && mGeom.dispose()
  mPoints && mPoints.material.dispose()
  geom && geom.dispose()
  points && points.material.dispose()
  renderer && renderer.dispose()
})

watch(() => [sensor.vol, props.showBox], updateBox, { deep: true })
watch(() => [sensor.online, props.mode], () => {
  source()
  // Applied here as well as on arrival: moving between stages does not push a
  // new model or mesh, so without this the live cloud lingers until something
  // else happens to redraw it.
  if (points && !meshObj) points.visible = liveVisible()
})

// Kept so the shading path is still callable from the console while it is
// being worked on, but nothing in the interface reaches it.
function setSurface (on) {
  surfaceMode = on
  const m = useSensor().latestModel
  if (m) onModelCloud(m)
}

defineExpose({ setSurface, resetView: () => { camera.position.set(1.4, 1.0, 0.9); controls.target.set(0, 0, SCENE_Z); controls.update() } })
</script>

<template>
  <div ref="host" class="vp" />
</template>

<style scoped>
.vp { position: absolute; inset: 0; }
.vp :deep(canvas) { display: block; cursor: grab; }
.vp :deep(canvas:active) { cursor: grabbing; }
</style>
