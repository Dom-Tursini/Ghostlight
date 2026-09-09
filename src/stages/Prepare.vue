<script setup>
import { computed, ref, watch } from 'vue'
import Panel from '../components/Panel.vue'
import Field from '../components/Field.vue'
import SetupHelp from '../components/SetupHelp.vue'
import ColourView from '../components/ColourView.vue'
import { useSensor } from '../composables/useSensor'

defineProps({ s: Object })
const emit = defineEmits(['start'])

const { sensor, setVolume, tilt, setMode, record, framework, findTurntable,
        rescan } = useSensor()

// Scene presets. A Kinect v1 cannot focus closer than 800 mm, so even the
// tightest preset starts beyond that; there is nothing nearer to offer.
// Only what falls inside the volume is tracked or integrated, so a tight
// volume is what keeps the room out of an object scan. Far clip sits just
// past the volume for the same reason.
const SCENES = {
  Object: { size: 0.5, voxel: 0.002, centre_z: 1.05, dmin: 0.75, dmax: 1.35 },
  Bust:   { size: 0.9, voxel: 0.003, centre_z: 1.25, dmin: 0.75, dmax: 1.8 },
  Body:   { size: 2.0, voxel: 0.006, centre_z: 1.6,  dmin: 0.8,  dmax: 2.8 },
  Room:   { size: 4.0, voxel: 0.012, centre_z: 2.4,  dmin: 0.8,  dmax: 4.0 }
}

const size = ref(0.5)
const voxelMm = ref(2)
const far = ref(1.35)
const near = ref(0.75)

// Which preset the box currently is, or null for none of them.
//
// This used to be a remembered name, set when a preset was clicked and never
// touched again. The sliders are adopted from the service on load and can be
// moved by hand afterwards, so the name went on claiming a preset that no
// longer described the box: a panel reading Body over a volume of some other
// size entirely. Deriving it means the highlight cannot disagree with the
// numbers under it.
const scene = computed(() => {
  for (const [n, p] of Object.entries(SCENES)) {
    // Size, voxel and centre only. The clip planes are recomputed from the
    // box by the volume itself, so they never come back as the figure a
    // preset asked for and would fail every comparison. The four presets have
    // distinct sizes, so this still tells them apart.
    if (Math.abs(size.value - p.size) < 0.005 &&
        Math.abs(voxelMm.value - p.voxel * 1000) < 0.05 &&
        Math.abs(cz.value - p.centre_z) < 0.005 &&
        Math.abs(cx.value) < 0.005 && Math.abs(cy.value) < 0.005) return n
  }
  return null
})
// Box position. A turntable subject sits below the optical axis with the table
// directly under it; if the table is inside the volume it is static while the
// subject turns, ICP locks to it, and the subject smears into a solid of
// revolution. Lifting the box clear of the table is the fix.
const cx = ref(0), cy = ref(0), cz = ref(1.05)

function preset (name) {
  const p = SCENES[name]
  size.value = p.size
  voxelMm.value = p.voxel * 1000
  far.value = p.dmax
  near.value = p.dmin
  cx.value = 0; cy.value = 0; cz.value = p.centre_z
  // No push here. Changing the refs above already wakes the watcher below,
  // which pushes once the values stop moving. Calling it as well rebuilt the
  // volume twice per click, and a rebuild releases and reallocates on the
  // card, so the second one doubled how long the drawn box lagged the panel.
}

function push () {
  // A volume the card cannot hold is not sent. The warning is already on
  // screen, so asking the service to refuse it as well only adds an error and
  // discards whatever has been scanned. Leave the slider where it was put and
  // let the reading speak for itself.
  if (tooBig()) return
  setVolume({
    size: size.value,
    voxel: voxelMm.value / 1000,
    centre_x: cx.value,
    centre_y: cy.value,
    centre_z: cz.value,
    dmin: near.value,
    dmax: far.value
  })
}

// Voxels along one edge, and what that actually costs. The figure counts colour
// as well as distance and weight: colour is three more float32 per voxel and
// therefore more than half the real total, and leaving it out understated the
// cost by 2.5x.
const grid = () => Math.round(size.value / (voxelMm.value / 1000))
// Eight bytes of distance and weight, plus twelve of colour only when colour
// is being stored. Keying this off whether the sensor merely has a colour
// stream charged for a channel that was never going to be used.
const bytesPerVoxel = () => (sensor.gpu?.bytes_per_voxel || (sensor.mapColour ? 20 : 8))
const needGb = () => Math.pow(grid(), 3) * bytesPerVoxel() / 1e9
const vram = () => needGb().toFixed(2)

