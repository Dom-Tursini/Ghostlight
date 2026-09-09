<script setup>
import Panel from '../components/Panel.vue'
import { useSensor } from '../composables/useSensor'

defineProps({ s: Object })
defineEmits(['next'])

const { sensor, refine } = useSensor()

const OPS = [
  { id: 'crop',      name: 'Crop to bounding box', note: 'drops floor and walls' },
  // Before islands on purpose: cutting the platter away is usually what
  // separates the subject from what it is standing on, and until that happens
  // the two are one component and keeping the largest keeps both.
  { id: 'turntable', name: 'Remove turntable',     note: 'cuts at the fitted plane' },
  { id: 'islands',   name: 'Remove loose parts',   note: 'keep largest component' },
  { id: 'smooth',    name: 'Smooth',               note: 'Taubin, no shrinkage' },
  { id: 'simplify',  name: 'Simplify',             note: 'quadric decimation, 50%' }
]

// Turntable removal needs a plane to cut at, and that comes from the Prepare
// stage. Offering the toggle with nothing behind it would look like a bug.
const ready = (id) => id !== 'turntable' || !!sensor.turntable

function toggle (id) {
  if (!ready(id)) return
  refine({ ...sensor.ops, [id]: !sensor.ops[id] }, sensor.vol?.size)
}
</script>

<template>
  <Panel title="Operations">
    <template #hdr>
      <span class="mono cnt">{{ Object.values(sensor.ops).filter(Boolean).length }} / {{ OPS.length }}</span>
    </template>

    <p class="blurb">
      Non-destructive. Every toggle rebuilds from the raw mesh.
    </p>

    <ul class="ops" :class="{ busy: sensor.busy === 'refine' }">
      <li v-for="op in OPS" :key="op.id"
          :class="{ on: sensor.ops[op.id], off: !ready(op.id) }" @click="toggle(op.id)">
        <i class="chk"><svg viewBox="0 0 10 10"><path d="M1 5l2.6 2.6L9 2.2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg></i>
        <div class="tx">
          <span class="n">{{ op.name }}</span>
          <span class="mono nt">{{ op.note }}</span>
        </div>
      </li>
    </ul>

    <p v-if="!sensor.turntable" class="hint">
      Remove turntable needs a fitted plane, from Turntable in Prepare.
    </p>

    <dl v-if="sensor.mesh" class="sum mono">
      <div><dt>Vertices</dt><dd>{{ sensor.mesh.vertices.toLocaleString() }}</dd></div>
      <div><dt>Faces</dt><dd>{{ sensor.mesh.faces.toLocaleString() }}</dd></div>
      <div><dt>Watertight</dt><dd>{{ sensor.mesh.watertight ? 'yes' : 'no' }}</dd></div>
    </dl>

    <button class="btn primary w" :disabled="!sensor.mesh || !!sensor.busy" @click="$emit('next')">
      {{ sensor.busy === 'refine' ? 'Rebuilding...' : 'Continue to export' }}
    </button>
  </Panel>
</template>

<style scoped>
.cnt { font-size: 10px; color: var(--text-faint); }
.blurb { margin: 0; font-size: 11px; line-height: 1.65; color: var(--text-faint); }
.ops { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; }
.ops.busy { opacity: 0.5; pointer-events: none; }
.ops li {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 9px; border-radius: var(--r); cursor: pointer;
  transition: 110ms ease;
}
.ops li:hover { background: var(--surface-2); }
.ops li.off { opacity: 0.4; cursor: not-allowed; }
.ops li.off:hover { background: none; }
.hint { font-size: 10px; line-height: 1.55; color: var(--text-faint); }
.chk {
  width: 14px; height: 14px; flex: none; border-radius: 3px;
  border: 1px solid var(--line-strong);
  display: grid; place-items: center; color: transparent;
  transition: 110ms ease;
}
.chk svg { width: 9px; height: 9px; }
.ops li.on .chk { background: var(--accent); border-color: var(--accent); color: #04120C; }
.tx { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.n { font-size: 12px; color: var(--text-faint); }
.ops li.on .n { color: var(--text); }
.nt { font-size: 9.5px; color: var(--text-faint); }
.sum { display: flex; flex-direction: column; gap: 6px; font-size: 11px; margin: 0; }
.sum div { display: flex; justify-content: space-between; }
.sum dt { color: var(--text-faint); }
.sum dd { margin: 0; color: var(--text-dim); }
.w { width: 100%; }
</style>
