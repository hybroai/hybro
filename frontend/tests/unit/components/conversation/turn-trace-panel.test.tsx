import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '../../../utils/test-utils'
import { TurnTracePanel } from '@/components/conversation/TurnTracePanel'

afterEach(cleanup)

describe('TurnTracePanel', () => {
it('shows a preparing marker while a Turn runs without progress logs', () => {
  render(<TurnTracePanel statusEntries={[]} isRunning />)

  expect(screen.getByRole('button', { name: /running,/i })).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText('Preparing a response')).toBeVisible()
  expect(screen.getByRole('log')).toBeInTheDocument()
})

it('treats the optimistic Thinking placeholder as no progress', () => {
  render(
    <TurnTracePanel
      statusEntries={[{ id: 'log-0', message: 'Thinking...', timestamp: '2030-01-01T00:00:00.000Z' }]}
      isRunning
    />,
  )

  expect(screen.getByText('Preparing a response')).toBeVisible()
  expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /work logs/i })).not.toBeInTheDocument()
})

it('drops the Thinking placeholder when it sits next to real progress', () => {
  render(
    <TurnTracePanel
      statusEntries={[
        { id: 'log-0', message: 'Thinking...', timestamp: '2030-01-01T00:00:00.000Z' },
        { id: 'log-1', message: 'Applying your answers…', timestamp: '2030-01-01T00:00:01.000Z' },
      ]}
      isRunning
    />,
  )

  expect(screen.getByRole('button', { name: 'Work Logs, 1 update' })).toBeInTheDocument()
  expect(screen.getByText('Applying your answers…')).toBeVisible()
  expect(screen.queryByText('Thinking...')).not.toBeInTheDocument()
})

it('renders a finished header without activity once the Turn settles', () => {
  render(
    <TurnTracePanel statusEntries={[]} startedAt="1970-01-01T00:00:00.000Z" />,
  )

  fireEvent.click(screen.getByRole('button', { name: 'Finished, 0.0s' }))
  expect(screen.queryByText('Preparing a response')).not.toBeInTheDocument()
  expect(screen.queryByRole('log')).not.toBeInTheDocument()
})

it('keeps progress logs available as the turn settles', () => {
  const statusEntries = [{
    id: 'log-1',
    message: 'Collecting source material',
    timestamp: '2030-01-01T00:00:00.000Z',
  }]
  const { rerender } = render(
    <TurnTracePanel statusEntries={statusEntries} isRunning />,
  )
  const workLogs = screen.getByRole('button', { name: /work logs/i })
  expect(screen.getByText('Collecting source material')).toBeVisible()
  expect(screen.queryByText('Preparing a response')).not.toBeInTheDocument()
  fireEvent.click(workLogs)
  expect(workLogs).toHaveAttribute('aria-expanded', 'true')

  rerender(<TurnTracePanel statusEntries={statusEntries} />)
  expect(screen.getByText('Collecting source material')).toBeVisible()
  expect(screen.getByRole('button', { name: /work logs/i })).toHaveAttribute('aria-expanded', 'true')
  expect(screen.queryByRole('button', { name: /finished,/i })).not.toBeInTheDocument()
})
})
