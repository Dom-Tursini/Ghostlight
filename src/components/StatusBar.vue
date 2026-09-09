<script setup>
import { computed } from 'vue'

const props = defineProps({
  sensor: Boolean, tracking: String, fps: Number,
  detail: String, live: Boolean, gpu: Object
})

const gpuText = computed(() => {
  const g = props.gpu
  if (!g || !g.name) return 'no CUDA device'
  return `${g.name} · ${g.arch} · CUDA ${g.cuda}`
})
</script>

<template>
  <div class="bar">
    <div class="grp">
      <i class="dot" :class="sensor ? 'ok' : 'bad'" />
      <span class="label">Sensor</span>
      <span class="mono val">{{ detail }}</span>
      <span v-if="live" class="tag mono">live</span>
    </div>

    <div class="grp gpu">
      <i class="dot" :class="gpu ? 'ok' : 'bad'" />
      <span class="label">GPU</span>
      <span class="mono val">{{ gpuText }}</span>
    </div>

    <div class="sp" />

    <div class="grp" v-if="tracking">
      <i class="dot" :class="tracking === 'good' ? 'ok' : tracking === 'weak' ? 'warn' : 'bad'" />
      <span class="mono val">{{ fps }} fps</span>
    </div>
    <span class="mono ver">Ghostlight 0.1.0</span>
  </div>
</template>

<style scoped>
.bar {
  display: flex; align-items: center; gap: 20px;
  height: 26px; padding: 0 14px;
  overflow: hidden;
  border-top: 1px solid var(--line);
  background: rgba(9,10,11,0.82);
  backdrop-filter: blur(16px);
  position: relative; z-index: 3;
}
.grp { display: flex; align-items: center; gap: 8px; min-width: 0; }
/* The sensor line carries a device string that can run long. Truncating it
   keeps the bar one row high instead of pushing the version off the edge. */
.val {
  font-size: 10.5px; color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.grp .label, .grp .dot, .tag { flex: none; }
.gpu { flex: 0 1 auto; }
.ver { font-size: 10.5px; color: var(--text-faint); flex: none; }
.tag {
  font-size: 8.5px; letter-spacing: 0.1em; text-transform: uppercase;
  color: #04120C; background: var(--accent);
  padding: 1px 5px; border-radius: 2px;
}
.sp { flex: 1; }
.dot { width: 6px; height: 6px; border-radius: 50%; display: block; }
.dot.ok   { background: var(--accent); box-shadow: 0 0 7px var(--accent-glow); }
.dot.warn { background: var(--warn); }
.dot.bad  { background: var(--bad); }
</style>
