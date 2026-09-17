import { defineConfig } from 'vite'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/vite'

// `base: './'` matters: Electron loads the build from a file:// URL, and the
// default absolute '/assets/...' paths resolve against the drive root there.
//
// Two entries, because the pets let loose on the desktop cannot live in the
// widget's window -- that one is sized to its own content and moves when the
// widget is dragged. `overlay.html` is a second, full-screen, click-through
// window, and it shares the pet engine with the widget but nothing else: no
// React, no Tailwind, no sidecar.
export default defineConfig({
  base: './',
  plugins: [react(), tailwind()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL('./index.html', import.meta.url)),
        overlay: fileURLToPath(new URL('./overlay.html', import.meta.url)),
      },
    },
  },
})
