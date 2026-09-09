<script setup>
import { ref } from 'vue'
import Panel from './Panel.vue'
import { useSensor } from '../composables/useSensor'

const { sensor } = useSensor()
// Open by default while there is no sensor: that is when people need it.
const open = ref(!sensor.online)

const STEPS = [
  {
    n: '01',
    t: 'Install the Kinect SDK 1.8',
    d: 'The only thing that opens a Kinect v1 on Windows. Install it before ' +
       'plugging the sensor in. Not 2.0, which stops v1 being detected.',
    link: 'https://www.microsoft.com/en-us/download/details.aspx?id=40278'
  },
  {
    n: '02',
    t: 'Connect the sensor, then wait',
    d: 'Mains adapter first, then USB. The first bind can take a few minutes. ' +
       'Device Manager should end up showing "Kinect for Windows Camera".'
  }
]

// Deliberately not here: Zadig. Rebinding the camera to libusbK or WinUSB is
// the right move for OpenNI2, but it takes the device away from Microsoft's
// driver and Ghostlight then cannot open it at all on Windows.
</script>

<template>
  <Panel title="Sensor setup">
    <template #hdr>
      <button class="tog" @click="open = !open">{{ open ? 'hide' : 'show' }}</button>
    </template>

    <template v-if="open">
      <p class="intro">
        Kinect v1 on Windows needs one driver package. Without it the camera often
        never appears on the USB bus at all.
      </p>

      <ol class="steps">
        <li v-for="st in STEPS" :key="st.n">
          <span class="n mono">{{ st.n }}</span>
          <div class="tx">
            <span class="t">{{ st.t }}</span>
            <span class="d">{{ st.d }}</span>
            <a v-if="st.link" :href="st.link" target="_blank" rel="noopener" class="mono lk">
              {{ st.link.replace(/^https?:\/\//, '').split('/')[0] }} ↗
            </a>
          </div>
        </li>
      </ol>

      <p class="note">
        Linux needs none of this. The kernel enumerates the sensor on its own and
        OpenNI2 opens it directly.
      </p>
    </template>
  </Panel>
</template>

<style scoped>
.tog { font-size: 10px; color: var(--text-faint); }
.tog:hover { color: var(--text-dim); }
.intro, .note { margin: 0; font-size: 11px; line-height: 1.65; color: var(--text-faint); }
.note { padding-top: 2px; border-top: 1px solid var(--line); margin-top: 2px; padding-top: 10px; }

.steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.steps li { display: flex; gap: 10px; }
.n { font-size: 9px; color: var(--accent); padding-top: 2px; flex: none; }
.tx { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.t { font-size: 12px; color: var(--text); }
.d { font-size: 10.5px; line-height: 1.6; color: var(--text-faint); }
.lk { font-size: 10px; color: var(--text-dim); text-decoration: none; margin-top: 2px; }
.lk:hover { color: var(--accent); }
</style>
