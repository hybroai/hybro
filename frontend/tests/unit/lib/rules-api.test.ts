import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import { ApiError } from '@/lib/api-client'
import {
  createRule,
  deleteRule,
  listRules,
  reorderRules,
  ruleErrorMessage,
  updateRule,
  type RuleView,
} from '@/lib/api/rules'

const RULE: RuleView = {
  rule_id: 'rule_1',
  kind: 'check',
  agent_id: 'writer',
  answer: 'deny',
  enabled: true,
  created_at: '2026-11-20T10:00:00Z',
  updated_at: '2026-11-20T10:00:00Z',
}
const ANY: RuleView = { ...RULE, rule_id: 'rule_2', agent_id: null, answer: 'ask' }

const refusal = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, 'x', { detail: { code, message: 'Backend text that is not shown.', ...extra } })

describe('rule API client', () => {
  it('lists the rules in order', async () => {
    server.use(http.get(getApiUrl('rules'), () => HttpResponse.json({ rules: [RULE, ANY] })))
    await expect(listRules()).resolves.toEqual({ rules: [RULE, ANY] })
  })

  it('creates a rule with its agent, answer and state in the body', async () => {
    let body: unknown
    server.use(http.post(getApiUrl('rules'), async ({ request }) => {
      body = await request.json()
      return HttpResponse.json(ANY, { status: 201 })
    }))
    await expect(createRule({ agent_id: null, answer: 'ask', enabled: true })).resolves.toEqual(ANY)
    expect(body).toEqual({ agent_id: null, answer: 'ask', enabled: true })
  })

  it('replaces and deletes a rule by an encoded id', async () => {
    const seen: string[] = []
    server.use(
      http.put(getApiUrl('rules/rule%2F1'), async ({ request }) => {
        seen.push(`PUT ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ ...RULE, enabled: false })
      }),
      http.delete(getApiUrl('rules/rule%2F1'), () => {
        seen.push('DELETE')
        return new HttpResponse(null, { status: 204 })
      }),
    )
    await expect(updateRule('rule/1', { agent_id: 'writer', answer: 'deny', enabled: false })).resolves.toMatchObject({ enabled: false })
    await expect(deleteRule('rule/1')).resolves.toBeUndefined()
    expect(seen).toEqual(['PUT {"agent_id":"writer","answer":"deny","enabled":false}', 'DELETE'])
  })

  it('puts a new order with every rule id', async () => {
    let body: unknown
    server.use(http.put(getApiUrl('rules/order'), async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ rules: [ANY, RULE] })
    }))
    await expect(reorderRules(['rule_2', 'rule_1'])).resolves.toEqual({ rules: [ANY, RULE] })
    expect(body).toEqual({ rule_ids: ['rule_2', 'rule_1'] })
  })

  it('shows fixed text for each refusal, never the backend message', () => {
    expect(ruleErrorMessage(refusal(404, 'rule_not_found'))).toBe('This rule no longer exists. The list has been reloaded.')
    expect(ruleErrorMessage(refusal(409, 'rule_order_mismatch'))).toBe('The rules changed since this page loaded. The list has been reloaded; try again.')
    expect(ruleErrorMessage(refusal(409, 'rule_limit_reached'))).toBe('The list holds the most rules it can. Delete a rule to add another.')
    expect(ruleErrorMessage(refusal(422, 'invalid_request', { fields: ['agent_id', 'answer'] }))).toBe('The rule was not saved. Check: agent_id, answer.')
    expect(ruleErrorMessage(refusal(422, 'invalid_request'))).toBe('The rule was not saved. Check the rule and try again.')
    expect(ruleErrorMessage(refusal(503, 'rules_unavailable'))).toBe('Rules are unavailable right now. Try again.')
    expect(ruleErrorMessage(new ApiError(500, 'boom'))).toBe('Something went wrong. Try again.')
    expect(ruleErrorMessage(new Error('network'))).toBe('Something went wrong. Try again.')
  })

  it('names only the fields the backend may name in a 422', () => {
    const odd = refusal(422, 'invalid_request', { fields: ['answer', '<script>', 42] })
    expect(ruleErrorMessage(odd)).toBe('The rule was not saved. Check: answer.')
  })
})
