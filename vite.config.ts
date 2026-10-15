import { defineConfig } from 'vite';
import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'legal_helper/webui_dist',
    emptyOutDir: true,
  },
  server: {
    host: '0.0.0.0',
    port: 5174,
    proxy: {
      // LEGAL_HELPER_API points the dev UI at another backend (a test server).
      '/api': process.env.LEGAL_HELPER_API ?? 'http://0.0.0.0:8010',
    },
  },
});
