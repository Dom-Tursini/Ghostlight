<script setup>
import { computed, onMounted, ref } from 'vue'
import Panel from '../components/Panel.vue'
import Field from '../components/Field.vue'
import { useSensor } from '../composables/useSensor'

defineProps({ s: Object })
const emit = defineEmits(['next'])

const { sensor, fuseBundle, listBundles, deleteBundle, setVolume } = useSensor()

const voxelMm = ref(sensor.track.voxel_mm || 2)
const picked = ref(null)

onMounted(() => { listBundles() })

const take = computed(() => picked.value
  || (sensor.bundle && !sensor.bundle.recording ? sensor.bundle : null)
  || sensor.bundles[0] || null)

const progress = computed(() => {
  const f = sensor.fuse
  return f && f.total ? f.done / f.total : 0
})

// Voxels along one edge of the volume, and what that costs in VRAM.
const grid = computed(() => Math.round(sensor.volume.size / (voxelMm.value / 1000)))
const vram = computed(() => (Math.pow(grid.value, 3) * 8 / 1e9).toFixed(2))
const changed = computed(() => Math.abs(voxelMm.value - (sensor.track.voxel_mm || 0)) > 0.05)

function run () {
  if (changed.value) setVolume({ ...sensor.volume, voxel: voxelMm.value / 1000 })
  // The volume rebuild is a separate message and clears the scan, which is
  // exactly what a re-fuse wants; the service applies them in order.
  // Sent so the service can tell whether the rebuild above actually took.
  fuseBundle(take.value ? take.value.path : undefined, voxelMm.value / 1000)
}

function mb (b) {
  return b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : Math.round(b / 1e6) + ' MB'
}
function when (t) {
  if (!t) return ''
  const d = new Date(t * 1000)
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ' ' +
         d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <Panel title="Fuse">
    <p class="blurb">
      Every frame is on disk, so you can re-fuse at a different voxel size
      without rescanning.
    </p>

    <p v-if="!take" class="warn">
      No recording on disk yet. Record something first.
    </p>

    <template v-else>
      <Field label="Recording" :value="take.frames + ' frames'">
        <select class="sel mono" v-model="picked">
          <option :value="null">
            {{ take.name }} · {{ take.frames }} frames · {{ mb(take.bytes) }}
          </option>
          <option v-for="b in sensor.bundles.filter(x => x.path !== take.path)"
                  :key="b.path" :value="b">
            {{ b.name }} · {{ b.frames }} frames · {{ mb(b.bytes) }}
          </option>
        </select>
        <span class="hint mono">
          {{ when(take.started) }} · {{ take.seconds }}s · recorded at {{ (take.voxel * 1000).toFixed(1) }} mm
        </span>
      </Field>

      <Field label="Voxel size" :value="`${voxelMm.toFixed(1)} mm`">
        <input type="range" min="1" max="16" step="0.5" v-model.number="voxelMm">
        <span class="hint mono">{{ grid }}³ voxels · {{ vram }} GB VRAM</span>
        <span class="hint">
          Frames are re-registered, not replayed, so a finer grid also tracks
          better.
        </span>
      </Field>
    </template>

    <div v-if="sensor.fuse" class="prog">
      <div class="bar"><i :style="{ width: (progress * 100).toFixed(1) + '%' }" /></div>
      <span class="mono pct">
        {{ sensor.fuse.done }} / {{ sensor.fuse.total }} frames
      </span>
    </div>

    <p v-if="sensor.lastError" class="err">{{ sensor.lastError }}</p>

    <dl v-if="sensor.track.points" class="sum mono">
      <div><dt>Registered</dt><dd>{{ sensor.track.registered.toLocaleString() }}</dd></div>
      <div><dt>Surface voxels</dt><dd>{{ sensor.track.points.toLocaleString() }}</dd></div>
      <div><dt>Grid</dt><dd>{{ sensor.track.voxel_mm }} mm</dd></div>
    </dl>

    <button class="btn primary w" :disabled="!take || !!sensor.busy" @click="run">
      {{ sensor.busy === 'fuse' ? 'Fusing...' : (changed ? `Re-fuse at ${voxelMm.toFixed(1)} mm` : 'Fuse') }}
    </button>

    <div class="acts">
      <button class="btn danger" :disabled="!take || !!sensor.busy"
              @click="deleteBundle(take.path); picked = null">Delete take</button>
      <button class="btn" :disabled="!sensor.track.points || !!sensor.busy"
              @click="emit('next')">Reconstruct</button>
    </div>
  </Panel>

  <Panel v-if="sensor.bundles.length > 1" title="On disk">
    <ul class="takes">
      <li v-for="b in sensor.bundles" :key="b.path" :class="{ on: take && b.path === take.path }">
        <span class="mono nm">{{ b.name }}</span>
        <span class="mono sz">{{ b.frames }}f · {{ mb(b.bytes) }}</span>
      </li>
    </ul>
    <p class="hint">
      Raw frames are large. Delete takes once their mesh is exported.
    </p>
  </Panel>
</template>

<style scoped>
.blurb { margin: 0; font-size: 11.5px; line-height: 1.7; color: var(--text-faint); }
.hint { font-size: 10px; line-height: 1.55; color: var(--text-faint); }
.warn { margin: 0; font-size: 10.5px; line-height: 1.6; color: var(--warn); }
.err { margin: 0; font-size: 11px; line-height: 1.6; color: var(--bad); }

.sel {
  width: 100%; height: 26px; padding: 0 6px; font-size: 10.5px;
  border: 1px solid var(--line-strong); border-radius: var(--r);
  background: var(--surface-2); color: var(--text-dim);
}

.prog { display: flex; flex-direction: column; gap: 5px; }
.bar { height: 3px; border-radius: 2px; background: var(--surface-3); overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); transition: width 160ms linear; }
.pct { font-size: 10px; color: var(--text-faint); }

.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }

.acts { display: flex; gap: 8px; }
.acts .btn { flex: 1; }
.w { width: 100%; }

.takes {
  list-style: none; margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: 1px;
  /* Grows by one row per recording kept, so it needs a ceiling of its own
     rather than pushing the panel past the bottom of the column. */
  max-height: 168px; overflow-y: auto;
  scrollbar-width: thin; scrollbar-color: var(--line-strong) transparent;
}
.takes::-webkit-scrollbar { width: 5px; }
.takes::-webkit-scrollbar-track { background: transparent; }
.takes::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 3px; }
.takes li {
  display: flex; justify-content: space-between; gap: 10px;
  padding: 5px 7px; border-radius: var(--r); font-size: 10px;
  color: var(--text-faint);
}
.takes li.on { background: var(--surface-2); color: var(--text-dim); }
.nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sz { flex: none; }
</style>
