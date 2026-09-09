<script setup>
defineProps({ stages: Array, current: String, furthest: Number })
defineEmits(['go'])
</script>

<template>
  <nav class="steps panel">
    <template v-for="(st, i) in stages" :key="st.id">
      <button
        class="step"
        :class="{ on: st.id === current, done: i < furthest, locked: i > furthest }"
        :disabled="i > furthest"
        @click="$emit('go', st.id)"
      >
        <span class="n mono">{{ String(i + 1).padStart(2, '0') }}</span>
        {{ st.name }}
      </button>
      <span v-if="i < stages.length - 1" class="sep" :class="{ lit: i < furthest }" />
    </template>
  </nav>
</template>

<style scoped>
.steps {
  display: flex; align-items: center; gap: 2px;
  padding: 5px 7px;
  pointer-events: auto;
  /* Six steps need about 560px. Narrower than that it scrolls sideways rather
     than spilling past both edges of the window. */
  max-width: 100%; overflow-x: auto; scrollbar-width: none;
}
.steps::-webkit-scrollbar { display: none; }
.step { flex: none; }
.step {
  display: flex; align-items: center; gap: 8px;
  height: 26px; padding: 0 11px;
  border-radius: var(--r);
  font-size: 12px; color: var(--text-faint);
  transition: 120ms ease;
}
.step .n { font-size: 9px; opacity: 0.5; }
.step:not(.locked):hover { color: var(--text-dim); background: var(--surface-2); }
.step.done { color: var(--text-dim); }
.step.done .n { color: var(--accent); opacity: 0.8; }
.step.on { color: var(--text); background: var(--surface-3); }
.step.on .n { color: var(--accent); opacity: 1; }
.step.locked { opacity: 0.4; cursor: not-allowed; }

.sep { width: 12px; height: 1px; background: var(--line-strong); }
.sep.lit { background: var(--accent-deep); }
</style>
