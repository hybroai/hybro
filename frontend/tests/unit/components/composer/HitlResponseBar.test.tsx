import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { HitlResponseBar, type HitlPromptView } from '@/components/composer/HitlResponseBar'
import { resetUnsentHitlDrafts } from '@/lib/hitl/interaction-controller'

const baseHitl: HitlPromptView = {
  hitlId: 'hitl-1',
  interactionId: 'interaction-1',
  source: 'agent',
  agentName: 'HITL Mock Agent',
  prompt: 'Human approval required.',
  promptType: 'approval',
  lifecycleState: 'open',
}

function renderBar(hitls: HitlPromptView[], onSubmit = vi.fn().mockResolvedValue(undefined)) {
  return render(
    <HitlResponseBar
      hitls={hitls}
      onSubmit={onSubmit}
      onCancel={vi.fn().mockResolvedValue(undefined)}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />,
  )
}

afterEach(() => {
  cleanup()
  resetUnsentHitlDrafts()
})

describe('HitlResponseBar', () => {

  it('uses one-question focus, preserves drafts by request id, and submits directly', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([
      { ...baseHitl, hitlId: 'hitl-1', prompt: 'Company name?', promptType: 'text', groupIndex: 0 },
      { ...baseHitl, hitlId: 'hitl-2', prompt: 'Renewal date?', promptType: 'date', groupIndex: 1 },
    ], onSubmit)

    const steps = screen.getByRole('tablist', { name: 'Questions' })
    expect(within(steps).getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Question 1', 'Question 2'])
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Renewal date?')).toBeDefined()

    fireEvent.change(document.querySelector('input[type="date"]') as HTMLInputElement, { target: { value: '2027-01-02' } })
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLInputElement).value).toBe('Acme')
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [
        { requestId: 'hitl-1', answer: 'Acme' },
        { requestId: 'hitl-2', answer: '2027-01-02' },
      ],
      undefined,
    ))
    expect(await screen.findByText('Applying your answers')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Check status' })).toBeNull()
  })

  it('keeps an unsent draft when the bar remounts, and submits it with Return', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const hitl = {
      ...baseHitl,
      interactionId: 'interaction-remount',
      prompt: 'Who signs the emails?',
      promptType: 'text' as const,
    }
    const first = renderBar([hitl], onSubmit)
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Morgan Reyes' } })
    // A reconnect or snapshot refresh changes the composer key and remounts.
    first.unmount()
    renderBar([hitl], onSubmit)
    const input = screen.getByPlaceholderText('Type your answer…') as HTMLInputElement
    expect(input.value).toBe('Morgan Reyes')

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-remount',
      [{ requestId: 'hitl-1', answer: 'Morgan Reyes' }],
      undefined,
    ))
    // A sent answer is not restored into a later card for the same interaction.
    cleanup()
    renderBar([hitl], onSubmit)
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLInputElement).value).toBe('')
  })

  it('cancels the exact interaction without submitting an unanswered questionnaire', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const onCancel = vi.fn().mockResolvedValue(undefined)
    render(
      <HitlResponseBar
        hitls={[
          { ...baseHitl, hitlId: 'reused-question', interactionId: 'interaction-2', prompt: 'Company name?', promptType: 'text', groupIndex: 0 },
          { ...baseHitl, hitlId: 'second-question', interactionId: 'interaction-2', prompt: 'Renewal date?', promptType: 'date', groupIndex: 1 },
        ]}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    )

    const reject = screen.getByRole('button', { name: 'Reject' })
    expect((reject as HTMLButtonElement).type).toBe('button')
    fireEvent.click(reject)
    expect(onCancel).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Reject request' }))

    await waitFor(() => expect(onCancel).toHaveBeenCalledWith(
      'reused-question',
      'interaction-2',
    ))
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getAllByText('Choose an answer to continue.')).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ hidden: true }),
      ]),
    )
  })

  it('submits a single question in one step without a review screen', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{ ...baseHitl, prompt: 'Where are you going?', promptType: 'text' }], onSubmit)

    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Review answers' })).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'New York City' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'New York City' }],
      undefined,
    ))
  })

  it('auto-refreshes while applying and autofocuses the follow-up open prompt', async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, prompt: 'Where are you going?', promptType: 'text' }]}
        onSubmit={onSubmit}
        onRefresh={onRefresh}
      />,
    )

    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'New York City' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(await screen.findByText('Applying your answers')).toBeDefined()
    await waitFor(() => expect(onRefresh).toHaveBeenCalled())

    rerender(
      <HitlResponseBar
        hitls={[{
          ...baseHitl,
          hitlId: 'hitl-2',
          interactionId: 'interaction-2',
          prompt: 'How many days/nights do you plan to spend in New York City?',
          promptType: 'text',
          lifecycleState: 'open',
        }]}
        onSubmit={onSubmit}
        onRefresh={onRefresh}
      />,
    )

    expect(await screen.findByText('How many days/nights do you plan to spend in New York City?')).toBeDefined()
    expect(screen.queryByText('Applying your answers')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check status' })).toBeNull()
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByPlaceholderText('Type your answer…'))
    })
  })

  it('autofocuses the next question control when advancing within an interaction', async () => {
    renderBar([
      { ...baseHitl, hitlId: 'hitl-1', prompt: 'Company name?', promptType: 'text', groupIndex: 0 },
      { ...baseHitl, hitlId: 'hitl-2', prompt: 'Budget?', promptType: 'text', groupIndex: 1 },
    ])

    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Budget?')).toBeDefined()
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByPlaceholderText('Type your answer…'))
    })
  })

  it('renders accessible single and multi-choice controls', () => {
    const { rerender } = render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'single_choice', choices: ['Use latest data', 'Use cached data'] }]}
        onSubmit={vi.fn()}
      />,
    )
    const latest = screen.getByRole('radio', { name: 'Use latest data' })
    fireEvent.click(latest)
    expect((latest as HTMLInputElement).checked).toBe(true)

    rerender(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'multi_choice', choices: ['Email', 'Phone'] }]}
        onSubmit={vi.fn()}
      />,
    )
    const email = screen.getByRole('checkbox', { name: 'Email' })
    fireEvent.click(email)
    expect((email as HTMLInputElement).checked).toBe(true)
  })

  it('registers textarea answers and submits directly', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{
      ...baseHitl,
      prompt: 'Add itinerary details',
      promptType: 'textarea',
    }], onSubmit)

    const textarea = screen.getByPlaceholderText('Add the details needed to continue…')
    fireEvent.change(textarea, { target: { value: 'Window seat and vegetarian meal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'Window seat and vegetarian meal' }],
      undefined,
    ))
  })

  it('never asks for authentication secrets in free text', () => {
    renderBar([{ ...baseHitl, prompt: 'Sign in to the carrier', promptType: 'authentication' }])

    expect(screen.getByText(/Never paste passwords/)).toBeDefined()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByRole('radio', { name: 'Authentication complete' })).toBeDefined()
  })

  it('renders generic prompts and delivery uncertainty as recovery states', () => {
    const { rerender } = renderBar([{
      ...baseHitl,
      prompt: 'The agent needs additional information.',
      promptType: 'text',
    }])
    expect(screen.getByText('This input request cannot be answered')).toBeDefined()
    expect(screen.queryByPlaceholderText('Type your answer…')).toBeNull()

    rerender(
      <HitlResponseBar
        hitls={[{ ...baseHitl, lifecycleState: 'delivery_uncertain' }]}
        onSubmit={vi.fn()}
        onRefresh={vi.fn().mockResolvedValue(undefined)}
      />,
    )
    expect(screen.getByText('Checking whether your answers were received')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Check status' })).toBeDefined()
  })
})


