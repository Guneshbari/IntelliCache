import { describe, expect, it } from 'vitest'
import {
  cleanQueryText,
  cleanResponseText,
  deduplicateRepeatedPhrase,
} from '../src/shared/text-cleaner'

describe('Text Cleaner Utilities', () => {
  describe('cleanQueryText', () => {
    it('strips "You said:" prefix (case-insensitive) and collapses duplicate phrases', () => {
      const input = 'you said Make the video longer make the video longer'
      expect(cleanQueryText(input)).toBe('Make the video longer')
    })

    it('strips "You said:" with colon and whitespace', () => {
      const input = 'You said:  What is the capital of France?'
      expect(cleanQueryText(input)).toBe('What is the capital of France?')
    })

    it('strips "you said" without colon and newline', () => {
      const input = 'you said\nMake the video longer'
      expect(cleanQueryText(input)).toBe('Make the video longer')
    })

    it('strips "User:" or "Human:" prefixes', () => {
      expect(cleanQueryText('User: How does LRU work?')).toBe('How does LRU work?')
      expect(cleanQueryText('Human: Explain quantum computing')).toBe('Explain quantum computing')
    })

    it('deduplicates multi-line duplicate scrape artifacts', () => {
      const input = 'Make the video longer\nmake the video longer'
      expect(cleanQueryText(input)).toBe('Make the video longer')
    })

    it('deduplicates repeated phrases with terminal punctuation', () => {
      const input = 'Make the video longer. Make the video longer.'
      expect(cleanQueryText(input)).toBe('Make the video longer.')
    })

    it('deduplicates 3x repeated phrase', () => {
      const input = 'Make the video longer make the video longer make the video longer'
      expect(cleanQueryText(input)).toBe('Make the video longer')
    })

    it('preserves intentional single-word repetitions', () => {
      expect(cleanQueryText('no no no')).toBe('no no no')
      expect(cleanQueryText('bye bye')).toBe('bye bye')
      expect(cleanQueryText('hello hello')).toBe('hello hello')
      expect(cleanQueryText('very very interesting')).toBe('very very interesting')
    })

    it('leaves standard, non-duplicated prompts untouched', () => {
      const prompt = 'What is the difference between semantic cache and exact key cache?'
      expect(cleanQueryText(prompt)).toBe(prompt)
    })

    it('handles empty or non-string gracefully', () => {
      expect(cleanQueryText('')).toBe('')
      expect(cleanQueryText(null as unknown as string)).toBe('')
      expect(cleanQueryText(undefined as unknown as string)).toBe('')
    })
  })

  describe('cleanResponseText', () => {
    it('strips "ChatGPT said:" prefix', () => {
      const input = 'ChatGPT said: Here is the code you requested.'
      expect(cleanResponseText(input)).toBe('Here is the code you requested.')
    })

    it('strips "Claude said:" prefix', () => {
      const input = 'Claude said: I can help with that.'
      expect(cleanResponseText(input)).toBe('I can help with that.')
    })

    it('strips "Gemini said:" prefix', () => {
      const input = 'Gemini said: Sure thing!'
      expect(cleanResponseText(input)).toBe('Sure thing!')
    })

    it('strips "Assistant said:" prefix', () => {
      const input = 'Assistant said: Hello world'
      expect(cleanResponseText(input)).toBe('Hello world')
    })

    it('leaves clean assistant responses untouched', () => {
      const resp = 'Caching is a high-speed data storage layer.'
      expect(cleanResponseText(resp)).toBe(resp)
    })
  })

  describe('deduplicateRepeatedPhrase', () => {
    it('handles short strings safely', () => {
      expect(deduplicateRepeatedPhrase('')).toBe('')
      expect(deduplicateRepeatedPhrase('abc')).toBe('abc')
    })

    it('only deduplicates multi-word or long phrases', () => {
      expect(deduplicateRepeatedPhrase('foo bar foo bar')).toBe('foo bar')
      expect(deduplicateRepeatedPhrase('test test')).toBe('test test')
    })
  })
})
