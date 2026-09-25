/**
 * IntelliCache Collector - Production Release Packager
 *
 * Automates the creation of ready-to-distribute, self-contained Chromium and Firefox
 * release packages (ZIP archives + installation documentation) that run without
 * requiring Node.js, pnpm, npm, Git, or build tools on target machines.
 *
 * Usage:
 *   pnpm release
 *   node scripts/release.mjs [--skip-tests]
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { execSync } from 'node:child_process'
import zlib from 'node:zlib'
import { Buffer } from 'node:buffer'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const rootDir = path.resolve(__dirname, '..')

// ─── UTILITY HELPERS ─────────────────────────────────────────────────────────

function fail(message) {
  console.error(`\n❌ [RELEASE ERROR] ${message}\n`)
  process.exit(1)
}

function logStep(stepNum, totalSteps, title) {
  console.log(`\n[${stepNum}/${totalSteps}] ${title}`)
}

function runCommand(cmd, cwd = rootDir) {
  try {
    execSync(cmd, { cwd, stdio: 'inherit', env: process.env })
  } catch (err) {
    fail(`Command failed: '${cmd}' (${err instanceof Error ? err.message : String(err)})`)
  }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

function recursivelyListFiles(dir, base = '') {
  let results = []
  if (!fs.existsSync(dir)) return results
  const items = fs.readdirSync(dir)
  for (const item of items) {
    const fullPath = path.join(dir, item)
    const relPath = base ? `${base}/${item}` : item
    const stat = fs.statSync(fullPath)
    if (stat.isDirectory()) {
      results = results.concat(recursivelyListFiles(fullPath, relPath))
    } else {
      results.push(relPath)
    }
  }
  return results
}

// ─── PKZIP GENERATOR (Zero-dependency Node standard library) ─────────────────

/**
 * Creates a deterministic, standard-compliant PKZIP archive buffer from a list of files.
 * Uses node:zlib deflateRawSync and crc32 with standard DOS timestamps.
 */