describe('HitlResponseBar structured questions', () => {

  const destination: HitlPromptView = {
    ...baseHitl,
    hitlId: 'destination',
    prompt: 'Where would you like to go?',
    promptType: 'single_choice',
    choices: ['Kyoto', 'Osaka'],
    groupIndex: 0,
    presentation: {
      header: 'Destination',
      required: true,
      allowOther: true,
      choiceOptions: [
        { value: 'Kyoto', description: 'Temples and food' },
        { value: 'Osaka', label: 'Osaka city' },
      ],
    },
  }
  const budget: HitlPromptView = {
    ...baseHitl,
    hitlId: 'budget',
    prompt: 'Any budget?',
    promptType: 'text',
    groupIndex: 1,
    presentation: { header: 'Budget', required: false, allowOther: false },
  }

  it('labels each question by its header and lets the owner jump between them', () => {
    renderBar([destination, budget])

    const steps = screen.getByRole('tablist', { name: 'Questions' })
    const [first, second] = within(steps).getAllByRole('tab')
    expect(first.textContent).toBe('Destination')
    expect(first.getAttribute('aria-selected')).toBe('true')

    fireEvent.click(second)
    expect(second.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('Any budget?')).toBeDefined()
  })

  it('shows choice labels and descriptions', () => {
    renderBar([destination])

    expect(screen.getByRole('radio', { name: /Kyoto/ })).toBeDefined()
    expect(screen.getByText('Temples and food')).toBeDefined()
    expect(screen.getByRole('radio', { name: /Osaka city/ })).toBeDefined()
  })

  it('submits a typed Other answer for a choice question', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([destination], onSubmit)

    fireEvent.click(screen.getByRole('radio', { name: /Other/ }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Other answer' }), { target: { value: 'Lisbon' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'destination', answer: 'Lisbon' }],
      undefined,
    ))
  })

  it('needs text before an Other answer counts', () => {
    renderBar([destination])
    fireEvent.click(screen.getByRole('radio', { name: /Other/ }))
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('combines picked choices and Other text for multi-choice questions', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{
      ...destination,
      promptType: 'multi_choice',
      choices: ['Food', 'Temples'],
      presentation: { header: 'Interests', required: true, allowOther: true },
    }], onSubmit)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Food' }))
    fireEvent.click(screen.getByRole('checkbox', { name: /Other/ }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Other answer' }), { target: { value: 'Hiking' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'destination', answer: 'Food, Hiking' }],
      undefined,
    ))
  })

  it('lets the owner skip an optional question and leaves it out of the answers', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{ ...budget, groupIndex: 0 }, { ...destination, groupIndex: 1 }], onSubmit)

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    fireEvent.click(screen.getByRole('radio', { name: /Kyoto/ }))
    expect(screen.queryByRole('button', { name: /Skip/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'destination', answer: 'Kyoto' }],
      undefined,
    ))
  })
})


