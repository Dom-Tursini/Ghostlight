<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useSensor } from '../composables/useSensor'

const props = defineProps({
  near: { type: Number, default: 500 },    // mm, mapped to the hot end of the ramp
  far:  { type: Number, default: 4000 }
})

const canvas = ref(null)
const { sensor, onFrame } = useSensor()
let ctx, img, off
let stop = null

// 256-entry ramp: near = accent mint, far = deep blue, invalid = near-black.
const RAMP = (() => {
  const r = new Uint8Array(256 * 3)
  for (let i = 0; i < 256; i++) {
    const t = i / 255
    // mint -> teal -> blue -> near black
    const rr = Math.round(20 * (1 - t) + 8 * t)
    const gg = Math.round(255 * Math.pow(1 - t, 0.75) + 14 * t)
    const bb = Math.round(174 * Math.pow(1 - t, 0.35) + 34 * t)
    r[i * 3] = rr; r[i * 3 + 1] = gg; r[i * 3 + 2] = bb
  }
  return r
})()

function draw (f) {
  if (!ctx) return
  if (!img || img.width !== f.w || img.height !== f.h) {
    canvas.value.width = f.w
    canvas.value.height = f.h
    img = ctx.createImageData(f.w, f.h)
    off = img.data
    off.fill(255)
  }

  const d = f.data
  const near = props.near
  const span = Math.max(1, props.far - props.near)

  for (let i = 0, p = 0; i < d.length; i++, p += 4) {
    const mm = d[i]
    if (mm === 0) {                       // no return
      off[p] = 10; off[p + 1] = 12; off[p + 2] = 13
      continue
    }
    let t = (mm - near) / span
    t = t < 0 ? 0 : t > 1 ? 1 : t
    const c = (t * 255) | 0
    off[p]     = RAMP[c * 3]
    off[p + 1] = RAMP[c * 3 + 1]
    off[p + 2] = RAMP[c * 3 + 2]
  }
  ctx.putImageData(img, 0, 0)
}

onMounted(() => {
  ctx = canvas.value.getContext('2d')
  stop = onFrame(draw)
})

onBeforeUnmount(() => stop && stop())
</script>

<template>
  <div class="wrap">
    <canvas ref="canvas" class="cv" />
    <div v-if="!sensor.online" class="idle">
      <span class="mono">{{ sensor.connected ? 'no sensor' : 'service offline' }}</span>
    </div>
  </div>
</template>

<style scoped>
.wrap { position: relative; aspect-ratio: 4/3; border-radius: 3px; border: 1px solid var(--line); overflow: hidden; background: #0A0C0D; }
.cv { width: 100%; height: 100%; display: block; image-rendering: pixelated; }
.idle {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  font-size: 9.5px; color: var(--text-faint);
  background: repeating-linear-gradient(-45deg, #0D1011 0 6px, #0A0C0D 6px 12px);
}
</style>
