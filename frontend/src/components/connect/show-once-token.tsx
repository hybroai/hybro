'use client'

import { TriangleAlert } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { useCopy } from './use-copy'

export interface TokenSnippet {
  id: string
  label: string
  text: string
}

/**
 * A new source token, shown once with ready-to-paste snippets that contain it.
 * The token lives only in the caller's memory: it is never stored in the
 * browser, put in a URL or logged, and closing the dialog drops it.
 */
export function ShowOnceToken({ token, snippets }: { token: string; snippets: TokenSnippet[] }) {
  const copy = useCopy()
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Alert>
        <TriangleAlert aria-hidden="true" />
        <AlertTitle>Copy the token now</AlertTitle>
        <AlertDescription>
          It is shown only this once. Hybro keeps a hash and the last four characters, so it cannot show it again; rotate the token if you lose it.
        </AlertDescription>
      </Alert>
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-xs font-medium text-muted-foreground">Source token</p>
        {copy.block('token', 'Source token', token)}
        <div className="flex flex-wrap items-center gap-2">
          {copy.button('token', token, 'Copy token', true)}
          {copy.state('token', 'Token')}
        </div>
      </div>
      {snippets.map(snippet => (
        <div key={snippet.id} className="flex min-w-0 flex-col gap-2">
          <p className="text-xs font-medium text-muted-foreground">{snippet.label}</p>
          {copy.block(snippet.id, snippet.label, snippet.text)}
          <div className="flex flex-wrap items-center gap-2">
            {copy.button(snippet.id, snippet.text, `Copy ${snippet.label.toLowerCase()}`)}
            {copy.state(snippet.id, snippet.label)}
          </div>
        </div>
      ))}
    </div>
  )
}
