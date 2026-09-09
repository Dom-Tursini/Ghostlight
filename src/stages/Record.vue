<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import Panel from '../components/Panel.vue'
import DepthView from '../components/DepthView.vue'
import ColourView from '../components/ColourView.vue'
import { useSensor } from '../composables/useSensor'

const props = defineProps({ s: Object })
const emit = defineEmits(['next'])

const { sensor, record, resetScan } = useSensor()

const TXT = { good: 'Tracking', weak: 'Tracking weak', lost: 'Tracking lost' }

// Why a frame did not register, and what to do about it. Each of these comes
// from a distinct measurement in the tracker rather than a guess at the cause:
// "nothing in the volume", "the shape cannot pin down a pose" and "you moved
// too fast" all look like a low score and have nothing to do with each other,
// and telling someone their scan failed without telling them which one leaves
// them changing the wrong thing.
const REASONS = {
  few_points: {
    label: 'Nothing in the volume',
    help: 'Almost nothing is landing inside the green box. Move the sensor, or ' +
          'go back and enlarge the box.'
  },
  ambiguous_plane: {
    label: 'Shape cannot fix a pose',
    help: 'What is in view is flat or smooth all round, so the pose is not ' +
          'pinned down and the scan will smear. Include an edge, or use markers.'
  },
  too_fast: {
    label: 'Moving too fast',
    help: 'The pose jumped further than one frame allows. Those frames are ' +
          'blurred, so they are dropped. Slow down.'
  },
  no_markers: {
    label: 'Not enough markers',
    help: 'Fewer than three visible, and three is the minimum for a pose. Aim ' +
          'lower to get more of the turntable in frame, or add more.'
  },
  not_anchored: {
    label: 'Framework not recognised',
    help: 'Not found in the current view yet. Turn the table until more ' +
          'markers are visible at once.'
  },
  ambiguous_layout: {
    label: 'Marker layout too regular',
    help: 'Spaced too evenly to tell apart in a single frame. Move a few in ' +
          'or out, then solve again.'
  },
  lost: {
    label: 'Frames not merging',
    help: 'Point back at something already scanned to pick the model up again.'
  }
}

const elapsed = ref(0)
let timer = null

watch(() => sensor.recording, (on) => {
  clearInterval(timer)
  if (on) {
    const t0 = performance.now()
    timer = setInterval(() => { elapsed.value = (performance.now() - t0) / 1000 }, 200)
  }
})

onBeforeUnmount(() => clearInterval(timer))

function toggle () {
  if (!sensor.recording) elapsed.value = 0
  record(!sensor.recording)
}

function clear () {
  elapsed.value = 0
  resetScan()
}

// Only worth surfacing while it is actually going wrong. A reason attached to a
// frame that registered fine is noise.
const why = computed(() => {
  const t = sensor.track
  if (t.tracking === 'good') return null
  return REASONS[t.reason] || null
})

