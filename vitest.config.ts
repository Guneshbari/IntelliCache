import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    setupFiles: ['./frontend/tests/setup.ts'],
    include: ['frontend/tests/**/*.{test,spec}.ts'],
    testTimeout: 10000,
    hookTimeout: 10000,
  },
})
