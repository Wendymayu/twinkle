import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// dev 模式下 Vite 在 :5173 提供页面，并将 /ws 代理到 Gateway
//（默认 ws://127.0.0.1:19000），使浏览器保持同源。
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
