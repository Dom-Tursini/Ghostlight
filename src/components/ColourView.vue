<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useSensor } from '../composables/useSensor'

const canvas = ref(null)
const { sensor, onColour } = useSensor()
let lastFrame = null
let ctx, img, off
let stop = null

function draw (f) {
  if (!ctx) return
  if (!img || img.width !== f.w || img.height !== f.h) {
    canvas.value.width = f.w
    canvas.value.height = f.h
    img = ctx.createImageData(f.w, f.h)
    off = img.data
    off.fill(255)
  }
  // The SDK hands us BGRA; canvas wants RGBA.
  const b = f.bgra
  for (let i = 0, n = f.w * f.h; i < n; i++) {
    const s = i * 4, d = i * 4
    off[d]     = b[s + 2]
    off[d + 1] = b[s + 1]
    off[d + 2] = b[s]
  }
  ctx.putImageData(img, 0, 0)
  lastFrame = f
  drawMarkers(f)
}

function drawMarkers (f) {
  const m = sensor.markers
  if (!m || !m.length) return
  // Marker coordinates are in full-resolution colour pixels; the preview may
  // be a different size, so scale rather than assuming they match.
  const sx = f.w / 640, sy = f.h / 480
  ctx.strokeStyle = '#00FFAE'
  ctx.lineWidth = Math.max(1, Math.round(f.w / 220))
  for (const [u, v] of m) {
    const r = Math.max(3, f.w / 90)
    ctx.beginPath()
    ctx.arc(u * sx, v * sy, r, 0, Math.PI * 2)
    ctx.stroke()
  }
}

onMounted(() => {
  ctx = canvas.value.getContext('2d')
  stop = onColour(draw)
})

onBeforeUnmount(() => stop && stop())
</script>

<template>
  <div class="wrap">
    <canvas ref="canvas" class="cv" />
    <div v-if="sensor.mode !== 'geometry'" class="badge mono">{{ sensor.markers.length }}</div>
    <div v-if="!sensor.online" class="idle">
      <span class="mono">{{ sensor.connected ? 'no sensor' : 'service offline' }}</span>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  position: relative; aspect-ratio: 4/3; border-radius: 3px;
  border: 1px solid var(--line); overflow: hidden; background: #0A0C0D;
}
.cv { width: 100%; height: 100%; display: block; }
.badge {
  position: absolute; top: 3px; right: 4px;
  font-size: 9px; padding: 0 4px; border-radius: 2px;
  color: #04120C; background: var(--accent);
}
.idle {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  font-size: 9.5px; color: var(--text-faint);
  background: repeating-linear-gradient(-45deg, #0D1011 0 6px, #0A0C0D 6px 12px);
}
</style>
