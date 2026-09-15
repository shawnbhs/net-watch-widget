import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/vite'

// `base: './'` matters: Electron loads the build from a file:// URL, and the
// default absolute '/assets/...' paths resolve against the drive root there.
export default defineConfig({
  base: './',
  plugins: [react(), tailwind()],
  build: { outDir: 'dist', emptyOutDir: true },
})
