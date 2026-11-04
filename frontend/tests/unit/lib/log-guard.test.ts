import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'fs'
import type { Dirent } from 'fs'
import { join, relative } from 'path'

// Event names are stable snake_case identifiers, never interpolated text.
const EVENT_PATTERN = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/
const LOG_CALL = /\blog\.(?:debug|info|warn|error)\(\s*/g
const STRING_LITERAL = /^'([^'\\\n]*)'/
// The guard only sees calls spelled `log.<level>(`; renaming the import hides them.
const LOG_MODULE = String.raw`['"](?:@/lib/log|(?:\.{1,2}/)+(?:lib/)?log)['"]`
const ALIASED_IMPORT = new RegExp(
  String.raw`import\s*(?:\{[^}]*\blog\s+as\s+\w+[^}]*\}|\*\s*as\s+\w+)\s*from\s*` + LOG_MODULE,
)

function sourceFiles(): string[] {
  const files: string[] = []
  const walk = (current: string): void => {
    for (const entry of readdirSync(current, { withFileTypes: true }) as Dirent[]) {
      const full = join(current, entry.name)
      if (entry.isDirectory()) {
        walk(full)
      } else if (/\.(ts|tsx)$/.test(entry.name)) {
        files.push(full)
      }
    }
  }
  walk(join(process.cwd(), 'src'))
  return files
}

describe('log call guard', () => {
  it('passes a snake_case string literal as the event of every log call', () => {
    const offenders: string[] = []
    let calls = 0
    for (const file of sourceFiles()) {
      const text = readFileSync(file, 'utf8')
      for (const match of text.matchAll(LOG_CALL)) {
        calls += 1
        const rest = text.slice((match.index ?? 0) + match[0].length)
        const literal = STRING_LITERAL.exec(rest)
        if (!literal || !EVENT_PATTERN.test(literal[1])) {
          const line = text.slice(0, match.index).split('\n').length
          offenders.push(`${relative(process.cwd(), file)}:${line}: ${rest.slice(0, 40)}`)
        }
      }
    }
    // The application logs through src/lib/log.ts; zero calls means the scan is broken.
    expect(calls).toBeGreaterThan(0)
    expect(offenders).toEqual([])
  })

  it('rejects importing the logger under another name', () => {
    expect(ALIASED_IMPORT.test("import { log as logger } from '@/lib/log'")).toBe(true)
    expect(ALIASED_IMPORT.test("import * as logging from '../log'")).toBe(true)
    expect(ALIASED_IMPORT.test("import { log, requestHeaders } from '../log'")).toBe(false)

    const offenders = sourceFiles()
      .filter((file) => ALIASED_IMPORT.test(readFileSync(file, 'utf8')))
      .map((file) => relative(process.cwd(), file))
    expect(offenders).toEqual([])
  })
})