function createZipArchive(files) {
  const localHeaders = []
  const centralHeaders = []
  let offset = 0

  // Fixed DOS date/time: 2026-09-26 00:00:00 UTC (deterministic)
  const dosTime = 0
  const dosDate = ((2026 - 1980) << 9) | (9 << 5) | 26

  // Sort files lexicographically for deterministic archive layout
  const sortedFiles = [...files].sort((a, b) => a.name.localeCompare(b.name))

  for (const file of sortedFiles) {
    const nameBuf = Buffer.from(file.name.replace(/\\/g, '/'), 'utf8')
    const dataBuf = Buffer.isBuffer(file.data) ? file.data : Buffer.from(file.data)
    const uncompressedSize = dataBuf.length
    const crc = zlib.crc32(dataBuf)
    const compressedData = zlib.deflateRawSync(dataBuf)
    const compressedSize = compressedData.length

    // Local File Header (30 bytes + name length)
    const localHeader = Buffer.alloc(30 + nameBuf.length)
    localHeader.writeUInt32LE(0x04034b50, 0)
    localHeader.writeUInt16LE(20, 4) // Version needed to extract (2.0)
    localHeader.writeUInt16LE(0x0800, 6) // Bit 11: UTF-8 filename encoding
    localHeader.writeUInt16LE(8, 8) // Compression: DEFLATE
    localHeader.writeUInt16LE(dosTime, 10)
    localHeader.writeUInt16LE(dosDate, 12)
    localHeader.writeUInt32LE(crc, 14)
    localHeader.writeUInt32LE(compressedSize, 18)
    localHeader.writeUInt32LE(uncompressedSize, 22)
    localHeader.writeUInt16LE(nameBuf.length, 26)
    localHeader.writeUInt16LE(0, 28) // Extra field length
    nameBuf.copy(localHeader, 30)

    // Central Directory Header (46 bytes + name length)
    const centralHeader = Buffer.alloc(46 + nameBuf.length)
    centralHeader.writeUInt32LE(0x02014b50, 0)
    centralHeader.writeUInt16LE(0x0314, 4) // Version made by (UNIX 2.0)
    centralHeader.writeUInt16LE(20, 6) // Version needed (2.0)
    centralHeader.writeUInt16LE(0x0800, 8) // Bit 11: UTF-8
    centralHeader.writeUInt16LE(8, 10) // DEFLATE
    centralHeader.writeUInt16LE(dosTime, 12)
    centralHeader.writeUInt16LE(dosDate, 14)
    centralHeader.writeUInt32LE(crc, 16)
    centralHeader.writeUInt32LE(compressedSize, 20)
    centralHeader.writeUInt32LE(uncompressedSize, 24)
    centralHeader.writeUInt16LE(nameBuf.length, 28)
    centralHeader.writeUInt16LE(0, 30) // Extra field length
    centralHeader.writeUInt16LE(0, 32) // Comment length
    centralHeader.writeUInt16LE(0, 34) // Disk number start
    centralHeader.writeUInt16LE(0, 36) // Internal attributes
    centralHeader.writeUInt32LE((0o100644 << 16) >>> 0, 38) // External file attributes (regular file 0644)
    centralHeader.writeUInt32LE(offset, 42) // Relative offset of local header
    nameBuf.copy(centralHeader, 46)

    localHeaders.push(localHeader, compressedData)
    centralHeaders.push(centralHeader)

    offset += localHeader.length + compressedData.length
  }

  const centralDirOffset = offset
  const centralDirSize = centralHeaders.reduce((acc, h) => acc + h.length, 0)

  // End of Central Directory Record (22 bytes)
  const eocd = Buffer.alloc(22)
  eocd.writeUInt32LE(0x06054b50, 0)
  eocd.writeUInt16LE(0, 4) // Number of this disk
  eocd.writeUInt16LE(0, 6) // Disk with start of CD
  eocd.writeUInt16LE(files.length, 8) // Total entries on this disk
  eocd.writeUInt16LE(files.length, 10) // Total entries in CD
  eocd.writeUInt32LE(centralDirSize, 12)
  eocd.writeUInt32LE(centralDirOffset, 16)
  eocd.writeUInt16LE(0, 20) // ZIP comment length

  return Buffer.concat([...localHeaders, ...centralHeaders, eocd])
}

/**
 * Parses and verifies a PKZIP buffer, returning entry metadata.
 */
function parseZipEntries(zipBuffer) {
  let eocdOffset = -1
  for (let i = zipBuffer.length - 22; i >= Math.max(0, zipBuffer.length - 65557); i--) {
    if (zipBuffer.readUInt32LE(i) === 0x06054b50) {
      eocdOffset = i
      break
    }
  }
  if (eocdOffset === -1) {
    throw new Error('EOCD signature (0x06054b50) not found in ZIP archive.')
  }

  const totalEntries = zipBuffer.readUInt16LE(eocdOffset + 10)
  const cdOffset = zipBuffer.readUInt32LE(eocdOffset + 16)

  let pos = cdOffset
  const entries = []
  for (let i = 0; i < totalEntries; i++) {
    if (zipBuffer.readUInt32LE(pos) !== 0x02014b50) {
      throw new Error(`Invalid central directory header signature at byte ${pos}`)
    }
    const compression = zipBuffer.readUInt16LE(pos + 10)
    const crc32 = zipBuffer.readUInt32LE(pos + 16)
    const compressedSize = zipBuffer.readUInt32LE(pos + 20)
    const uncompressedSize = zipBuffer.readUInt32LE(pos + 24)
    const nameLen = zipBuffer.readUInt16LE(pos + 28)
    const extraLen = zipBuffer.readUInt16LE(pos + 30)
    const commentLen = zipBuffer.readUInt16LE(pos + 32)
    const localHeaderOffset = zipBuffer.readUInt32LE(pos + 42)

    const name = zipBuffer.toString('utf8', pos + 46, pos + 46 + nameLen)
    entries.push({
      name,
      compression,
      crc32,
      compressedSize,
      uncompressedSize,
      localHeaderOffset,
    })

    pos += 46 + nameLen + extraLen + commentLen
  }
  return entries
}