// The budget comes from the service, which can see the card. Guessing a ceiling
// here would be wrong on every machine except the one it was written on. Until
// the service has reported, nothing is claimed either way.
const budgetGb = computed(() => sensor.gpu?.budget_gb || 0)
const tooBig = () => budgetGb.value > 0 && needGb() > budgetGb.value

// Debounced: dragging a slider would otherwise rebuild the volume every tick.
let t = null
watch([size, voxelMm, far, cx, cy, cz], () => { clearTimeout(t); t = setTimeout(push, 250) })

// The framework pass is setup, not scanning: it measures where the markers are
// and integrates nothing. Keeping it here rather than on the Record stage says
// so, and means finishing it does not look like finishing a scan.
const surveying = computed(() => sensor.recording && sensor.track.mode === 'framework')
const surveyed = computed(() => sensor.framework?.frames ?? 0)

// Markers are stickers: they sit still and the count barely moves. Reflections,
// bright edges and specular highlights come and go every frame. So a count that
// will not settle is itself the diagnosis, and worth saying out loud rather
// than leaving someone to watch a number flicker and wonder what it means.
const recent = ref([])
watch(() => sensor.track.markers_seen, (n) => {
  if (!surveying.value) { recent.value = []; return }
  recent.value.push(n ?? 0)
  if (recent.value.length > 24) recent.value.shift()
})
const unstable = computed(() => {
  const r = recent.value
  if (r.length < 12) return false
  const avg = r.reduce((a, b) => a + b, 0) / r.length
  return avg > 0 && (Math.max(...r) - Math.min(...r)) > avg * 0.6
})

function survey () {
  if (surveying.value) { record(false); return }
  setMode('framework')
  record(true)
}

function solve () {
  if (sensor.recording) record(false)
  framework('solve')
}

// The service echoes its volume back on every status frame, and an earlier
// version copied that echo into the sliders. That fights whoever is using
// them: you drag, the value goes out, it comes back, and the control jumps to
// whatever the service settled on. Worse, a volume the card cannot fit is
// refused, so the slider sprang back and there was no way to sit on an invalid
// setting long enough to read the warning about it.
//
// The sliders are the input. They are adopted from the service once, so the
// panel opens describing what is actually allocated, and after that they are
// left alone. Warn, do not force.
let adopted = false
watch(() => sensor.vol, (v) => {
  if (!v || !v.centre) return

  if (!adopted) {
    adopted = true
    size.value = v.size
    if (v.voxel) voxelMm.value = Math.round(v.voxel * 10000) / 10
    if (v.dmax) far.value = v.dmax
    if (v.dmin) near.value = v.dmin
    cx.value = Math.round(v.centre[0] * 100) / 100
    cy.value = Math.round(v.centre[1] * 100) / 100
    cz.value = Math.round(v.centre[2] * 100) / 100
    return
  }

  // The one thing the service moves on its own is the box centre, when Lift
  // box clear puts it above the platter. A move of a centimetre or more cannot
  // be our own echo, which matches what we just sent.
  const moved = Math.abs(v.centre[0] - cx.value) > 0.01 ||
                Math.abs(v.centre[1] - cy.value) > 0.01 ||
                Math.abs(v.centre[2] - cz.value) > 0.01
  if (!moved) return
  cx.value = Math.round(v.centre[0] * 100) / 100
  cy.value = Math.round(v.centre[1] * 100) / 100
  cz.value = Math.round(v.centre[2] * 100) / 100
})
</script>

