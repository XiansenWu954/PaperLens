/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Batch A red-config: runs ONLY the ingestion-lifecycle red spec (expected
// FAILs). The default `npm test` keeps 54/54 green; Tasks 5.5 will move these
// assertions back into default discovery.
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['src/components/__tests__/*.red-spec.ts'],
  },
})
