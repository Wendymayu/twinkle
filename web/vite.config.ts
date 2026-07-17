import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// In dev, Vite serves the page on :5173 and proxies /ws to the Gateway
// (default ws://127.0.0.1:19000), keeping the browser same-origin.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'ws://127.0.0.1:19000',
        ws: true,
      },
    },
  },
})
