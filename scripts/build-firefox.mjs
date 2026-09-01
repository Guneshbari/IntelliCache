/**
 * IntelliCache Collector - Firefox Build Packager
 *
 * Copies the compiled Vite distribution and produces a Firefox-optimized
 * distribution directory (`dist-firefox/`) with Gecko-compliant Manifest V3
 * background script definitions and extension identity.
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const rootDir = path.resolve(__dirname, '..')

const distDir = path.join(rootDir, 'dist')
const firefoxDistDir = path.join(rootDir, 'dist-firefox')

function buildFirefoxPackage() {
  if (!fs.existsSync(distDir)) {
    console.error('Error: dist/ directory does not exist. Run vite build first.')
    process.exit(1)
  }

  // 1. Clean and recreate dist-firefox/
  if (fs.existsSync(firefoxDistDir)) {
    fs.rmSync(firefoxDistDir, { recursive: true, force: true })
  }
  fs.cpSync(distDir, firefoxDistDir, { recursive: true })

  // 2. Read and adapt manifest.json for Firefox
  const manifestPath = path.join(firefoxDistDir, 'manifest.json')
  const manifestRaw = fs.readFileSync(manifestPath, 'utf8')
  const manifest = JSON.parse(manifestRaw)

  // Ensure gecko specific settings
  manifest.browser_specific_settings = {
    gecko: {
      id: 'intellicache-collector@research.local',
      strict_min_version: '109.0',
    },
  }

  // In Firefox MV3, background scripts (event pages) are the standard supported mechanism
  if (manifest.background && manifest.background.service_worker) {
    const swScript = manifest.background.service_worker
    manifest.background = {
      scripts: [swScript],
      type: 'module',
    }
  }

  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8')

  console.log('✓ Firefox extension bundle generated in dist-firefox/')
  console.log('  - Manifest: Gecko MV3 with background.scripts event page')
  console.log('  - Gecko ID: intellicache-collector@research.local')
  console.log('  - Min Version: Firefox 109.0+')
}

buildFirefoxPackage()
