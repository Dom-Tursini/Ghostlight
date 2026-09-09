<script setup>
import Panel from '../components/Panel.vue'
import Field from '../components/Field.vue'
import { useSensor } from '../composables/useSensor'

defineProps({ s: Object })
const emit = defineEmits(['next'])

const { sensor, reconstruct } = useSensor()

// Detail is set by voxel size before scanning, not here: marching cubes just
// reads the surface out of the TSDF that was already built at that resolution.
</script>

<template>
  <Panel title="Reconstruct">
    <p class="blurb">
      {{ sensor.track.points.toLocaleString() }} surface voxels from
      {{ sensor.track.registered.toLocaleString() }} frames at
      {{ sensor.track.voxel_mm }} mm.
    </p>

    <p v-if="sensor.lastError" class="err">{{ sensor.lastError }}</p>

    <dl v-if="sensor.mesh" class="sum mono">
      <div><dt>Vertices</dt><dd>{{ sensor.mesh.vertices.toLocaleString() }}</dd></div>
      <div><dt>Faces</dt><dd>{{ sensor.mesh.faces.toLocaleString() }}</dd></div>
      <div><dt>Watertight</dt><dd>{{ sensor.mesh.watertight ? 'yes' : 'no' }}</dd></div>
      <div v-if="sensor.mesh.size_mm"><dt>Size</dt><dd>{{ sensor.mesh.size_mm.join(' x ') }} mm</dd></div>
    </dl>

    <button class="btn primary w"
            :disabled="sensor.busy === 'reconstruct' || sensor.track.points < 1000"
            @click="reconstruct()">
      {{ sensor.busy === 'reconstruct' ? 'Reconstructing...' : (sensor.mesh ? 'Reconstruct again' : 'Reconstruct') }}
    </button>

    <button v-if="sensor.mesh" class="btn w" @click="emit('next')">Continue to refine</button>
  </Panel>
</template>

<style scoped>
.blurb { margin: 0; font-size: 11.5px; line-height: 1.7; color: var(--text-faint); }
.hint { font-size: 10px; color: var(--text-faint); }
.err { margin: 0; font-size: 11px; line-height: 1.6; color: var(--bad); }
.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }
.w { width: 100%; }
</style>
