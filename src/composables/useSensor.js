import { reactive, readonly } from 'vue'

// Capture service endpoint. Override with ?service=host:port when the sensor
// lives on another machine, or VITE_GHOSTLIGHT_SERVICE at build time.
//
// Built for production, the service is also what served this page, so the
// socket is same-origin and the address is simply wherever we came from. That
// keeps a non-default port, or serving to another machine, working with no
// configuration. In dev the page comes from Vite on its own port, so fall back
// to the service's default.
const URL = (() => {
  const q = new URLSearchParams(location.search).get('service')
  const host = q
    || import.meta.env.VITE_GHOSTLIGHT_SERVICE
    || (import.meta.env.DEV ? '127.0.0.1:8787' : location.host)
  if (host.startsWith('ws')) return host
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${host}`
})()

const MAGIC_DEPTH = 0x474c4454
const MAGIC_MODEL = 0x474c4d44
const MAGIC_MESH  = 0x474c4d53
const MAGIC_RGB   = 0x474c5247
const MAGIC_VIEW  = 0x474c5256
const RETRY_MS = 2000

const state = reactive({
  connected: false,       // is the capture service reachable
  online: false,          // is a sensor actually open
  recording: false,
  name: null,
  backend: null,
  vid: null,
  pid: null,
  width: 0,
  height: 0,
  fps: 0,
  colour: false,
  tilt: null,
  gpu: null,              // real device, reported by the service
  error: null,
  diagnosis: null,
  frameIndex: 0,
  fpsMeasured: 0,
  busy: null,             // 'reconstruct' | 'refine' while a long job runs
  lastError: null,
  lastExport: null,
  // filled by the tracker while recording
  track: {
    frames: 0, registered: 0, points: 0, tracking: 'good', fitness: 0, rmse: 0,
    mode: 'geometry',     // must default, or mode-gated panels flash on load
    reason: null,         // why a frame failed, when it did
    cond: 0,              // ICP conditioning: low means the pose is unconstrained
    inbox: 0,             // depth pixels landing inside the scan volume
    locked: false,        // is a solved marker framework installed
    anchored: true,       // has that framework been recognised in the live view
    ambiguity: 0          // how self-similar the marker layout is
  },
  mesh: null,             // { vertices, faces, watertight } once reconstructed
  ops: { crop: false, turntable: false, islands: true, smooth: false, simplify: false },
  volume: { size: 2.0, voxel: 0.006, centre_z: 1.6, dmin: 0.8, dmax: 2.8 },
  vol: null,  // actual volume geometry from the service
  mapColour: false,
  mode: 'geometry',
  markers: [],
  bundle: null,           // the recording on disk: live while recording, then its summary
  bundles: [],            // everything on disk, after listBundles()
  fuse: null,             // { done, total, name } while a re-fusion runs
  framework: null,        // last framework solve report
  turntable: null         // fitted platter plane, and its axis once known
})

// Latest depth frame and latest model cloud. Neither is reactive: they churn
// and nothing should re-render on them. Consumers subscribe instead.
let frame = null
let model = null
let mesh = null
let rgb = null
let view = null
const frameListeners = new Set()
const viewListeners = new Set()
const modelListeners = new Set()
const meshListeners = new Set()
const rgbListeners = new Set()

let ws = null
let retry = null
let tickCount = 0
let tickSince = 0

function apply (msg) {
  const s = msg.sensor || {}
  state.online = !!s.online
  state.error = s.error ?? null
  state.diagnosis = s.diagnosis ?? null
  state.recording = !!msg.recording
  if (s.online) {
    state.backend = s.backend || null
    state.name = s.name
    state.vid = s.vid
    state.pid = s.pid
    state.width = s.width
    state.height = s.height
    state.fps = s.fps
    state.colour = !!s.colour
    state.tilt = s.tilt ?? null
  }
  state.busy = msg.busy ?? null
  if (msg.gpu) state.gpu = msg.gpu
  if (msg.track) Object.assign(state.track, msg.track)
  if (msg.ops) Object.assign(state.ops, msg.ops)
  if (msg.volume) state.vol = msg.volume
  if ('map_colour' in msg) state.mapColour = !!msg.map_colour
  state.markers = msg.markers || []
  // The live survey count rides along with the tracker status, while solve and
  // apply replies arrive as their own message. Both describe the same thing, so
  // they merge into one place rather than the UI having to know which is which.
  if (msg.track && msg.track.framework) {
    state.framework = { ...(state.framework || {}), ...msg.track.framework }
  }
  state.bundle = msg.bundle ?? state.bundle
  state.fuse = msg.fuse ?? null
  if ('turntable' in msg) state.turntable = msg.turntable
  if (msg.track && msg.track.mode) state.mode = msg.track.mode
  if (msg.track) {
    if (msg.track.voxel_mm) state.volume.voxel = msg.track.voxel_mm / 1000
    if (msg.track.volume_m) state.volume.size = msg.track.volume_m
  }
  state.mesh = msg.mesh ?? state.mesh
}

function onBinary (buf) {
  const dv = new DataView(buf)
  const magic = dv.getUint32(0, true)

  if (magic === MAGIC_DEPTH) {
    const w = dv.getUint16(4, true)
    const h = dv.getUint16(6, true)
    const idx = dv.getUint32(8, true)
    frame = { w, h, idx, data: new Uint16Array(buf, 12, w * h) }
    state.frameIndex = idx

    tickCount++
    const now = performance.now()
    if (now - tickSince > 1000) {
      state.fpsMeasured = Math.round((tickCount * 1000) / (now - tickSince))
      tickCount = 0
      tickSince = now
    }
    for (const fn of frameListeners) fn(frame)
    return
  }

  if (magic === MAGIC_MODEL) {
    const count = dv.getUint32(4, true)
    const xyz = new Float32Array(buf, 8, count * 3)
    const nrm = new Float32Array(buf, 8 + count * 12, count * 3)
    model = { count, xyz, nrm }
    for (const fn of modelListeners) fn(model)
    return
  }

  if (magic === MAGIC_MESH) {
    const nv = dv.getUint32(4, true)
    const nf = dv.getUint32(8, true)
    if (nv === 0) {
      mesh = null
    } else {
      let o = 12
      const positions = new Float32Array(buf, o, nv * 3); o += nv * 12
      const normals   = new Float32Array(buf, o, nv * 3); o += nv * 12
      const colors    = new Float32Array(buf, o, nv * 3); o += nv * 12
      const indices   = new Uint32Array(buf, o, nf * 3)
      mesh = { nv, nf, positions, normals, colors, indices }
    }
    for (const fn of meshListeners) fn(mesh)
    return
  }

  if (magic === MAGIC_VIEW) {
    const w = dv.getUint16(4, true)
    const h = dv.getUint16(6, true)
    view = { w, h, rgb: new Uint8Array(buf, 8, w * h * 4) }
    for (const fn of viewListeners) fn(view)
    return
  }

  if (magic === MAGIC_RGB) {
    const w = dv.getUint16(4, true)
    const h = dv.getUint16(6, true)
    rgb = { w, h, bgra: new Uint8Array(buf, 8, w * h * 4) }
    for (const fn of rgbListeners) fn(rgb)
  }
}

function connect () {
  if (ws) return
  try {
    ws = new WebSocket(URL)
  } catch {
    schedule()
    return
  }
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    state.connected = true
    tickSince = performance.now()
  }

  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      const msg = JSON.parse(e.data)
      if (msg.type === 'status') apply(msg)
      else if (msg.type === 'error') state.lastError = msg.message
      else if (msg.type === 'exported') state.lastExport = msg
      else if (msg.type === 'framework') state.framework = msg
      else if (msg.type === 'bundles') state.bundles = msg.items || []
    } else {
      onBinary(e.data)
    }
  }

  ws.onclose = () => {
    ws = null
    state.connected = false
    state.online = false
    state.recording = false
    state.fpsMeasured = 0
    frame = null
    schedule()
  }

  ws.onerror = () => { try { ws && ws.close() } catch {} }
}

function schedule () {
  if (retry) return
  retry = setTimeout(() => { retry = null; connect() }, RETRY_MS)
}

function send (obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
}

connect()

// Vite re-evaluates this module on every hot update. Without this, each update
// opens another socket and the old one keeps reconnecting forever, so the
// service ends up serving a dozen ghost clients and starves the tracker.
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    clearTimeout(retry)
    retry = null
    if (ws) { ws.onclose = null; try { ws.close() } catch {} ws = null }
  })
}

export function useSensor () {
  return {
    sensor: readonly(state),

    /** Subscribe to live depth frames. Returns an unsubscribe function. */
    onFrame (fn) {
      frameListeners.add(fn)
      if (frame) fn(frame)
      return () => frameListeners.delete(fn)
    },

    /** Subscribe to the accumulated model cloud. */
    onModel (fn) {
      modelListeners.add(fn)
      if (model) fn(model)
      return () => modelListeners.delete(fn)
    },

    /** Subscribe to the shaded raycast of the model. */
    onRender (fn) {
      viewListeners.add(fn)
      if (view) fn(view)
      return () => viewListeners.delete(fn)
    },

    /** Subscribe to the colour preview. */
    onColour (fn) {
      rgbListeners.add(fn)
      if (rgb) fn(rgb)
      return () => rgbListeners.delete(fn)
    },

    /** Subscribe to the reconstructed mesh. Called with null when cleared. */
    onMesh (fn) {
      meshListeners.add(fn)
      if (mesh) fn(mesh)
      return () => meshListeners.delete(fn)
    },

    record (on) { send({ type: 'record', on }) },
    /** Rebuild the volume from a recording at the current voxel size. Omit
        `path` to re-fuse the most recent take. */
    fuseBundle (path, voxel) { state.lastError = null; send({ type: 'fuse', path, voxel }) },
    listBundles () { send({ type: 'bundles' }) },
    deleteBundle (path) { send({ type: 'bundle_delete', path }) },
    /** Fit the turntable plane. `place` also moves the scan volume onto it. */
    findTurntable (place = false) { send({ type: 'turntable', place }) },
    /** action: 'solve' | 'apply' | 'clear' */
    framework (action) { send({ type: 'framework', action }) },
    resetScan () { send({ type: 'reset' }) },
    rescan () { send({ type: 'rescan' }) },
    reconstruct () { state.lastError = null; send({ type: 'reconstruct' }) },
    tilt (degrees) { send({ type: 'tilt', degrees }) },
    /** Tell the service where we are looking, so the model is raycast from
        here rather than from the sensor. The function this comment described
        was never actually written, so the service had no viewer camera and the
        preview fell back to splatting points. */
    setView (c2w, w, h, fov) { send({ type: 'view', c2w, w, h, fov }) },
    setMapColour (on) { send({ type: 'map_colour', on }) },
    setMode (mode) { send({ type: 'mode', mode }) },
    setVolume (v) { Object.assign(state.volume, v); send({ type: 'volume', ...state.volume }) },
    refine (ops, bbox) { send({ type: 'refine', ops, bbox }) },
    exportMesh (format) { state.lastExport = null; send({ type: 'export', format }) },

    get latest () { return frame },
    get latestModel () { return model },
    get latestMesh () { return mesh }
  }
}
