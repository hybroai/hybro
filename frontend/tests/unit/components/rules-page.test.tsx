import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { focusManager } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { cleanup, render, screen, waitFor, within } from '../../utils/test-utils'
import { server } from '../../setup/msw-server'
import { stubFetchWithoutSignal } from '../../utils/fetch-without-signal'
import { getApiUrl } from '@/lib/utils'
import { placeholderAgent } from '@/lib/network-agent'
import type { RuleView } from '@/lib/api/rules'

const agents = {
  list: [
    { ...placeholderAgent('writer', '', 'active'), name: 'Writer' },
    { ...placeholderAgent('reviewer', '', 'inactive'), name: 'Reviewer' },
  ],
  loading: false,
  error: null as string | null,
}
vi.mock('@/hooks/useNetworks', () => ({
  useNetworks: () => ({ directory: agents.list, agentsLoading: agents.loading, agentsError: agents.error }),
}))

import { RulesPage } from '@/components/rules/rules-page'

const at = '2026-11-20T10:00:00Z'
const rule = (rule_id: string, agent_id: string | null, answer: RuleView['answer'], enabled = true): RuleView => ({
  rule_id, kind: 'check', agent_id, answer, enabled, created_at: at, updated_at: at,
})
const DENY_WRITER = rule('rule_1', 'writer', 'deny')
const ASK_ANY = rule('rule_2', null, 'ask', false)
const ALLOW_GONE = rule('rule_3', 'agent_gone', 'allow')

const state = {
  rules: [] as RuleView[],
  gets: 0,
  writes: [] as string[],
}

const order = (ids: string[]) => ids.map(id => state.rules.find(item => item.rule_id === id)!)

