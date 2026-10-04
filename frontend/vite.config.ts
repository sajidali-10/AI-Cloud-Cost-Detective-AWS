import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy only: forwards /api to the backend on the Docker network.
// In production, Nginx serves the built static assets and proxies /api itself.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 8080,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