function clock (sec) {
  const m = Math.floor(sec / 60), r = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`
}
</script>

<template>
  <Panel title="Capture">
    <div class="rec">
      <button class="rbtn" :class="{ live: sensor.recording }"
              :disabled="!sensor.online" @click="toggle">
        <span class="glyph" :class="{ stop: sensor.recording }" />
      </button>
      <div class="ct">
        <span class="mono t">{{ clock(elapsed) }}</span>
        <span class="mono f">
          {{ sensor.track.registered.toLocaleString() }} registered
          <template v-if="sensor.track.frames > sensor.track.registered">
            / {{ sensor.track.frames.toLocaleString() }}
          </template>
        </span>
      </div>
    </div>

    <div class="trk" :class="sensor.recording ? sensor.track.tracking : 'idle'">
      <i class="dot" />
      <span>{{ sensor.recording ? (why ? why.label : TXT[sensor.track.tracking]) : 'Idle' }}</span>
      <span v-if="sensor.recording" class="fit mono">fit {{ sensor.track.fitness.toFixed(2) }}</span>
    </div>

    <p v-if="sensor.recording && why" class="warn">{{ why.help }}</p>

    <dl class="sum mono">
      <template v-if="sensor.track.mode === 'markers'">
        <div><dt>Markers seen</dt><dd>{{ sensor.track.markers_seen ?? 0 }}</dd></div>
        <div><dt>Markers mapped</dt><dd>{{ sensor.track.markers_mapped ?? 0 }}</dd></div>
      </template>
      <div v-if="sensor.track.locked">
        <dt>Framework</dt>
        <dd :class="{ bad: !sensor.track.anchored }">
          {{ sensor.track.anchored ? 'anchored' : 'searching' }}
        </dd>
      </div>
      <div><dt>Model points</dt><dd>{{ sensor.track.points.toLocaleString() }}</dd></div>
      <div><dt>ICP RMSE</dt><dd>{{ (sensor.track.rmse * 1000).toFixed(1) }} mm</dd></div>
      <div v-if="sensor.track.mode === 'geometry' && sensor.recording">
        <dt>Conditioning</dt>
        <dd :class="{ bad: sensor.track.reason === 'ambiguous_plane' }">
          {{ sensor.track.cond.toFixed(4) }}
        </dd>
      </div>
    </dl>

    <div class="acts">
      <button class="btn danger" :disabled="!sensor.track.points" @click="clear">Clear</button>
      <button class="btn" :disabled="!sensor.track.points || sensor.recording"
              @click="emit('next')">Reconstruct</button>
    </div>
  </Panel>

  <Panel title="Streams">
    <div class="thumbs">
      <figure><DepthView /><figcaption class="label">Depth</figcaption></figure>
      <figure><ColourView /><figcaption class="label">Colour</figcaption></figure>
    </div>
  </Panel>
</template>

<style scoped>
.rec { display: flex; align-items: center; gap: 13px; }
.rbtn {
  width: 42px; height: 42px; border-radius: 50%;
  border: 1px solid var(--line-strong);
  display: grid; place-items: center; flex: none;
  transition: 140ms ease;
}
.rbtn:hover:not(:disabled) { border-color: var(--bad); }
.rbtn:disabled { opacity: 0.35; cursor: not-allowed; }
.rbtn.live { border-color: var(--bad); box-shadow: 0 0 0 3px rgba(232,93,74,0.12); }
.glyph { width: 16px; height: 16px; border-radius: 50%; background: var(--bad); transition: 140ms ease; }
.glyph.stop { border-radius: 2px; width: 13px; height: 13px; }
.ct { display: flex; flex-direction: column; gap: 3px; }
.t { font-size: 19px; font-weight: 300; letter-spacing: 0.02em; }
.f { font-size: 10.5px; color: var(--text-faint); }

.trk {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: var(--r); font-size: 11.5px;
  border: 1px solid var(--line);
}
.trk .dot { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.trk.idle { color: var(--text-faint); }
.trk.idle .dot { background: var(--text-faint); }
.trk.good { color: var(--text-dim); }
.trk.good .dot { background: var(--accent); }
.trk.weak { color: var(--warn); border-color: rgba(233,180,76,0.3); background: rgba(233,180,76,0.06); }
.trk.weak .dot { background: var(--warn); }
.trk.lost { color: var(--bad); border-color: rgba(232,93,74,0.42); background: rgba(232,93,74,0.10); }
.trk.lost .dot { background: var(--bad); }
.fit { margin-left: auto; font-size: 10px; opacity: 0.8; }

.warn { margin: 0; font-size: 10.5px; line-height: 1.6; color: var(--warn); }

.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }
.sum dd.bad { color: var(--warn); }

.acts { display: flex; gap: 8px; }
.acts .btn { flex: 1; }

.thumbs { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
figure { margin: 0; display: flex; flex-direction: column; gap: 6px; }
</style>