/**
 * Extracts a specific entry from a parsed PKZIP buffer.
 */
function extractZipEntry(zipBuffer, entry) {
  const localOffset = entry.localHeaderOffset
  if (zipBuffer.readUInt32LE(localOffset) !== 0x04034b50) {
    throw new Error(`Invalid local header signature at byte ${localOffset} for entry ${entry.name}`)
  }
  const nameLen = zipBuffer.readUInt16LE(localOffset + 26)
  const extraLen = zipBuffer.readUInt16LE(localOffset + 28)
  const dataStart = localOffset + 30 + nameLen + extraLen
  const compressedData = zipBuffer.subarray(dataStart, dataStart + entry.compressedSize)

  if (entry.compression === 0) {
    return compressedData
  } else if (entry.compression === 8) {
    return zlib.inflateRawSync(compressedData)
  } else {
    throw new Error(`Unsupported compression method ${entry.compression} in ${entry.name}`)
  }
}

// ─── VALIDATION LOGIC ────────────────────────────────────────────────────────

function validateZipArtifact(zipPath, platformName, expectedVersion, stagingFiles) {
  console.log(`\nValidating ${platformName} release package: ${path.basename(zipPath)}`)

  if (!fs.existsSync(zipPath)) {
    fail(`${platformName} ZIP artifact not found at ${zipPath}`)
  }

  const zipBuffer = fs.readFileSync(zipPath)
  if (zipBuffer.length === 0) {
    fail(`${platformName} ZIP artifact is completely empty (0 bytes).`)
  }

  let entries
  try {
    entries = parseZipEntries(zipBuffer)
  } catch (err) {
    fail(
      `Corrupted ${platformName} ZIP archive: ${err instanceof Error ? err.message : String(err)}`
    )
  }

  if (entries.length === 0) {
    fail(`${platformName} ZIP archive contains 0 files.`)
  }

  const zipFileNames = new Set(entries.map((e) => e.name))

  // 1. Forbidden files verification
  const FORBIDDEN_PATTERNS = [
    /node_modules/i,
    /\.git/i,
    /\.env/i,
    /\.ts$/i,
    /tsconfig/i,
    /vite\.config/i,
    /package\.json/i,
    /pnpm-lock/i,
    /yarn\.lock/i,
    /package-lock/i,
    /tests\//i,
    /\.test\./i,
    /\.spec\./i,
    /coverage/i,
    /\.DS_Store/i,
    /Thumbs\.db/i,
  ]

  for (const name of zipFileNames) {
    for (const pattern of FORBIDDEN_PATTERNS) {
      if (pattern.test(name)) {
        fail(`Forbidden development file detected inside ${platformName} ZIP: '${name}'`)
      }
    }
  }

  // 2. Staging parity verification
  const stagingSet = new Set(stagingFiles)
  if (stagingSet.size !== zipFileNames.size) {
    fail(
      `File count mismatch between staging (${stagingSet.size}) and ${platformName} ZIP (${zipFileNames.size}).`
    )
  }
  for (const staged of stagingSet) {
    if (!zipFileNames.has(staged)) {
      fail(`File staged at '${staged}' was not found in ${platformName} ZIP archive.`)
    }
  }

  // 3. Manifest existence and JSON validity
  const manifestEntry = entries.find((e) => e.name === 'manifest.json')
  if (!manifestEntry) {
    fail(`manifest.json missing from root of ${platformName} ZIP archive.`)
  }

  let manifest
  try {
    const rawManifest = extractZipEntry(zipBuffer, manifestEntry).toString('utf8')
    manifest = JSON.parse(rawManifest)
  } catch (err) {
    fail(
      `Failed to parse manifest.json in ${platformName} ZIP: ${err instanceof Error ? err.message : String(err)}`
    )
  }

  // 4. Manifest metadata verification
  if (manifest.manifest_version !== 3) {
    fail(
      `Invalid manifest_version in ${platformName} ZIP: expected 3, got ${manifest.manifest_version}`
    )
  }
  if (!manifest.name || typeof manifest.name !== 'string') {
    fail(`Missing extension name in ${platformName} manifest.`)
  }
  if (manifest.version !== expectedVersion) {
    fail(
      `Version mismatch in ${platformName} manifest: expected '${expectedVersion}', found '${manifest.version}'`
    )
  }

  // 5. Entry point and file reference validation
  const referencedFiles = new Set()

  // Background entry
  if (platformName === 'Chromium') {
    const sw = manifest.background?.service_worker
    if (!sw) {
      fail(`Missing background.service_worker in Chromium manifest.`)
    }
    referencedFiles.add(sw)
  } else {
    const scripts = manifest.background?.scripts
    if (!Array.isArray(scripts) || scripts.length === 0) {
      fail(`Missing or empty background.scripts in Firefox manifest.`)
    }
    for (const s of scripts) referencedFiles.add(s)
  }

  // Action default_popup
  if (manifest.action?.default_popup) {
    referencedFiles.add(manifest.action.default_popup)
  }

  // Side panel / sidebar
  if (manifest.side_panel?.default_path) {
    referencedFiles.add(manifest.side_panel.default_path)
  }
  if (manifest.sidebar_action?.default_panel) {
    referencedFiles.add(manifest.sidebar_action.default_panel)
  }

  // Content scripts
  if (Array.isArray(manifest.content_scripts)) {
    for (const cs of manifest.content_scripts) {
      if (Array.isArray(cs.js)) {
        for (const jsFile of cs.js) referencedFiles.add(jsFile)
      }
      if (Array.isArray(cs.css)) {
        for (const cssFile of cs.css) referencedFiles.add(cssFile)
      }
    }
  }

  // Icons
  if (manifest.icons && typeof manifest.icons === 'object') {
    for (const iconPath of Object.values(manifest.icons)) {
      referencedFiles.add(iconPath)
    }
  }

  // Web accessible resources
  if (Array.isArray(manifest.web_accessible_resources)) {
    for (const war of manifest.web_accessible_resources) {
      if (Array.isArray(war.resources)) {
        for (const r of war.resources) referencedFiles.add(r)
      }
    }
  }

  // Verify each referenced file exists in the ZIP
  for (const ref of referencedFiles) {
    if (!zipFileNames.has(ref)) {
      fail(
        `Manifest references file '${ref}', but it does NOT exist inside ${platformName} ZIP archive!`
      )
    }
  }

  console.log(`  ✓ Readability: OK (${formatBytes(zipBuffer.length)}, ${entries.length} files)`)
  console.log(`  ✓ Manifest: valid MV3 (v${manifest.version})`)
  console.log(`  ✓ All ${referencedFiles.size} referenced manifest files exist inside archive`)
  console.log(`  ✓ Staging parity: 100% match`)
  console.log(`  ✓ Forbidden files scan: clean (zero source/dev/node_modules leakage)`)

  return {
    size: zipBuffer.length,
    fileCount: entries.length,
    manifestVersion: manifest.version,
  }
}

