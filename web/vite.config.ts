import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
      '/health': { target: 'http://127.0.0.1:8787', changeOrigin: false },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalized = id.replace(/\\/g, '/')
          if (!normalized.includes('node_modules')) return undefined
          if (normalized.includes('/react/') || normalized.includes('/react-dom/') || normalized.includes('/scheduler/')) {
            return 'vendor-react'
          }
          if (normalized.includes('/lucide-react/')) {
            return 'vendor-icons'
          }
          if (
            normalized.includes('/react-markdown/') ||
            normalized.includes('/remark-') ||
            normalized.includes('/micromark') ||
            normalized.includes('/mdast-') ||
            normalized.includes('/hast-') ||
            normalized.includes('/unified/') ||
            normalized.includes('/vfile')
          ) {
            return 'vendor-markdown'
          }
          return undefined
        },
      },
    },
  },
})
