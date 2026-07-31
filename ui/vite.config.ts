import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/health': 'http://localhost:7861',
      '/events': 'http://localhost:7861',
      '/runs': 'http://localhost:7861',
      '/graph': 'http://localhost:7861',
      '/text': 'http://localhost:7861',
      '/nodes': 'http://localhost:7861',
      '/tree': 'http://localhost:7861',
      '/groups': 'http://localhost:7861',
      '/resolve': 'http://localhost:7861',
      '/stream': {
        target: 'http://localhost:7861',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
