import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'

import { DirectiveContent } from './directive-text'
import { MarkdownTextContent } from './markdown-text'

const openSessionTile = vi.fn()
const ensureGatewayProfile = vi.fn(async (_profile?: string) => {})

vi.mock('@/store/session-states', () => ({
  openSessionTile: (...args: unknown[]) => openSessionTile(...args)
}))

vi.mock('@/store/profile', () => ({
  ensureGatewayProfile: (profile?: string) => ensureGatewayProfile(profile)
}))

afterEach(() => {
  cleanup()
  openSessionTile.mockClear()
  ensureGatewayProfile.mockReset()
  ensureGatewayProfile.mockResolvedValue(undefined)
  __resetSessionLinkTitleCache()
})

// Both surfaces render a session ref differently — an inline link in agent
// prose, a chip in the user's own message — but either one opens the session
// it names, the way its sidebar row would.
describe('session refs open the session', () => {
  it('opens the session from an agent-written link', async () => {
    let finishProfileSwitch!: () => void

    ensureGatewayProfile.mockImplementationOnce(
      () => new Promise<void>(resolve => (finishProfileSwitch = resolve))
    )
    render(<MarkdownTextContent isRunning={false} text="Picked up in @session:work/20260101_abc123 last night." />)

    fireEvent.click(await screen.findByTitle('work/20260101_abc123'))

    await vi.waitFor(() => expect(ensureGatewayProfile).toHaveBeenCalledWith('work'))
    expect(openSessionTile).not.toHaveBeenCalled()

    finishProfileSwitch()

    await vi.waitFor(() => expect(openSessionTile).toHaveBeenCalledWith('20260101_abc123', 'center'))
  })

  it('opens the session from a chip in the user transcript', async () => {
    render(<DirectiveContent text="pick up @session:work/20260101_abc123 please" />)

    const chip = screen.getByTitle('work/20260101_abc123')

    expect(chip.tagName).toBe('BUTTON')
    fireEvent.click(chip)

    await vi.waitFor(() => expect(ensureGatewayProfile).toHaveBeenCalledWith('work'))
    await vi.waitFor(() => expect(openSessionTile).toHaveBeenCalledWith('20260101_abc123', 'center'))
  })

  it('waits for an in-flight profile switch before opening a profileless ref', async () => {
    let finishProfileSwitch!: () => void

    ensureGatewayProfile.mockImplementationOnce(
      () => new Promise<void>(resolve => (finishProfileSwitch = resolve))
    )
    render(<MarkdownTextContent isRunning={false} text="Continue @session:20260101_abc123 here." />)

    fireEvent.click(await screen.findByTitle('20260101_abc123'))

    await vi.waitFor(() => expect(ensureGatewayProfile).toHaveBeenCalledWith(undefined))
    expect(openSessionTile).not.toHaveBeenCalled()

    finishProfileSwitch()

    await vi.waitFor(() => expect(openSessionTile).toHaveBeenCalledWith('20260101_abc123', 'center'))
  })
})