beforeEach(() => {
  state.rules = [DENY_WRITER, ASK_ANY, ALLOW_GONE]
  state.gets = 0
  state.writes = []
  agents.list = [
    { ...placeholderAgent('writer', '', 'active'), name: 'Writer' },
    { ...placeholderAgent('reviewer', '', 'inactive'), name: 'Reviewer' },
  ]
  agents.loading = false
  agents.error = null
  stubFetchWithoutSignal()
  server.use(
    http.get(getApiUrl('rules'), () => {
      state.gets += 1
      return HttpResponse.json({ rules: state.rules })
    }),
    http.post(getApiUrl('rules'), async ({ request }) => {
      const body = await request.json() as Pick<RuleView, 'agent_id' | 'answer' | 'enabled'>
      state.writes.push(`POST ${JSON.stringify(body)}`)
      const created = rule('rule_new', body.agent_id, body.answer, body.enabled)
      state.rules = [...state.rules, created]
      return HttpResponse.json(created, { status: 201 })
    }),
    http.put(getApiUrl('rules/order'), async ({ request }) => {
      const { rule_ids } = await request.json() as { rule_ids: string[] }
      state.writes.push(`ORDER ${rule_ids.join(',')}`)
      state.rules = order(rule_ids)
      return HttpResponse.json({ rules: state.rules })
    }),
    http.put(getApiUrl('rules/:id'), async ({ request, params }) => {
      const body = await request.json() as Pick<RuleView, 'agent_id' | 'answer' | 'enabled'>
      state.writes.push(`PUT ${String(params.id)} ${JSON.stringify(body)}`)
      state.rules = state.rules.map(item => item.rule_id === params.id ? { ...item, ...body } : item)
      return HttpResponse.json(state.rules.find(item => item.rule_id === params.id))
    }),
    http.delete(getApiUrl('rules/:id'), ({ params }) => {
      state.writes.push(`DELETE ${String(params.id)}`)
      state.rules = state.rules.filter(item => item.rule_id !== params.id)
      return new HttpResponse(null, { status: 204 })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const list = () => screen.findByRole('list', { name: 'Check rules' })
const items = async () => within(await list()).getAllByRole('listitem')

describe('Rules page', () => {
  it('lists the rules in order with agent, answer and state', async () => {
    render(<RulesPage />)
    expect(screen.getByRole('heading', { level: 1, name: 'Rules' })).toBeInTheDocument()
    const rows = await items()
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByRole('combobox', { name: 'Agent for rule 1' })).toHaveValue('writer')
    expect(within(rows[0]).getByRole('combobox', { name: 'Answer for rule 1' })).toHaveValue('deny')
    expect(within(rows[0]).getByRole('switch', { name: 'Rule 1 on' })).toBeChecked()
    expect(within(rows[1]).getByRole('combobox', { name: 'Agent for rule 2' })).toHaveValue('')
    expect(within(rows[1]).getByRole('option', { name: 'Any agent', selected: true })).toBeInTheDocument()
    expect(within(rows[1]).getByRole('combobox', { name: 'Answer for rule 2' })).toHaveValue('ask')
    expect(within(rows[1]).getByRole('switch', { name: 'Rule 2 on' })).not.toBeChecked()
  })

  it('says which calls the rules cover and how they are read', () => {
    render(<RulesPage />)
    const help = screen.getByRole('region', { name: 'How rules work' })
    expect(help).toHaveTextContent('The first rule that is on and matches the agent decides')
    expect(help).toHaveTextContent('If no rule matches, the call is allowed.')
    expect(help).toHaveTextContent('in Room chat and through the Client API alike')
    expect(help).toHaveTextContent('are not checked again')
    expect(help).toHaveTextContent('Ask holds the call until you approve or reject it')
  })

  it('says every call is allowed when there are no rules', async () => {
    state.rules = []
    render(<RulesPage />)
    expect(await screen.findByText('No rules yet, so every agent call is allowed.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Check rules' })).not.toBeInTheDocument()
  })

  it('shows a rule whose agent is gone, and lets the owner pick another', async () => {
    render(<RulesPage />)
    const row = (await items())[2]
    expect(within(row).getByText(/This agent is no longer in your agents/)).toBeInTheDocument()
    const select = within(row).getByRole('combobox', { name: 'Agent for rule 3' })
    expect(select).toHaveValue('agent_gone')
    expect(within(select).getByRole('option', { name: 'Missing agent (agent_gone)' })).toBeInTheDocument()
    await userEvent.selectOptions(select, 'writer')
    await waitFor(() => expect(state.writes).toEqual(['PUT rule_3 {"agent_id":"writer","answer":"allow","enabled":true}']))
    await waitFor(() => expect(within(row).queryByText(/no longer in your agents/)).not.toBeInTheDocument())
  })

  it('does not call an agent missing while the agents could not load', async () => {
    agents.list = []
    agents.error = 'Unable to load agents.'
    render(<RulesPage />)
    const row = (await items())[0]
    expect(within(row).queryByText(/no longer in your agents/)).not.toBeInTheDocument()
    expect(screen.getByText(/Agents couldn.t load/)).toBeInTheDocument()
  })

  it('adds a rule for an agent with an answer', async () => {
    render(<RulesPage />)
    await items()
    await userEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a rule' })
    await userEvent.selectOptions(within(dialog).getByRole('combobox', { name: 'Agent' }), 'reviewer')
    await userEvent.selectOptions(within(dialog).getByRole('combobox', { name: 'Answer' }), 'ask')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(state.writes).toEqual(['POST {"agent_id":"reviewer","answer":"ask","enabled":true}'])
    await waitFor(async () => expect(await items()).toHaveLength(4))
  })

  it('adds a rule for any agent', async () => {
    render(<RulesPage />)
    await items()
    await userEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a rule' })
    expect(within(dialog).getByRole('combobox', { name: 'Agent' })).toHaveValue('')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(state.writes).toEqual(['POST {"agent_id":null,"answer":"deny","enabled":true}']))
  })

  it('changes a rule\'s answer, agent and state with the whole rule', async () => {
    render(<RulesPage />)
    const rows = await items()
    await userEvent.selectOptions(within(rows[0]).getByRole('combobox', { name: 'Answer for rule 1' }), 'ask')
    await waitFor(() => expect(state.writes).toHaveLength(1))
    await userEvent.click(within(rows[1]).getByRole('switch', { name: 'Rule 2 on' }))
    await waitFor(() => expect(state.writes).toHaveLength(2))
    await userEvent.selectOptions(within(rows[1]).getByRole('combobox', { name: 'Agent for rule 2' }), 'writer')
    await waitFor(() => expect(state.writes).toHaveLength(3))
    expect(state.writes).toEqual([
      'PUT rule_1 {"agent_id":"writer","answer":"ask","enabled":true}',
      'PUT rule_2 {"agent_id":null,"answer":"ask","enabled":true}',
      'PUT rule_2 {"agent_id":"writer","answer":"ask","enabled":true}',
    ])
  })

  it('deletes a rule only after confirmation', async () => {
    render(<RulesPage />)
    const rows = await items()
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Delete rule 1' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete rule 1?' })
    await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(state.writes).toEqual([])
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Delete rule 1' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(state.writes).toEqual(['DELETE rule_1']))
    await waitFor(async () => expect(await items()).toHaveLength(2))
  })

  it('reorders with the keyboard and keeps focus on the moved rule', async () => {
    const user = userEvent.setup()
    render(<RulesPage />)
    const rows = await items()
    const down = within(rows[0]).getByRole('button', { name: 'Move rule 1 down' })
    expect(within(rows[0]).getByRole('button', { name: 'Move rule 1 up' })).toHaveAttribute('aria-disabled', 'true')
    down.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(state.writes).toEqual(['ORDER rule_2,rule_1,rule_3']))
    await waitFor(async () => {
      const moved = (await items())[1]
      expect(within(moved).getByRole('combobox', { name: 'Agent for rule 2' })).toHaveValue('writer')
      expect(within(moved).getByRole('button', { name: 'Move rule 2 down' })).toHaveFocus()
    })
    expect(screen.getByRole('status')).toHaveTextContent('Rule moved to position 2 of 3.')
    // The next press waits until the reload after the move has settled.
    await waitFor(async () => expect(within((await items())[1]).getByRole('button', { name: 'Move rule 2 down' })).toHaveAttribute('aria-disabled', 'false'))
    await user.keyboard('{Enter}')
    await waitFor(() => expect(state.writes).toEqual(['ORDER rule_2,rule_1,rule_3', 'ORDER rule_2,rule_3,rule_1']))
    // At the bottom, focus moves to the control that can still act.
    await waitFor(async () => expect(within((await items())[2]).getByRole('button', { name: 'Move rule 3 up' })).toHaveFocus())
  })

  it('shows fixed text after a 404 and reloads the list', async () => {
    server.use(http.put(getApiUrl('rules/:id'), () => HttpResponse.json({ detail: { code: 'rule_not_found', message: 'x' } }, { status: 404 })))
    render(<RulesPage />)
    const rows = await items()
    const before = state.gets
    await userEvent.selectOptions(within(rows[0]).getByRole('combobox', { name: 'Answer for rule 1' }), 'allow')
    expect(await screen.findByText('This rule no longer exists. The list is being reloaded.')).toBeInTheDocument()
    await waitFor(() => expect(state.gets).toBeGreaterThan(before))
    // The select shows the stored answer again, not the refused one.
    await waitFor(async () => expect(within((await items())[0]).getByRole('combobox', { name: 'Answer for rule 1' })).toHaveValue('deny'))
  })

  it('shows fixed text after a 409 on reorder and reloads the list', async () => {
    server.use(http.put(getApiUrl('rules/order'), () => HttpResponse.json({ detail: { code: 'rule_order_mismatch', message: 'x' } }, { status: 409 })))
    render(<RulesPage />)
    const rows = await items()
    const before = state.gets
    await userEvent.click(within(rows[1]).getByRole('button', { name: 'Move rule 2 up' }))
    expect(await screen.findByText('The rules changed since this page loaded. The list is being reloaded; try again once it is.')).toBeInTheDocument()
    await waitFor(() => expect(state.gets).toBeGreaterThan(before))
  })

  it('shows the rule limit and a 422 with its fields in the add dialog', async () => {
    server.use(http.post(getApiUrl('rules'), () => HttpResponse.json({ detail: { code: 'rule_limit_reached', message: 'x' } }, { status: 409 })))
    render(<RulesPage />)
    await items()
    await userEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a rule' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    expect(await within(dialog).findByText('The list holds the most rules it can. Delete a rule to add another.')).toBeInTheDocument()
    server.use(http.post(getApiUrl('rules'), () => HttpResponse.json({ detail: { code: 'invalid_request', message: 'x', fields: ['agent_id'] } }, { status: 422 })))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    expect(await within(dialog).findByText('The rule was not saved. Check: agent_id.')).toBeInTheDocument()
  })

  it('shows a retryable error when the rules cannot load', async () => {
    server.use(http.get(getApiUrl('rules'), () => HttpResponse.json({ detail: 'x' }, { status: 500 })))
    render(<RulesPage />)
    expect(await screen.findByText("Rules couldn't load.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('closes the add dialog once a slow create succeeds, after one request', async () => {
    server.use(http.post(getApiUrl('rules'), async ({ request }) => {
      const body = await request.json() as Pick<RuleView, 'agent_id' | 'answer' | 'enabled'>
      state.writes.push(`POST ${JSON.stringify(body)}`)
      await delay(50)
      state.rules = [...state.rules, rule('rule_new', body.agent_id, body.answer, body.enabled)]
      return HttpResponse.json(state.rules.at(-1), { status: 201 })
    }))
    render(<RulesPage />)
    await items()
    await userEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a rule' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(state.writes).toHaveLength(1)
    await waitFor(async () => expect(await items()).toHaveLength(4))
  })

  it('sends overlapping edits one at a time, each with the edits before it', async () => {
    const events: string[] = []
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    let calls = 0
    server.use(http.put(getApiUrl('rules/:id'), async ({ request, params }) => {
      const call = ++calls
      const body = await request.json() as Pick<RuleView, 'agent_id' | 'answer' | 'enabled'>
      events.push(`start ${call} ${JSON.stringify(body)}`)
      if (call === 1) await delay(30)
      else await held
      state.rules = state.rules.map(item => item.rule_id === params.id ? { ...item, ...body } : item)
      events.push(`end ${call}`)
      return HttpResponse.json(state.rules.find(item => item.rule_id === params.id))
    }))
    render(<RulesPage />)
    const row = (await items())[0]
    await userEvent.selectOptions(within(row).getByRole('combobox', { name: 'Answer for rule 1' }), 'ask')
    await userEvent.click(within(row).getByRole('switch', { name: 'Rule 1 on' }))
    await waitFor(() => expect(events).toHaveLength(3))
    // The second write starts only after the first has answered.
    expect(events).toEqual([
      'start 1 {"agent_id":"writer","answer":"ask","enabled":true}',
      'end 1',
      'start 2 {"agent_id":"writer","answer":"ask","enabled":false}',
    ])
    // While it is held, nothing puts the unsaved change back, not even a
    // return to the window.
    const gets = state.gets
    focusManager.setFocused(false)
    focusManager.setFocused(true)
    focusManager.setFocused(undefined)
    await delay(30)
    expect(state.gets).toBe(gets)
    expect(within((await items())[0]).getByRole('switch', { name: 'Rule 1 on' })).not.toBeChecked()
    release()
    await waitFor(() => expect(events).toHaveLength(4))
    await waitFor(() => expect(state.gets).toBeGreaterThan(gets))
    const shown = (await items())[0]
    expect(within(shown).getByRole('combobox', { name: 'Answer for rule 1' })).toHaveValue('ask')
    expect(within(shown).getByRole('switch', { name: 'Rule 1 on' })).not.toBeChecked()
  })

  it('ignores a move while a write is running', async () => {
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    server.use(http.put(getApiUrl('rules/:id'), async ({ request, params }) => {
      state.writes.push(`PUT ${String(params.id)}`)
      await held
      return HttpResponse.json({ ...state.rules[0], ...(await request.json() as object) })
    }))
    render(<RulesPage />)
    const row = (await items())[0]
    await userEvent.selectOptions(within(row).getByRole('combobox', { name: 'Answer for rule 1' }), 'allow')
    const down = within(row).getByRole('button', { name: 'Move rule 1 down' })
    expect(down).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(down)
    release()
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Move rule 1 down' })).toHaveAttribute('aria-disabled', 'false'))
    expect(state.writes).toEqual(['PUT rule_1'])
  })

  it('ignores edits and deletes while a move is running', async () => {
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    server.use(http.put(getApiUrl('rules/order'), async ({ request }) => {
      const { rule_ids } = await request.json() as { rule_ids: string[] }
      state.writes.push('ORDER')
      await held
      state.rules = order(rule_ids)
      return HttpResponse.json({ rules: state.rules })
    }))
    render(<RulesPage />)
    const rows = await items()
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Move rule 1 down' }))
    await userEvent.click(within(rows[2]).getByRole('switch', { name: 'Rule 3 on' }))
    // An edit that went through would show at once.
    expect(within(rows[2]).getByRole('switch', { name: 'Rule 3 on' })).toBeChecked()
    await userEvent.click(within(rows[2]).getByRole('button', { name: 'Delete rule 3' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    release()
    await waitFor(() => expect(within(rows[2]).getByRole('button', { name: 'Delete rule 3' })).toHaveAttribute('aria-disabled', 'false'))
    expect(state.writes).toEqual(['ORDER'])
  })

  it('shows the field names of a 422 on an edit and puts the stored rule back', async () => {
    server.use(http.put(getApiUrl('rules/:id'), () => HttpResponse.json({ detail: { code: 'invalid_request', message: 'x', fields: ['answer'] } }, { status: 422 })))
    render(<RulesPage />)
    const row = (await items())[0]
    await userEvent.selectOptions(within(row).getByRole('combobox', { name: 'Answer for rule 1' }), 'allow')
    expect(await screen.findByText('The rule was not saved. Check: answer.')).toBeInTheDocument()
    await waitFor(async () => expect(within((await items())[0]).getByRole('combobox', { name: 'Answer for rule 1' })).toHaveValue('deny'))
  })

  it('treats a delete of a rule already gone as done, and moves the focus to the list heading', async () => {
    render(<RulesPage />)
    const rows = await items()
    // Another tab deleted it first.
    state.rules = state.rules.filter(item => item.rule_id !== 'rule_1')
    server.use(http.delete(getApiUrl('rules/:id'), () => HttpResponse.json({ detail: { code: 'rule_not_found', message: 'x' } }, { status: 404 })))
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Delete rule 1' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.queryByText(/no longer exists/)).not.toBeInTheDocument()
    await waitFor(async () => expect(await items()).toHaveLength(2))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Check rules' })).toHaveFocus())
  })

  it('moves the focus to the list heading after a delete', async () => {
    render(<RulesPage />)
    const rows = await items()
    await userEvent.click(within(rows[1]).getByRole('button', { name: 'Delete rule 2' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(state.writes).toEqual(['DELETE rule_2']))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Check rules' })).toHaveFocus())
  })

  it('shows the id, without calling it missing, while the agents load', async () => {
    agents.list = []
    agents.loading = true
    render(<RulesPage />)
    const row = (await items())[0]
    expect(within(row).getByRole('option', { name: 'writer', selected: true })).toBeInTheDocument()
    expect(within(row).queryByText(/no longer in your agents/)).not.toBeInTheDocument()
  })

  it('names an agent that is not running, without calling it missing', async () => {
    state.rules = [rule('rule_1', 'reviewer', 'deny')]
    render(<RulesPage />)
    const row = (await items())[0]
    expect(within(row).getByRole('option', { name: 'Reviewer (inactive)', selected: true })).toBeInTheDocument()
    expect(within(row).queryByText(/no longer in your agents/)).not.toBeInTheDocument()
  })

  it('replaces the list with the stored one after a 409 on reorder', async () => {
    server.use(http.put(getApiUrl('rules/order'), () => HttpResponse.json({ detail: { code: 'rule_order_mismatch', message: 'x' } }, { status: 409 })))
    render(<RulesPage />)
    const rows = await items()
    // Another tab added a rule since this page loaded.
    state.rules = [...state.rules, rule('rule_4', 'writer', 'allow')]
    await userEvent.click(within(rows[1]).getByRole('button', { name: 'Move rule 2 up' }))
    await waitFor(async () => expect(await items()).toHaveLength(4))
  })

  it('puts the focus back on the opener when a dialog closes without a change', async () => {
    const user = userEvent.setup()
    render(<RulesPage />)
    const rows = await items()
    await user.click(within(rows[1]).getByRole('button', { name: 'Delete rule 2' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(within(rows[1]).getByRole('button', { name: 'Delete rule 2' })).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Add rule' }))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add rule' })).toHaveFocus())
  })

  it('puts the focus back on Add rule after a rule is added', async () => {
    render(<RulesPage />)
    await items()
    await userEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add rule' })).toHaveFocus())
  })

  it('says deleting a rule that is off changes no answer', async () => {
    render(<RulesPage />)
    const rows = await items()
    await userEvent.click(within(rows[1]).getByRole('button', { name: 'Delete rule 2' }))
    expect(await screen.findByText('Ask for any agent. The rule is off, so deleting it changes no answer.')).toBeInTheDocument()
  })

  it('moves the focus to the list heading when a reload removes the focused row', async () => {
    render(<RulesPage />)
    const rows = await items()
    // Another tab deleted rule 2.
    state.rules = state.rules.filter(item => item.rule_id !== 'rule_2')
    server.use(http.put(getApiUrl('rules/:id'), () => HttpResponse.json({ detail: { code: 'rule_not_found', message: 'x' } }, { status: 404 })))
    const select = within(rows[1]).getByRole('combobox', { name: 'Answer for rule 2' })
    select.focus()
    await userEvent.selectOptions(select, 'deny')
    await waitFor(async () => expect(await items()).toHaveLength(2))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Check rules' })).toHaveFocus())
  })

  it('keeps a list that failed to reload, locked until a reload succeeds', async () => {
    render(<RulesPage />)
    const rows = await items()
    server.use(http.get(getApiUrl('rules'), () => HttpResponse.json({ detail: 'x' }, { status: 503 }), { once: true }))
    await userEvent.selectOptions(within(rows[0]).getByRole('combobox', { name: 'Answer for rule 1' }), 'ask')
    expect(await screen.findByText(/Rules couldn.t reload, so the list may be out of date/)).toBeInTheDocument()
    expect(await items()).toHaveLength(3)
    expect(screen.getByRole('button', { name: 'Add rule' })).toBeEnabled()
    const row = (await items())[1]
    expect(within(row).getByRole('button', { name: 'Move rule 2 up' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(row).getByRole('button', { name: 'Delete rule 2' })).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(within(row).getByRole('switch', { name: 'Rule 2 on' }))
    expect(state.writes).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name: 'Reload' }))
    await waitFor(() => expect(screen.queryByText(/couldn.t reload/)).not.toBeInTheDocument())
    await userEvent.click(within((await items())[1]).getByRole('switch', { name: 'Rule 2 on' }))
    await waitFor(() => expect(state.writes).toHaveLength(2))
  })

  it('ignores edits while the list reloads after a refused write', async () => {
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    render(<RulesPage />)
    const rows = await items()
    state.rules = state.rules.filter(item => item.rule_id !== 'rule_1')
    server.use(
      http.put(getApiUrl('rules/:id'), ({ params }) => {
        state.writes.push(`PUT ${String(params.id)}`)
        return HttpResponse.json({ detail: { code: 'rule_not_found', message: 'x' } }, { status: 404 })
      }, { once: true }),
      http.get(getApiUrl('rules'), async () => {
        await held
        state.gets += 1
        return HttpResponse.json({ rules: state.rules })
      }, { once: true }),
    )
    await userEvent.selectOptions(within(rows[0]).getByRole('combobox', { name: 'Answer for rule 1' }), 'allow')
    await screen.findByText('This rule no longer exists. The list is being reloaded.')
    await userEvent.click(within(rows[1]).getByRole('switch', { name: 'Rule 2 on' }))
    expect(within(rows[1]).getByRole('switch', { name: 'Rule 2 on' })).not.toBeChecked()
    release()
    await waitFor(async () => expect(await items()).toHaveLength(2))
    expect(state.writes).toEqual(['PUT rule_1'])
  })

  it('ignores edits while the list reloads after a delete of a rule already gone', async () => {
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    render(<RulesPage />)
    const rows = await items()
    // Another tab deleted rule 1 first.
    state.rules = state.rules.filter(item => item.rule_id !== 'rule_1')
    server.use(
      http.delete(getApiUrl('rules/:id'), () => HttpResponse.json({ detail: { code: 'rule_not_found', message: 'x' } }, { status: 404 }), { once: true }),
      http.get(getApiUrl('rules'), async () => {
        await held
        return HttpResponse.json({ rules: state.rules })
      }, { once: true }),
    )
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Delete rule 1' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    const row = (await items())[0]
    await userEvent.click(within(row).getByRole('switch', { name: 'Rule 1 on' }))
    expect(within(row).getByRole('switch', { name: 'Rule 1 on' })).not.toBeChecked()
    release()
    await waitFor(async () => expect(await items()).toHaveLength(2))
    expect(state.writes).toEqual([])
  })
})
