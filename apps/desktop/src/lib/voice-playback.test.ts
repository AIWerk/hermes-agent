import { beforeEach, describe, expect, it, vi } from 'vitest'

import { playSpeechText, stopVoicePlayback } from './voice-playback'

const directMocks = vi.hoisted(() => ({
  cutSentences: vi.fn(),
  directTtsConfig: vi.fn(),
  synthesizeSpeechClientDirect: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getApiRequestConnection: vi.fn(() => null),
  getApiRequestProfile: vi.fn(() => null),
  speakText: vi.fn(async () => ({ data_url: 'data:audio/mpeg;base64,dummy' }))
}))

vi.mock('@/lib/voice-client-direct', () => ({
  cutSentences: directMocks.cutSentences,
  directTtsConfig: directMocks.directTtsConfig,
  synthesizeSpeechClientDirect: directMocks.synthesizeSpeechClientDirect
}))

interface ListenerEntry {
  listener: EventListenerOrEventListenerObject
  once?: boolean
}

class MockAudio {
  static instances: MockAudio[] = []

  readonly listeners = new Map<string, ListenerEntry[]>()
  src: string
  currentTime = 0
  paused = true
  readyState = 0
  pause = vi.fn(() => {
    this.paused = true
  })
  play = vi.fn(async () => {
    this.paused = false
  })
  load = vi.fn()

  constructor(src: string) {
    this.src = src
    MockAudio.instances.push(this)
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions) {
    const entries = this.listeners.get(type) ?? []
    entries.push({ listener, once: options?.once })
    this.listeners.set(type, entries)
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const entries = this.listeners.get(type) ?? []
    this.listeners.set(
      type,
      entries.filter(entry => entry.listener !== listener)
    )
  }

  emit(type: string) {
    const event = new Event(type)
    const entries = [...(this.listeners.get(type) ?? [])]

    for (const entry of entries) {
      if (typeof entry.listener === 'function') {
        entry.listener(event)
      } else {
        entry.listener.handleEvent(event)
      }

      if (entry.once) {
        this.removeEventListener(type, entry.listener)
      }
    }
  }

  listenerCount(type: string) {
    return this.listeners.get(type)?.length ?? 0
  }
}

const readinessEvents = ['loadeddata', 'canplay', 'canplaythrough'] as const

const directConfig = {
  api_key: 'test-key',
  base_url: 'https://tts.invalid',
  mode: 'direct' as const,
  model: null,
  provider: 'test',
  speed: null,
  voice: null,
  wire: 'openai-speech' as const
}

async function waitForAudio(index = 0) {
  await vi.waitFor(() => expect(MockAudio.instances.length).toBeGreaterThan(index))

  return MockAudio.instances[index]
}

function expectReadinessListenersRemoved(audio: MockAudio, remainingPlaybackErrorListeners = 0) {
  for (const event of readinessEvents) {
    expect(audio.listenerCount(event)).toBe(0)
  }

  expect(audio.listenerCount('error')).toBe(remainingPlaybackErrorListeners)
}

describe('playSpeechText', () => {
  beforeEach(() => {
    stopVoicePlayback()
    MockAudio.instances = []
    vi.clearAllMocks()
    vi.stubGlobal('Audio', MockAudio)
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    directMocks.directTtsConfig.mockResolvedValue(null)
    directMocks.cutSentences.mockImplementation((text: string) => ({ rest: '', sentences: text ? [text] : [] }))
    directMocks.synthesizeSpeechClientDirect.mockResolvedValue(new ArrayBuffer(1))
  })

  it.each(readinessEvents)(
    'waits for %s before starting playback',
    async (readinessEvent: (typeof readinessEvents)[number]) => {
      const playback = playSpeechText('Hello world', { source: 'voice-conversation' })
      const audio = await waitForAudio()

      expect(audio.play).not.toHaveBeenCalled()
      expect(audio.load).toHaveBeenCalledTimes(1)

      audio.emit(readinessEvent)
      await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1))
      expectReadinessListenersRemoved(audio, 1)

      audio.emit('ended')
      await expect(playback).resolves.toBe(true)
      expectReadinessListenersRemoved(audio)
    }
  )

  it('removes readiness listeners and rejects when the media errors', async () => {
    const playback = playSpeechText('Broken audio', { source: 'voice-conversation' })
    const audio = await waitForAudio()

    audio.emit('error')

    await expect(playback).rejects.toThrow('Playback failed')
    expect(audio.play).not.toHaveBeenCalled()
    expectReadinessListenersRemoved(audio)
  })

  it('removes readiness listeners and never starts a cancelled sequence', async () => {
    const playback = playSpeechText('Cancelled audio', { source: 'voice-conversation' })
    const audio = await waitForAudio()

    stopVoicePlayback()
    audio.emit('loadeddata')

    await expect(playback).resolves.toBe(false)
    expect(audio.play).not.toHaveBeenCalled()
    expectReadinessListenersRemoved(audio)
  })

  it('does not let a superseded readiness completion start or cancel the new sequence', async () => {
    const firstPlayback = playSpeechText('First', { source: 'voice-conversation' })
    const firstAudio = await waitForAudio()
    const secondPlayback = playSpeechText('Second', { source: 'voice-conversation' })
    const secondAudio = await waitForAudio(1)

    firstAudio.emit('canplay')
    expect(firstAudio.play).not.toHaveBeenCalled()
    await expect(firstPlayback).resolves.toBe(false)

    stopVoicePlayback()
    secondAudio.emit('canplaythrough')

    await expect(secondPlayback).resolves.toBe(false)
    expect(secondAudio.play).not.toHaveBeenCalled()
    expectReadinessListenersRemoved(secondAudio)
  })

  it('waits for readiness on client-direct audio before playback', async () => {
    directMocks.directTtsConfig.mockResolvedValueOnce(directConfig)

    const playback = playSpeechText('Direct audio', { source: 'voice-conversation' })
    const audio = await waitForAudio()

    expect(audio.play).not.toHaveBeenCalled()
    audio.emit('canplay')
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1))

    audio.emit('ended')
    await expect(playback).resolves.toBe(true)
  })

  it('keeps client-direct cancellation active after readiness while audio is playing', async () => {
    directMocks.directTtsConfig.mockResolvedValueOnce(directConfig)

    const playback = playSpeechText('Stop direct audio', { source: 'voice-conversation' })
    const audio = await waitForAudio()

    audio.emit('canplay')
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1))

    stopVoicePlayback()

    expect(audio.pause).toHaveBeenCalledTimes(1)
    await expect(playback).resolves.toBe(false)
    expectReadinessListenersRemoved(audio, 1)
  })

  it('does not start superseded client-direct audio after readiness', async () => {
    directMocks.directTtsConfig.mockResolvedValue(directConfig)

    const firstPlayback = playSpeechText('First direct', { source: 'voice-conversation' })
    const firstAudio = await waitForAudio()
    const secondPlayback = playSpeechText('Second direct', { source: 'voice-conversation' })
    const secondAudio = await waitForAudio(1)

    firstAudio.emit('loadeddata')
    await expect(firstPlayback).resolves.toBe(false)
    expect(firstAudio.play).not.toHaveBeenCalled()

    stopVoicePlayback()
    secondAudio.emit('canplaythrough')
    await expect(secondPlayback).resolves.toBe(false)
    expect(secondAudio.play).not.toHaveBeenCalled()
  })
})