describe('HitlResponseBar decisions', () => {

  it('names a check rule as the asker of a system approval', () => {
    renderBar([{
      ...baseHitl,
      source: 'system',
      agentName: undefined,
      prompt: 'Allow this call to Echo?',
      choices: ['Approve', 'Reject'],
    }])

    expect(screen.getByRole('heading', { name: 'Your check rule needs your approval' })).toBeDefined()
    expect(screen.queryByText(/HYBRO AI/)).toBeNull()
  })

  it('keeps HYBRO AI as the asker of a supervisor question', () => {
    renderBar([{ ...baseHitl, source: 'supervisor', agentName: undefined }])

    expect(screen.getByRole('heading', { name: 'HYBRO AI needs your approval' })).toBeDefined()
  })

  it('answers a single approval with one click on Approve', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const onCancel = vi.fn().mockResolvedValue(undefined)
    render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, prompt: 'Share floor-plan.pdf with the client?' }]}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    )

    expect(screen.getByRole('heading', { name: 'HITL Mock Agent needs your approval' })).toBeDefined()
    expect(screen.queryByRole('radio')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'Approve' }],
      undefined,
    ))
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('answers Reject as the decision and shows it in red', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{ ...baseHitl, prompt: 'Share floor-plan.pdf with the client?' }], onSubmit)

    const reject = screen.getByRole('button', { name: 'Reject' })
    expect(reject.className).toContain('bg-destructive')
    fireEvent.click(reject)

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'Reject' }],
      undefined,
    ))
  })

  it('offers Confirm and Decline for a yes-or-no question', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{ ...baseHitl, prompt: 'Book the hotel?', promptType: 'confirmation' }], onSubmit)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'Confirm' }],
      undefined,
    ))
    expect(screen.queryByRole('button', { name: 'Decline' })).toBeNull()
  })

  it('shows the cancel Reject in red for other questions and confirms first', async () => {
    const onCancel = vi.fn().mockResolvedValue(undefined)
    render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, prompt: 'Where?', promptType: 'text' }]}
        onSubmit={vi.fn()}
        onCancel={onCancel}
      />,
    )
    const reject = screen.getByRole('button', { name: 'Reject' })
    expect(reject.className).toContain('bg-destructive')
    fireEvent.click(reject)
    fireEvent.click(await screen.findByRole('button', { name: 'Reject request' }))
    await waitFor(() => expect(onCancel).toHaveBeenCalledWith('hitl-1', 'interaction-1'))
  })
})


