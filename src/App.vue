<script setup>
import { computed, ref, watch } from 'vue'
import { useSession } from './composables/useSession'
import { useSensor }  from './composables/useSensor'
import Viewport   from './components/Viewport.vue'
import StageBar   from './components/StageBar.vue'
import StatusBar  from './components/StatusBar.vue'
import Prepare     from './stages/Prepare.vue'
import Record      from './stages/Record.vue'
import Fuse        from './stages/Fuse.vue'
import Reconstruct from './stages/Reconstruct.vue'
import Refine      from './stages/Refine.vue'
import Export      from './stages/Export.vue'

const { s, STAGES, go, unlock, start, toggleRecord } = useSession()
const { sensor } = useSensor()
const vp = ref(null)

// When the capture service is up it owns the truth. Otherwise the mock state
// stands, so the UI is still explorable with nothing plugged in.
watch(() => [sensor.connected, sensor.online], ([connected, online]) => {
  if (connected) s.sensorOnline = online
}, { immediate: true })

const sensorDetail = computed(() => {
  if (!sensor.connected) return 'service offline'
  if (!sensor.online) return sensor.error || 'not detected'
  return `${sensor.name} · ${sensor.vid}:${sensor.pid} · ${sensor.width}×${sensor.height} · ${sensor.fps} Hz`
})

const mode = computed(() => ({
  prepare: 'empty', record: 'scanning', fuse: 'cloud',
  reconstruct: 'cloud', refine: 'mesh', export: 'mesh'
}[s.stage]))

const cover = computed(() => s.stage === 'record' ? s.coverage : 1)

function next (id) { const i = STAGES.findIndex(x => x.id === id); unlock(i); s.stage = id }

// The panel column scrolls now that it can outgrow the window, and it is full
// of sliders. A wheel over one of those must move the panel, not nudge the
// value: silently retuning the voxel size while someone scrolls past it is a
// nasty way to lose a setting. preventDefault alone would kill the scroll too,
// so the scroll is reapplied by hand.
function wheelGuard (e) {
  const el = e.target
  if (!el || el.tagName !== 'INPUT' || el.type !== 'range') return
  e.preventDefault()
  const col = el.closest('.panels')
  if (col) col.scrollTop += e.deltaY
}
</script>

<template>
  <div class="app">
    <main class="stage">
      <Viewport
        ref="vp"
        :mode="mode"
        :coverage="cover"
        :show-box="s.stage === 'prepare' || s.stage === 'record'"
      />

      <div class="hud left">
        <img class="logo" src="/brand/logo.svg" alt="Ghostlight" />

        <div class="panels" @wheel="wheelGuard">
          <Prepare     v-if="s.stage === 'prepare'"     :s="s" @start="start" />
          <Record      v-else-if="s.stage === 'record'"  :s="s" @next="next('fuse')" />
          <Fuse        v-else-if="s.stage === 'fuse'"    :s="s" @next="next('reconstruct')" />
          <Reconstruct v-else-if="s.stage === 'reconstruct'" :s="s" @next="next('refine')" />
          <Refine      v-else-if="s.stage === 'refine'"  :s="s" @next="next('export')" />
          <Export      v-else :s="s" />
        </div>
      </div>

      <div class="hud top">
        <StageBar :stages="STAGES" :current="s.stage" :furthest="s.furthest" @go="go" />
      </div>

      <div class="hud right">
        <div class="readout mono">
          <template v-if="sensor.online && (s.stage === 'record' || sensor.track.points)">
            <div><span>registered</span><b>{{ sensor.track.registered.toLocaleString() }}</b></div>
            <div><span>points</span><b>{{ sensor.track.points.toLocaleString() }}</b></div>
            <div><span>fitness</span><b>{{ sensor.track.fitness.toFixed(2) }}</b></div>
            <div><span>rmse</span><b>{{ (sensor.track.rmse * 1000).toFixed(1) }} mm</b></div>
          </template>
          <template v-else-if="s.stage === 'prepare'">
            <div><span>volume</span><b>{{ sensor.vol ? (sensor.vol.size ** 3).toFixed(2) : '0.00' }} m³</b></div>
            <div><span>voxels</span><b>{{ sensor.vol ? Math.round(sensor.vol.size / sensor.vol.voxel) : 0 }}³</b></div>
          </template>
          <template v-else>
            <div v-if="s.vertices"><span>vertices</span><b>{{ s.vertices.toLocaleString() }}</b></div>
            <div v-if="s.faces"><span>faces</span><b>{{ s.faces.toLocaleString() }}</b></div>
          </template>
        </div>
      </div>
    </main>

    <StatusBar
      :sensor="s.sensorOnline"
      :detail="sensorDetail"
      :live="sensor.online"
      :gpu="sensor.gpu"
      :tracking="s.tracking"
      :fps="sensor.online ? sensor.fpsMeasured : (s.recording ? 30 : 0)" />
  </div>
</template>

<style scoped>
.app { height: 100%; display: flex; flex-direction: column; background: var(--bg); }
.stage { flex: 1; position: relative; overflow: hidden; }

.hud { position: absolute; pointer-events: none; z-index: 2; }

.hud.left {
  /* The column itself does not scroll. It is a fixed masthead with a scrolling
     list under it, so the logo stays put and the panels run out of sight
     beneath it rather than carrying it away with them.

     Height follows the content, capped at the viewport. Stretching to the
     bottom edge instead would mean the column swallowed clicks and drags meant
     for the 3D view in whatever empty space sat under the panels. */
  left: 18px; top: 18px; width: 306px;
  max-height: calc(100% - 36px);
  display: flex; flex-direction: column;
  pointer-events: none;
}

.panels {
  /* 288 of panel, a 12px gutter, then the scrollbar. Everything is border-box,
     so this has to carry all three or the panels lose the width instead of
     gaining the gap. scrollbar-gutter keeps the space reserved when there is
     nothing to scroll, so panels do not jump wider the moment content fits.

     min-height:0 is what actually lets it scroll: a flex child defaults to a
     minimum of its content, so without it the column grows past its own cap
     and nothing ever overflows. */
  flex: 0 1 auto; min-height: 0;
  padding-right: 12px;
  scrollbar-gutter: stable;
  display: flex; flex-direction: column; gap: 14px;
  overflow-y: auto; overflow-x: hidden;
  pointer-events: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--line-strong) transparent;
}
/* Panels keep their own height and scroll as a group. Without this flex
   compresses them to fit and every panel loses its bottom rows at once. */
.panels > * { flex: none; }
.panels::-webkit-scrollbar { width: 6px; }
.panels::-webkit-scrollbar-track { background: transparent; }
.panels::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 3px; }
.panels::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.24); }
.logo {
  display: block; height: auto; flex: none;
  /* The gutter and scrollbar live on .panels, not here, so the logo is centred
     over the panels rather than over the whole column. */
  width: calc(100% - 56px);
  margin: 4px auto 22px;
  pointer-events: none;
}

.hud.top {
  top: 18px; left: 18px; right: 18px;
  display: flex; justify-content: center;
}

.hud.right { right: 18px; top: 18px; display: flex; flex-direction: column; align-items: flex-end; }

.readout {
  pointer-events: auto;
  display: flex; flex-direction: column; gap: 5px;
  font-size: 10.5px; text-align: right;
}
.readout div { display: flex; gap: 14px; justify-content: flex-end; }
.readout span { color: var(--text-faint); }
.readout b { font-weight: 400; color: var(--text-dim); min-width: 74px; }
</style>