<template>
  <Panel title="Sensor">
    <div class="sensor" :class="{ off: !sensor.online }">
      <div class="row">
        <span class="nm">
          {{ sensor.online ? (sensor.name || 'Depth sensor')
                           : (sensor.diagnosis?.title || 'No sensor found') }}
        </span>
        <i class="dot" :class="sensor.online ? 'ok' : 'bad'" />
      </div>

      <p v-if="sensor.online" class="mono meta">
        {{ sensor.vid }}:{{ sensor.pid }} · {{ sensor.backend || 'unknown backend' }}<br>
        depth {{ sensor.width }}×{{ sensor.height }} @{{ sensor.fps }}{{ sensor.colour ? ' + colour' : '' }}
      </p>

      <div v-if="sensor.online" class="tilt">
        <span class="label">Tilt</span>
        <button class="tbtn" @click="tilt(-5)">−5°</button>
        <span class="mono deg">{{ sensor.tilt ?? '-' }}°</span>
        <button class="tbtn" @click="tilt(5)">+5°</button>
      </div>
      <p v-if="sensor.online" class="hint">
        The gearbox is fragile. One nudge a second at most.
      </p>

      <template v-else>
        <p class="meta">
          {{ sensor.diagnosis?.detail
             || (sensor.connected ? 'Waiting for a sensor.' : 'Capture service is not running.') }}
        </p>
        <p v-if="sensor.diagnosis?.action" class="meta act">{{ sensor.diagnosis.action }}</p>
        <p v-if="sensor.connected && sensor.diagnosis?.code === 'ok'" class="meta act">
          Windows sees the camera but the SDK has not picked it up. Recheck
          reloads it, which is what a hot-plugged sensor needs.
        </p>
        <button v-if="sensor.connected" class="tbtn" @click="rescan()">Recheck</button>
        <code v-if="!sensor.connected" class="cmd mono">python ghostlight.py</code>
      </template>
    </div>
  </Panel>

  <SetupHelp v-if="!sensor.online" />

  <Panel v-if="sensor.track.mode !== 'geometry'" title="Marker framework">
    <p class="blurb">
      Markers are surveyed and solved before the scan, then held fixed.
    </p>

    <div class="acts">
      <button class="btn" :disabled="!sensor.online" @click="survey">
        {{ surveying ? 'Stop survey' : 'Survey markers' }}
      </button>
      <button class="btn" :disabled="surveyed < 20 || !!sensor.busy" @click="solve">Solve</button>
    </div>

    <template v-if="surveying">
      <ColourView />
      <dl class="sum mono">
        <div><dt>In view</dt><dd :class="{ bad: unstable }">{{ sensor.track.markers_seen }}</dd></div>
        <div><dt>Collected</dt><dd>{{ surveyed }}</dd></div>
      </dl>
      <p v-if="unstable" class="warn">
        The count will not settle, so most of these are reflections rather than
        markers. Tighten the scan volume onto the turntable.
      </p>
      <p v-else class="hint">Turn the table through a full revolution.</p>
    </template>

    <dl v-if="sensor.framework && sensor.framework.markers" class="sum mono">
      <div><dt>Markers</dt><dd>{{ sensor.framework.markers }}</dd></div>
      <div><dt>Frames</dt><dd>{{ sensor.framework.frames }}</dd></div>
      <div><dt>Fit</dt><dd>{{ sensor.framework.rmse_mm }} mm rms</dd></div>
      <div><dt>Worst marker</dt><dd>{{ sensor.framework.worst_mm }} mm</dd></div>
    </dl>

    <p v-if="sensor.framework && sensor.framework.note" class="warn">
      {{ sensor.framework.note }}
    </p>

    <div v-if="sensor.framework && sensor.framework.solved" class="acts">
      <button class="btn primary" :disabled="!sensor.framework.usable"
              @click="framework('apply')">
        {{ sensor.track.locked ? 'Reapply' : 'Use for scan' }}
      </button>
      <button class="btn danger" @click="framework('clear')">Clear</button>
    </div>

    <p v-if="sensor.track.locked" class="ok">
      {{ sensor.track.markers_mapped }} markers locked as fixed reference.
    </p>
  </Panel>

  <Panel title="Scan volume">
    <Field label="Tracking">
      <div class="seg">
        <button :class="{ on: sensor.track.mode === 'geometry' }" @click="setMode('geometry')">Geometry</button>
        <button :class="{ on: sensor.track.mode !== 'geometry' }" @click="setMode('markers')">Markers</button>
      </div>
      <span class="hint">
        Geometry needs a distinctive shape. A smooth object on a turntable
        smears, so use markers: four or more, spread out, not in a line.
      </span>
    </Field>

    <Field label="Preset">
      <div class="seg">
        <button v-for="(p, n) in SCENES" :key="n" :class="{ on: scene === n }"
                @click="preset(n)">{{ n }}</button>
      </div>
    </Field>

    <Field label="Volume" :value="`${size.toFixed(2)} m`">
      <input type="range" min="0.25" max="4" step="0.05" v-model.number="size">
      <span class="hint mono" :class="{ bad: tooBig() }">
        {{ grid() }}³ voxels · {{ vram() }} GB VRAM{{ sensor.mapColour ? ' with colour' : '' }}
      </span>
      <span v-if="tooBig()" class="warn">
        Needs {{ vram() }} GB. {{ budgetGb.toFixed(1) }} GB of the
        {{ sensor.gpu?.vram_gb }} GB on this card is available for a volume,
        the rest being other applications and headroom for meshing. Raise the
        voxel size or shrink the volume.
      </span>
    </Field>

    <Field label="Voxel size" :value="`${voxelMm.toFixed(1)} mm`">
      <input type="range" min="1" max="16" step="0.5" v-model.number="voxelMm">
      <span class="hint">Detail. Smaller is finer and costs memory.</span>
    </Field>

    <Field label="Far clip" :value="`${far.toFixed(1)} m`">
      <input type="range" min="0.9" max="4" step="0.05" v-model.number="far">
      <span class="hint">
        Keeps the room out. Depth error grows with range: 2 mm at 0.8 m,
        47 mm at 4 m.
      </span>
    </Field>

    <Field label="Box position" :value="`${cx.toFixed(2)}, ${cy.toFixed(2)}, ${cz.toFixed(2)} m`">
      <div class="axes">
        <label><span class="ax">X</span><input type="range" min="-1" max="1" step="0.01" v-model.number="cx"></label>
        <label><span class="ax">Y</span><input type="range" min="-1" max="1" step="0.01" v-model.number="cy"></label>
        <label><span class="ax">Z</span><input type="range" min="0.6" max="3" step="0.01" v-model.number="cz"></label>
      </div>
      <span class="hint">
        Y is down. Keep the turntable surface outside the box, or tracking
        locks to it instead of the subject.
      </span>
    </Field>

    <Field label="Turntable">
      <div class="acts">
        <button class="tbtn f" :disabled="!sensor.online"
                @click="findTurntable(false)">Find surface</button>
        <button class="tbtn f" :disabled="!sensor.online || !sensor.turntable"
                @click="findTurntable(true)">Lift box clear</button>
      </div>
      <span v-if="sensor.turntable" class="hint mono">
        plane found, {{ Math.round(sensor.turntable.coverage * 100) }}% of the
        search region, tilted {{ sensor.turntable.tilt_deg }} deg
        <template v-if="sensor.turntable.axis">
          , axis fitted at radius {{ (sensor.turntable.axis.radius * 100).toFixed(1) }} cm
        </template>
      </span>
      <span class="hint">
        Keeps the scan box above the platter, so tracking follows the subject
        and not the table. Lift only moves the box up, never sideways.
      </span>
    </Field>

    <p class="note">Changing any of these clears the current scan.</p>

    <button class="btn primary w" :disabled="!sensor.online" @click="emit('start')">Start scan</button>
  </Panel>