// ─── DOCUMENTATION GENERATOR ────────────────────────────────────────────────

function generateReleaseReadme(version, chromiumZipName, firefoxZipName) {
  return `# IntelliCache Collector — Release v${version}

**Package Version**: \`v${version}\`  
**Distribution Type**: Standalone Pre-compiled Browser Extension Packages  
**Release Date**: ${new Date().toISOString().split('T')[0]}  
**Target Environments**: Chromium-based browsers & Mozilla Firefox  

---

## 1. Release Packages Included

This directory contains clean, standalone release packages ready for installation:

- **Chromium Package**: \`${chromiumZipName}\`  
  Supports Google Chrome, Brave, Microsoft Edge, Opera, Vivaldi, Arc, and Chromium.
- **Firefox Package**: \`${firefoxZipName}\`  
  Supports Mozilla Firefox, Firefox Developer Edition, LibreWolf, Floorp, and Waterfox.

---

## 2. What Is NOT Required on the Target Computer

These packages are fully self-contained and pre-compiled:
- ❌ **No Node.js** required
- ❌ **No pnpm, npm, or yarn** required
- ❌ **No Git** required
- ❌ **No TypeScript compiler** required
- ❌ **No Vite or bundlers** required
- ❌ **No source code or node_modules** required

---

## 3. Installation Guide

### For Chromium-based Browsers (Chrome, Brave, Edge, Opera, Vivaldi)
1. Download or copy \`${chromiumZipName}\` to your computer.
2. Extract the ZIP archive into a dedicated folder (e.g., \`IntelliCache-Chromium/\`).
3. Open your browser and navigate to the extension management page:
   - **Chrome**: \`chrome://extensions\`
   - **Brave**: \`brave://extensions\`
   - **Edge**: \`edge://extensions\`
4. Enable the **Developer mode** toggle in the top-right corner.  
   *(Note: Chromium requires Developer Mode to load unpacked local extensions. ZIP packaging does not bypass this browser security restriction).*
5. Click the **Load unpacked** button in the top-left toolbar.
6. Select the extracted folder containing \`manifest.json\`.
7. **Verification**: The IntelliCache Collector card will appear with status "Active". Click the puzzle icon in your browser toolbar and pin **IntelliCache Collector** for quick dashboard access.

---

### For Mozilla Firefox

#### Option A: Temporary Loading (Standard Firefox Release)
Standard Firefox release builds enforce add-on signing for permanent installations. For local evaluations:
1. Download or copy \`${firefoxZipName}\` to your computer.
2. Open Firefox and navigate to:  
   \`about:debugging#/runtime/this-firefox\`
3. Click the **Load Temporary Add-on...** button.
4. Select the \`${firefoxZipName}\` archive directly (or extract it and select \`manifest.json\`).
5. **Verification**: IntelliCache Collector will appear under **Temporary Extensions**.  
   *(Important note: Temporary add-ons remain active for the duration of the Firefox session until the browser is restarted. This is standard Mozilla policy for unsigned local packages).*

#### Option B: Persistent Installation (Firefox Developer Edition / Nightly)
If you require persistent installation across browser restarts without Mozilla store signing:
1. Open Firefox Developer Edition or Firefox Nightly.
2. Navigate to \`about:config\` and accept the prompt.
3. Search for: \`xpinstall.signatures.required\` and double-click to toggle it to \`false\`.
4. Navigate to \`about:addons\`, click the gear icon (top-right), and select **Install Add-on From File...**
5. Select \`${firefoxZipName}\` to install persistently.

---

## 4. How to Verify IntelliCache is Capturing Conversations

1. Click the **IntelliCache** emblem icon in your browser toolbar to open the Neo-Brutalist dashboard popup.
2. Confirm the status pill displays **ACTIVE** and the database shows **Connected**.
3. Open any supported AI platform in a tab:
   - **ChatGPT**: https://chatgpt.com
   - **Claude**: https://claude.ai
   - **Gemini**: https://gemini.google.com
4. Ask any prompt and receive a response.
5. Re-open the IntelliCache popup dashboard:
   - Observe the **Total Interactions** count increment.
   - The captured query and response will appear in the **Recent Activity** list and **Interaction Explorer**.
   - All data is stored 100% locally in your browser's private IndexedDB (\`intelliCache\`). Zero data leaves your computer.

---

## 5. Troubleshooting

- **Extension icon not visible in toolbar**:
  - Click the puzzle-piece (Extensions) icon in the browser toolbar and pin "IntelliCache Collector".
- **Interactions not appearing in dashboard**:
  - Ensure the chat page was opened or refreshed after installing the extension.
  - Make sure the response generation has finished (in-progress streaming turns are held until complete).
- **Service Worker status shows "Offline" / "Idle"**:
  - In Manifest V3, background service workers are ephemeral and sleep when idle to conserve battery and memory. It will automatically wake when an interaction is captured or when the popup dashboard opens.

---

## 6. How to Remove IntelliCache

- **Chromium**: Navigate to \`chrome://extensions\`, locate "IntelliCache Collector", click **Remove**, and confirm.
- **Firefox**: Navigate to \`about:addons\` (or \`about:debugging\`), locate "IntelliCache Collector", and click **Remove**.
- All local IndexedDB data associated with the extension origin is cleanly uninstalled by the browser.
`
}

