// Page states worth scanning: each one puts different elements on screen (drive
// states, an offline drive, urgent queue rows, the comparison table).
export const STATES = [
  { name: 'first load', query: '' },
  { name: 'big restore mid-run', query: '?scenario=big-restore&policy=fifo&t=1500' },
  { name: 'urgent requests queued', query: '?scenario=urgent-starve&policy=fifo&t=900' },
  { name: 'drive failed', query: '?scenario=drive-failure&t=1500' },
  { name: 'comparison table open', query: '?scenario=big-restore&t=900&compare=1' },
];

export const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 720 },
  { name: 'phone 360', width: 360, height: 740 },
];
