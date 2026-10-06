import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { createCheckoutStrategy, type CheckoutStrategyDeps } from './checkout'

/**
 * Regression: the "no staged updater" branch must probe for the hand-off
 * script with the resolver that matches the target platform.
 *
 * `resolveUpdateScriptHandoff` only ever looks for
 * `scripts/desktop-update/windows.ps1` and returns null unconditionally off
 * Windows. Calling it in the shared branch made the guard permanently true
 * on macOS/Linux, so every checkout install was handed the manual
 * `hermes update` card — even when `scripts/desktop-update/posix.sh` was
 * present and the posix hand-off would have succeeded.
 *
 * Reported as 4 failing legs of one class in hermes-agent#80:
 *   macos: desktop-installer@latest -> open-app-update        (HEAD -> NEXT)
 *   macos: desktop-installer@latest -> open-app-update        (v2026.7.1 -> HEAD)
 *   macos: desktop-installer@latest -> hermes-desktop-app-update (v2026.7.1 -> HEAD)
 *   macos: desktop-installer@latest -> hermes-desktop-app-update (HEAD -> NEXT)
 * all failing at the update step with
 * `[updates] no staged updater; surfacing manual ...`.
 *
 * These tests inject the platform rather than reading `process.platform`, so
 * the macOS half of the pair is asserted on whatever runner CI happens to
 * use. The pre-existing checkout-legacy test derives `isWindows`/`isMac`
 * from the host, so it only ever exercised the Windows half.
 */
describe('checkout strategy — platform-correct hand-off probe', () => {
  let root: string
  let home: string

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-handoff-'))
    home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-home-'))
  })

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true })
    fs.rmSync(home, { recursive: true, force: true })
  })

  function depsFor(isWindows: boolean, isMac: boolean): CheckoutStrategyDeps {
    return {
      readSourceUpdate: vi.fn().mockResolvedValue({
        supported: true,
        updateAvailable: true,
        behind: null,
        branch: 'main',
        hermesRoot: root,
      }),
      hermesHome: home,
      isWindows,
      isMac,
      defaultUpdateBranch: 'main',
      updateHandoffDwellMs: 0,
      resolveUpdateRoot: (): string => root,
      // The condition under test: no staged updater binary, so the shared
      // "no staged updater" branch is taken.
      resolveUpdaterBinary: vi.fn((): null => null),
      remoteGatewayActive: (): boolean => false,
      emitUpdateProgress: vi.fn(),
      rememberLog: vi.fn(),
      startHermes: vi.fn(async (): Promise<void> => {}),
      stopBackendsForUpdate: vi.fn(async (): Promise<void> => {}),
      repairMacUpdaterHelper: vi.fn(),
      preflightStateDb: vi.fn(),
      runningAppBundle: (): null => null,
      markQuittingForHandoff: vi.fn(),
      quit: vi.fn(),
    }
  }

  it('macOS with posix.sh present does NOT land on the manual card', async () => {
    fs.mkdirSync(path.join(root, 'scripts', 'desktop-update'), { recursive: true })
    fs.writeFileSync(path.join(root, 'scripts', 'desktop-update', 'posix.sh'), '#!/bin/bash\n')

    const deps = depsFor(false, true)
    const strategy = createCheckoutStrategy(deps)

    const result = await strategy.apply()

    // Before the fix this returned { manual: true }. The posix hand-off must
    // be selected instead, so `manual` must be absent.
    expect(result.manual).toBeUndefined()
    const logged = deps.rememberLog.mock.calls.map((c) => String(c[0])).join('\n')
    expect(logged).not.toContain('surfacing manual')
  })

  it('macOS without posix.sh still falls back to the manual card', async () => {
    const deps = depsFor(false, true)
    const strategy = createCheckoutStrategy(deps)

    const result = await strategy.apply()

    expect(result).toMatchObject({ ok: true, manual: true, command: 'hermes update' })
  })

  it('Windows with windows.ps1 present does NOT land on the manual card', async () => {
    fs.mkdirSync(path.join(root, 'scripts', 'desktop-update'), { recursive: true })
    fs.writeFileSync(path.join(root, 'scripts', 'desktop-update', 'windows.ps1'), '# noop\n')

    const deps = depsFor(true, false)
    const strategy = createCheckoutStrategy(deps)

    const result = await strategy.apply()

    expect(result.manual).toBeUndefined()
    const logged = deps.rememberLog.mock.calls.map((c) => String(c[0])).join('\n')
    expect(logged).not.toContain('surfacing manual')
  })

  it('Windows without windows.ps1 still falls back to the manual card', async () => {
    const deps = depsFor(true, false)
    const strategy = createCheckoutStrategy(deps)

    const result = await strategy.apply()

    expect(result).toMatchObject({ ok: true, manual: true, command: 'hermes update' })
  })

  it('a posix.sh in the checkout does not rescue a Windows install', async () => {
    // Guards against "fix" that just probes both resolvers: on Windows the
    // posix script must stay inert, because the Windows hand-off is the
    // PowerShell script or the staged binary.
    fs.mkdirSync(path.join(root, 'scripts', 'desktop-update'), { recursive: true })
    fs.writeFileSync(path.join(root, 'scripts', 'desktop-update', 'posix.sh'), '#!/bin/bash\n')

    const deps = depsFor(true, false)
    const strategy = createCheckoutStrategy(deps)

    const result = await strategy.apply()

    expect(result).toMatchObject({ ok: true, manual: true, command: 'hermes update' })
  })
})