// ─── MAIN RELEASE PIPELINE ──────────────────────────────────────────────────

async function runRelease() {
  const args = process.argv.slice(2)
  const skipTests = args.includes('--skip-tests')

  console.log('═════════════════════════════════════════════════════════════════')
  console.log('       IntelliCache Collector — Release Packaging Pipeline        ')
  console.log('═════════════════════════════════════════════════════════════════')

  // 1. Read authoritative version from package.json
  logStep(1, 8, 'Reading authoritative project version...')
  const packageJsonPath = path.join(rootDir, 'package.json')
  if (!fs.existsSync(packageJsonPath)) {
    fail(`package.json not found at ${packageJsonPath}`)
  }
  const pkg = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'))
  const version = pkg.version
  if (!version || typeof version !== 'string') {
    fail(`Invalid version '${version}' in package.json`)
  }
  console.log(`Authoritative Version: v${version}`)

  // 2. Run test suite
  if (!skipTests) {
    logStep(2, 8, 'Executing full regression test suite (pnpm test)...')
    runCommand('pnpm test')
    console.log('✓ All test suites passed cleanly.')
  } else {
    console.log('\n[2/8] Skipping test suite (--skip-tests flag provided).')
  }

  // 3. Build Chromium MV3 bundle
  logStep(3, 8, 'Building Chromium distribution (pnpm run build)...')
  runCommand('pnpm run build')
  const distDir = path.join(rootDir, 'dist')
  if (!fs.existsSync(distDir)) {
    fail(`dist/ directory was not generated by vite build.`)
  }
  console.log('✓ Chromium bundle compiled in dist/')

  // 4. Build Firefox Gecko MV3 bundle
  logStep(4, 8, 'Adapting Firefox distribution (node scripts/build-firefox.mjs)...')
  runCommand('node scripts/build-firefox.mjs')
  const distFirefoxDir = path.join(rootDir, 'dist-firefox')
  if (!fs.existsSync(distFirefoxDir)) {
    fail(`dist-firefox/ directory was not generated.`)
  }
  console.log('✓ Firefox bundle compiled in dist-firefox/')

  // 5. Prepare releases directory and clean staging directories
  logStep(5, 8, 'Preparing release directories and staging runtime files...')
  const releasesBaseDir = path.join(rootDir, 'releases')
  const releaseDir = path.join(releasesBaseDir, `v${version}`)
  const stagingChromiumDir = path.join(releaseDir, '.staging-chromium')
  const stagingFirefoxDir = path.join(releaseDir, '.staging-firefox')

  if (fs.existsSync(releaseDir)) {
    fs.rmSync(releaseDir, { recursive: true, force: true })
  }
  fs.mkdirSync(stagingChromiumDir, { recursive: true })
  fs.mkdirSync(stagingFirefoxDir, { recursive: true })

  // 6. Stage files (copy only runtime-required assets, excluding dev artifacts)
  function stageDistribution(srcDir, targetDir) {
    const rawFiles = recursivelyListFiles(srcDir)
    const stagedFiles = []

    for (const rel of rawFiles) {
      // Filter out source maps, hidden files, and any accidental source/config files
      if (
        rel.endsWith('.map') ||
        rel.endsWith('.ts') ||
        rel.endsWith('.DS_Store') ||
        rel.startsWith('.env')
      ) {
        continue
      }
      const srcFull = path.join(srcDir, rel)
      const targetFull = path.join(targetDir, rel)
      fs.mkdirSync(path.dirname(targetFull), { recursive: true })
      fs.copyFileSync(srcFull, targetFull)
      stagedFiles.push(rel)
    }
    return stagedFiles
  }

  const chromiumStagedFiles = stageDistribution(distDir, stagingChromiumDir)
  const firefoxStagedFiles = stageDistribution(distFirefoxDir, stagingFirefoxDir)

  console.log(`✓ Staged ${chromiumStagedFiles.length} runtime files for Chromium`)
  console.log(`✓ Staged ${firefoxStagedFiles.length} runtime files for Firefox`)

  // 7. Create ZIP Archives
  logStep(6, 8, 'Generating deterministic ZIP archives using Node standard library...')
  const chromiumZipName = `IntelliCache-v${version}-Chromium.zip`
  const firefoxZipName = `IntelliCache-v${version}-Firefox.zip`
  const chromiumZipPath = path.join(releaseDir, chromiumZipName)
  const firefoxZipPath = path.join(releaseDir, firefoxZipName)

  function buildZipFromStaging(stagingDir, stagedList, outputPath) {
    const filesToArchive = stagedList.map((rel) => ({
      name: rel,
      data: fs.readFileSync(path.join(stagingDir, rel)),
    }))
    const zipBuffer = createZipArchive(filesToArchive)
    fs.writeFileSync(outputPath, zipBuffer)
  }

  buildZipFromStaging(stagingChromiumDir, chromiumStagedFiles, chromiumZipPath)
  buildZipFromStaging(stagingFirefoxDir, firefoxStagedFiles, firefoxZipPath)

  console.log(`✓ Generated ${chromiumZipName} (${formatBytes(fs.statSync(chromiumZipPath).size)})`)
  console.log(`✓ Generated ${firefoxZipName} (${formatBytes(fs.statSync(firefoxZipPath).size)})`)

  // 8. Validate ZIP Artifacts
  logStep(7, 8, 'Performing deep inspection & validation on created ZIP archives...')
  const chromiumValidation = validateZipArtifact(
    chromiumZipPath,
    'Chromium',
    version,
    chromiumStagedFiles
  )
  const firefoxValidation = validateZipArtifact(
    firefoxZipPath,
    'Firefox',
    version,
    firefoxStagedFiles
  )

  // 9. Generate Release Documentation & Cleanup Staging
  logStep(8, 8, 'Generating release documentation and cleaning staging artifacts...')
  const readmeContent = generateReleaseReadme(version, chromiumZipName, firefoxZipName)
  const readmePath = path.join(releaseDir, 'README.md')
  fs.writeFileSync(readmePath, readmeContent, 'utf8')
  console.log(`✓ Generated release documentation: ${path.relative(rootDir, readmePath)}`)

  fs.rmSync(stagingChromiumDir, { recursive: true, force: true })
  fs.rmSync(stagingFirefoxDir, { recursive: true, force: true })
  console.log('✓ Staging directories cleaned.')

  // Final Summary Output
  console.log('\n═════════════════════════════════════════════════════════════════')
  console.log('               RELEASE ARTIFACTS READY FOR DISTRIBUTION          ')
  console.log('═════════════════════════════════════════════════════════════════')
  console.log(`Version:       v${version}`)
  console.log(`Release Dir:   ${path.relative(rootDir, releaseDir)}/`)
  console.log(
    `Chromium ZIP:  ${chromiumZipName} (${formatBytes(chromiumValidation.size)}, ${chromiumValidation.fileCount} files)`
  )
  console.log(
    `Firefox ZIP:   ${firefoxZipName} (${formatBytes(firefoxValidation.size)}, ${firefoxValidation.fileCount} files)`
  )
  console.log(`Documentation: README.md (${formatBytes(fs.statSync(readmePath).size)})`)
  console.log('Validation:    100% PASSED (All manifest references verified)')
  console.log('Target Req:    0 dependencies (No Node, pnpm, or source code needed)')
  console.log('═════════════════════════════════════════════════════════════════\n')
}

runRelease().catch((err) => {
  console.error('\nRelease pipeline encountered an unexpected fatal error:', err)
  process.exit(1)
})
