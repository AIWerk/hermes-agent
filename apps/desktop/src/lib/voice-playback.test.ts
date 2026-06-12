import { beforeEach, describe, expect, it, vi } from 'vitest'

import { playSpeechText } from './voice-playback'

vi.mock('@/hermes', () => ({
  speakText: vi.fn(async () => ({ data_url: 'data:audio/mpeg;base64,dummy' }))
}))

interface ListenerEntry {
  listener: EventListenerOrEventListenerObject
  once?: boolean
}

class MockAudio {
  static instances: MockAudio[] = []

  readonly listeners = new Map<string, ListenerEntry[]>()
  readonly src: string
  currentTime = 0
  paused = true
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
}

describe('playSpeechText', () => {
  beforeEach(() => {
    MockAudio.instances = []
    vi.stubGlobal('Audio', MockAudio)
  })

  it('waits until the audio can play before starting playback', async () => {
    const playback = playSpeechText('Hello world', { source: 'voice-conversation' })

    await vi.waitFor(() => expect(MockAudio.instances).toHaveLength(1))

    const audio = MockAudio.instances[0]
    expect(audio.play).not.toHaveBeenCalled()

    audio.emit('loadeddata')
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1))

    audio.emit('ended')
    await expect(playback).resolves.toBe(true)
  })
})
