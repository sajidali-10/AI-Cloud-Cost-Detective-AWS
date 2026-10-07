import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Vitest config — kept lean. No path aliases so the existing
// `tsconfig.json` keeps working unchanged.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/tests/setup.ts'],
    include: ['src/tests/**/*.test.{ts,tsx}'],
    css: false,
    clearMocks: true,
    restoreMocks: true,
  },
})