describe('HitlResponseBar optional last question', () => {

  it('sends the earlier answers when the last optional question is skipped', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([
      { ...baseHitl, hitlId: 'name', prompt: 'Name?', promptType: 'text', groupIndex: 0 },
      {
        ...baseHitl,
        hitlId: 'notes',
        prompt: 'Notes?',
        promptType: 'text',
        groupIndex: 1,
        presentation: { required: false, allowOther: false },
      },
    ], onSubmit)

    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Acme' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip and send' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'name', answer: 'Acme' }],
      undefined,
    ))
  })
})


describe('HitlResponseBar server lifecycle reconciliation', () => {

  it('leaves applying when the authoritative lifecycle reports failed', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'text', lifecycleState: 'open' }]}
        onSubmit={onSubmit}
        onRefresh={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Ok' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(await screen.findByText('Applying your answers')).toBeDefined()

    rerender(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'text', lifecycleState: 'expired' }]}
        onSubmit={onSubmit}
        onRefresh={vi.fn().mockResolvedValue(undefined)}
      />,
    )

    expect(await screen.findByText('Input request expired')).toBeDefined()
    expect(screen.queryByText('Applying your answers')).toBeNull()
  })

  it('clears stale local recovery state when the server returns to open', async () => {
    const { rerender } = render(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'text', lifecycleState: 'routing_failed' }]}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByText('This input request cannot be answered')).toBeDefined()

    rerender(
      <HitlResponseBar
        hitls={[{ ...baseHitl, promptType: 'text', lifecycleState: 'open' }]}
        onSubmit={vi.fn()}
      />,
    )

    expect(await screen.findByPlaceholderText('Type your answer…')).toBeDefined()
  })

  it('advances to the next question when Enter is pressed on an intermediate answer', () => {
    renderBar([
      { ...baseHitl, hitlId: 'hitl-1', prompt: 'Company name?', promptType: 'text', groupIndex: 0 },
      { ...baseHitl, hitlId: 'hitl-2', prompt: 'Renewal date?', promptType: 'date', groupIndex: 1 },
    ])

    const input = screen.getByPlaceholderText('Type your answer…')
    fireEvent.change(input, { target: { value: 'Acme' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('Renewal date?')).toBeDefined()
  })

  it('submits from the last question when Enter is pressed', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    renderBar([{ ...baseHitl, prompt: 'Where are you going?', promptType: 'text' }], onSubmit)

    const input = screen.getByPlaceholderText('Type your answer…')
    fireEvent.change(input, { target: { value: 'Tokyo' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(
      'interaction-1',
      [{ requestId: 'hitl-1', answer: 'Tokyo' }],
      undefined,
    ))
  })
})
