<script setup>
import { ref } from 'vue'
import Panel from '../components/Panel.vue'
import Field from '../components/Field.vue'
import { useSensor } from '../composables/useSensor'

defineProps({ s: Object })
const { sensor, exportMesh } = useSensor()

const FORMATS = ['STL', 'PLY', 'OBJ', 'GLB']
const format = ref('STL')
</script>

<template>
  <Panel title="Export">
    <Field label="Format">
      <div class="seg">
        <button v-for="f in FORMATS" :key="f" :class="{ on: format === f }" @click="format = f">{{ f }}</button>
      </div>
      <span class="hint">
        {{ format === 'STL' ? 'geometry only' : 'geometry plus normals' }}
      </span>
    </Field>

    <dl v-if="sensor.mesh" class="sum mono">
      <div><dt>Vertices</dt><dd>{{ sensor.mesh.vertices.toLocaleString() }}</dd></div>
      <div><dt>Faces</dt><dd>{{ sensor.mesh.faces.toLocaleString() }}</dd></div>
      <div><dt>Watertight</dt><dd>{{ sensor.mesh.watertight ? 'yes' : 'no' }}</dd></div>
    </dl>
    <p v-else class="hint">Nothing reconstructed yet.</p>

    <p v-if="!sensor.mesh?.watertight && sensor.mesh" class="warn">
      Not watertight. The surface has holes or open edges.
    </p>

    <button class="btn primary w" :disabled="!sensor.mesh" @click="exportMesh(format)">
      Export {{ format }}
    </button>

    <p v-if="sensor.lastExport" class="done mono">
      {{ sensor.lastExport.ok ? 'Written to' : 'Failed writing' }}<br>{{ sensor.lastExport.path }}
    </p>
    <p v-if="sensor.lastError" class="err">{{ sensor.lastError }}</p>
  </Panel>
</template>

<style scoped>
.hint { font-size: 10px; line-height: 1.55; color: var(--text-faint); }
.warn { margin: 0; font-size: 10.5px; line-height: 1.6; color: var(--warn); }
.err { margin: 0; font-size: 11px; color: var(--bad); }
.done { margin: 0; font-size: 10px; line-height: 1.7; color: var(--accent); word-break: break-all; }
.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }
.w { width: 100%; }
</style>
