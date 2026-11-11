import { resolve } from 'path'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(import.meta.dirname, 'frontend'),
  publicDir: resolve(import.meta.dirname, 'frontend/public'),
  base: './',
  build: {
    outDir: resolve(import.meta.dirname, 'dist'),
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5174,
    strictPort: false,
  },
})
