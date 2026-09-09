import { reactive, computed } from 'vue'

export const STAGES = [
  { id: 'prepare',     name: 'Prepare' },
  { id: 'record',      name: 'Record' },
  { id: 'fuse',        name: 'Fuse' },
  { id: 'reconstruct', name: 'Reconstruct' },
  { id: 'refine',      name: 'Refine' },
  { id: 'export',      name: 'Export' }
]

// Everything here is fake. No sensor, no CUDA, no Python.
const s = reactive({
  stage: 'prepare',
  furthest: 0,

  // prepare
  sensorOnline: true,

  // record
  recording: false,
  frames: 0,
  elapsed: 0,
  tracking: 'good',      // good | weak | lost
  coverage: 0,

  // reconstruct
  fusing: false,
  fuseProgress: 0,
  vertices: 0,
  faces: 0,

  // refine  (non-destructive stack, toggleable)
  ops: [
    { id: 'crop',      name: 'Crop to bounding box', on: true,  note: 'removes floor + walls' },
    { id: 'islands',   name: 'Remove loose parts',   on: true,  note: 'keep largest component' },
    { id: 'holes',     name: 'Fill holes',           on: false, note: 'Poisson' },
    { id: 'watertight',name: 'Make watertight',      on: false, note: 'Poisson, depth 9' },
    { id: 'simplify',  name: 'Simplify',             on: false, note: 'quadric decimation, 50%' },
    { id: 'colorize',  name: 'Colorize',             on: true,  note: 'vertex colours' }
  ],

  format: 'STL'
})

// dev: poke any state from the console while reviewing, e.g. __gl.sensorOnline = false
if (import.meta.env.DEV) window.__gl = s

let timer = null

export function useSession () {
  const stageIndex = computed(() => STAGES.findIndex(x => x.id === s.stage))

  function go (id) {
    const i = STAGES.findIndex(x => x.id === id)
    if (i <= s.furthest) s.stage = id
  }

  function unlock (i) { if (i > s.furthest) s.furthest = i }

  function start () {
    s.furthest = Math.max(s.furthest, 1)
    s.stage = 'record'
  }

  function toggleRecord () {
    s.recording = !s.recording
    if (s.recording) {
      let last = performance.now()
      timer = setInterval(() => {
        const now = performance.now()
        const dt = Math.min(1.0, (now - last) / 1000)   // real time, throttle-proof
        last = now
        s.elapsed += dt
        s.frames += Math.round(dt * 30)
        s.coverage = Math.min(1, s.coverage + dt * 0.11)
        const r = Math.random()
        s.tracking = r > 0.985 ? 'lost' : r > 0.93 ? 'weak' : 'good'
      }, 50)
    } else {
      clearInterval(timer)
      s.tracking = 'good'
      if (s.frames > 0) unlock(2)
    }
  }

  function fuse () {
    s.fusing = true
    s.fuseProgress = 0
    const t = setInterval(() => {
      s.fuseProgress += 0.02
      if (s.fuseProgress >= 1) {
        clearInterval(t)
        s.fusing = false
        s.fuseProgress = 1
        s.vertices = 284913
        s.faces = 561048
        unlock(3)
      }
    }, 40)
  }

  function reset () {
    clearInterval(timer)
    Object.assign(s, {
      stage: 'prepare', furthest: 0, recording: false, frames: 0,
      elapsed: 0, coverage: 0, tracking: 'good', fusing: false,
      fuseProgress: 0, vertices: 0, faces: 0
    })
  }

  return { s, STAGES, stageIndex, go, unlock, start, toggleRecord, fuse, reset }
}