</template>

<style scoped>
.sensor { display: flex; flex-direction: column; gap: 9px; }
.nm { font-size: 13px; }
.sensor.off .nm { color: var(--bad); }
.meta { margin: 0; font-size: 11px; line-height: 1.65; color: var(--text-faint); }
.act { color: var(--text-dim); }
.hint { font-size: 10px; line-height: 1.55; color: var(--text-faint); }
.note { margin: 0; font-size: 10px; color: var(--text-faint); }
.blurb { margin: 0; font-size: 11px; line-height: 1.7; color: var(--text-faint); }
.warn { margin: 0; font-size: 10.5px; line-height: 1.6; color: var(--warn); }
.hint.bad { color: var(--bad); }
.ok { margin: 0; font-size: 10.5px; line-height: 1.6; color: var(--accent); }
.acts { display: flex; gap: 6px; }
.acts .btn, .acts .tbtn.f { flex: 1; }
.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }
.axes { display: flex; flex-direction: column; gap: 5px; }
.axes label { display: flex; align-items: center; gap: 8px; }
.ax { font-size: 9px; color: var(--text-faint); width: 8px; font-family: var(--mono); }
.tilt { display: flex; align-items: center; gap: 8px; }
.tbtn {
  height: 22px; padding: 0 8px; font-size: 11px;
  border: 1px solid var(--line-strong); border-radius: var(--r);
  color: var(--text-dim); background: var(--surface-2);
}
.tbtn:hover { color: var(--text); border-color: rgba(255,255,255,0.24); }
.deg { font-size: 11px; color: var(--text-dim); min-width: 34px; text-align: center; }
.cmd {
  display: block; font-size: 10.5px; padding: 6px 8px; border-radius: var(--r);
  border: 1px solid var(--line); background: var(--surface-1); color: var(--text-dim);
}
.w { width: 100%; }
.dot { width: 6px; height: 6px; border-radius: 50%; }
.dot.ok  { background: var(--accent); box-shadow: 0 0 8px var(--accent-glow); }
.dot.bad { background: var(--bad); }
</style>
