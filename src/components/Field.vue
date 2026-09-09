<script setup>
defineProps({ label: String, value: [String, Number] })
</script>

<template>
  <div class="f">
    <div class="row">
      <span class="label">{{ label }}</span>
      <span v-if="value !== undefined" class="mono v">{{ value }}</span>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.f { display: flex; flex-direction: column; gap: 7px; }
.v { font-size: 11px; color: var(--text-dim); }
</style>
