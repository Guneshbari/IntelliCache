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

function fail(message) {
  console.error(`Error: ${message}`)
  process.exit(1)
}

function buildFirefoxPackage() {
  if (!fs.existsSync(distDir)) {
    fail('dist/ directory does not exist. Run vite build first.')
  }

  // 1. Clean and recreate dist-firefox/
  if (fs.existsSync(firefoxDistDir)) {
    fs.rmSync(firefoxDistDir, { recursive: true, force: true })
  }
  fs.cpSync(distDir, firefoxDistDir, { recursive: true })

  // 2. Read and adapt manifest.json for Firefox
  const manifestPath = path.join(firefoxDistDir, 'manifest.json')
  if (!fs.existsSync(manifestPath)) {
    fail(`manifest.json not found in ${firefoxDistDir}. Vite build output is incomplete.`)
  }

  let manifestRaw
  try {
    manifestRaw = fs.readFileSync(manifestPath, 'utf8')
  } catch (err) {
    fail(`Cannot read manifest.json: ${err instanceof Error ? err.message : String(err)}`)
  }

  let manifest
  try {
    manifest = JSON.parse(manifestRaw)
  } catch (err) {
    fail(`manifest.json is not valid JSON: ${err instanceof Error ? err.message : String(err)}`)
  }

  // Ensure gecko specific settings, preserving any pre-existing values.
  const existingGecko = manifest.browser_specific_settings?.gecko ?? {}
  manifest.browser_specific_settings = {
    ...manifest.browser_specific_settings,
    gecko: {
      id: existingGecko.id ?? 'intellicache-collector@research.local',
      strict_min_version: existingGecko.strict_min_version ?? '109.0',
    },
  }

  // In Firefox MV3, background scripts (event pages) are the standard supported mechanism.
  // Only string service_worker entries are converted; anything else is left untouched
  // with a warning so an unexpected manifest shape can't silently produce an invalid bundle.
  if (manifest.background && manifest.background.service_worker !== undefined) {
    const swScript = manifest.background.service_worker
    if (typeof swScript === 'string' && swScript.length > 0) {
      manifest.background = {
        scripts: [swScript],
        type: 'module',
      }
    } else {
      console.warn(
        'Warning: manifest.background.service_worker is not a string; leaving background section unchanged.'
      )
    }
  }

  // Firefox uses sidebar_action instead of Chromium's side_panel.
  delete manifest.side_panel
  if (Array.isArray(manifest.permissions)) {
    manifest.permissions = manifest.permissions.filter((p) => p !== 'sidePanel')
  }
  manifest.sidebar_action = {
    default_panel: 'src/popup/index.html',
    default_title: 'IntelliCache Collector',
  }

  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8')

  console.log('✓ Firefox extension bundle generated in dist-firefox/')
  console.log('  - Manifest: Gecko MV3 with background.scripts event page')
  console.log(`  - Gecko ID: ${manifest.browser_specific_settings.gecko.id}`)
  console.log(
    `  - Min Version: Firefox ${manifest.browser_specific_settings.gecko.strict_min_version}+`
  )
}

buildFirefoxPackage()
