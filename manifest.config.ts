import { defineManifest } from '@crxjs/vite-plugin'

export default defineManifest({
  manifest_version: 3,
  name: 'IntelliCache Collector',
  version: '0.1.0',
  description: 'Local AI conversation data collector for the IntelliCache research project',
  action: {
    default_popup: 'src/popup/index.html',
    default_title: 'IntelliCache Collector',
  },
  background: {
    service_worker: 'src/background/service-worker.ts',
    type: 'module',
  },
  content_scripts: [
    {
      matches: [
        'https://chatgpt.com/*',
        'https://chat.openai.com/*',
        'https://claude.ai/*',
        'https://gemini.google.com/*',
      ],
      js: ['src/content/content.ts'],
      run_at: 'document_idle',
    },
  ],
  permissions: [],
})
