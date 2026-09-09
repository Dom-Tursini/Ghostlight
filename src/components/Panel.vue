<script setup>
defineProps({ title: String, wide: Boolean, mock: Boolean })
</script>

<template>
  <section class="panel pnl" :class="{ wide }">
    <header v-if="title" class="hd">
      <span class="label">{{ title }}</span>
      <span v-if="mock" class="mock mono">not wired up</span>
      <slot name="hdr" />
    </header>
    <div class="bd"><slot /></div>
  </section>
</template>

<style scoped>
.pnl { width: 100%; pointer-events: auto; }
.hd {
  display: flex; align-items: center; justify-content: space-between;
  height: 32px; padding: 0 12px;
  border-bottom: 1px solid var(--line);
}
.mock {
  font-size: 8.5px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--warn); border: 1px solid rgba(233,180,76,0.35);
  border-radius: 2px; padding: 1px 5px; margin-left: auto; margin-right: 8px;
}
.bd { padding: 12px; display: flex; flex-direction: column; gap: 12px; }
</style>
