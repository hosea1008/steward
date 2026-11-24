// Service substitution and cancellation are verified without hardware or a model API.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { Context } from '@deepseek-ai/cordis'
import EgoRuntimeClient from '@steward/dsh-ego-runtime-client'
import * as motionTools from '../packages/dsh-tool-ego-real/lib/motion.js'
import * as motionProvider from '../packages/dsh-tool-ego-real/lib/motion-provider.js'

test('motion tools use a substitute Ego service and release their registrations', async () => {
  const ctx = new Context(), definitions = new Map(), calls = []
  const controller = new AbortController()
  const record = name => async (...args) => { calls.push({ name, args }); return { state: 'succeeded' } }
  ctx.provide('ego', {
    summary: record('summary'), stop: record('stop'),
    motion: { navigateToPose: record('navigate'), moveLift: record('lift'), clearFault: record('clear') },
  })
  ctx.provide('tools', { register(tool) {
    definitions.set(tool.name, tool)
    return () => definitions.delete(tool.name)
  } })
  try {
    await ctx.plugin(motionTools)
    assert.deepEqual([...definitions.keys()].sort(), ['body_stop', 'clear_motion_fault', 'ego_state', 'lift_move', 'navigate_to_pose'])
    const nav = { x: 1, y: 2, heading_deg: 90, map_name: 'fixture', timeout_s: 20,
      position_tolerance_m: .05, heading_tolerance_deg: 2 }
    const lift = { action: 'home', timeout_s: 10 }
    const clear = { fault_id: 'fixture-fault', operator_confirmed_stopped: true, reason: 'operator inspected' }
    for (const [name, args] of [['ego_state', {}], ['navigate_to_pose', nav], ['lift_move', lift], ['clear_motion_fault', clear], ['body_stop', {}]]) {
      assert.deepEqual(await definitions.get(name).execute(args, { signal: controller.signal }), { state: 'succeeded' })
    }
    assert.deepEqual(calls, [
      { name: 'summary', args: [controller.signal] },
      { name: 'navigate', args: [nav, controller.signal] },
      { name: 'lift', args: [lift, controller.signal] },
      { name: 'clear', args: [clear, controller.signal] },
      { name: 'stop', args: [controller.signal] },
    ])
  } finally { await ctx.fiber.dispose() }
  assert.equal(definitions.size, 0)
})

test('an Ego provider without motion cannot register motion tools', async () => {
  const ctx = new Context()
  try {
    await ctx.plugin(EgoRuntimeClient, { endpoint: 'http://127.0.0.1:1' })
    assert.equal(ctx.ego.motion, undefined)
    assert.throws(() => motionTools.apply(ctx), /parameterized motion support/)
  } finally { await ctx.fiber.dispose() }
})

test('motion provider forwards credentials privately and closes an aborted request', { timeout: 10000 }, async t => {
  const ctx = new Context(), requests = []
  let received, closed
  const accepted = new Promise(resolve => { received = resolve })
  const disconnected = new Promise(resolve => { closed = resolve })
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const part of req) raw += part
    requests.push({ path: req.url, body: JSON.parse(raw) })
    if (req.url === '/execute') {
      res.on('close', closed)
      received()
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ mode: 'fake' }))
    }
  }).listen(0, '127.0.0.1')
  await once(server, 'listening')
  t.after(async () => {
    await ctx.fiber.dispose()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  })
  await ctx.plugin(motionProvider, { endpoint: `http://127.0.0.1:${server.address().port}`,
    token: 'fixture-secret', timeoutMs: 5000 })
  assert.deepEqual(await ctx.ego.summary(), { mode: 'fake' })
  assert.deepEqual(await ctx.ego.stop(), { mode: 'fake' })
  const clear = { fault_id: 'fixture-fault', operator_confirmed_stopped: true, reason: 'operator inspected' }
  assert.deepEqual(await ctx.ego.motion.clearFault(clear), { mode: 'fake' })
  const controller = new AbortController(), args = { action: 'height', height_mm: 200, timeout_s: 4 }
  const pending = ctx.ego.motion.moveLift(args, controller.signal)
  const rejected = assert.rejects(pending)
  await accepted
  controller.abort()
  await rejected
  await disconnected
  assert.deepEqual(requests, [
    { path: '/state', body: { token: 'fixture-secret' } },
    { path: '/stop', body: { token: 'fixture-secret' } },
    { path: '/clear-fault', body: { arguments: clear, token: 'fixture-secret' } },
    { path: '/execute', body: { tool: 'lift_move', arguments: args, token: 'fixture-secret' } },
  ])
})

test('motion provider rejects invalid connection configuration before publishing ego', async () => {
  for (const config of [
    { endpoint: 'http://127.0.0.1:1', token: '', timeoutMs: 5000 },
    { endpoint: 'http://127.0.0.1:1', token: 'fixture', timeoutMs: 0 },
    { endpoint: 'invalid-address', token: 'fixture', timeoutMs: 5000 },
  ]) {
    const ctx = new Context()
    try {
      assert.throws(() => motionProvider.apply(ctx, config), /private token and timeoutMs|not an absolute URL/)
      assert.equal(ctx.ego, undefined)
    } finally { await ctx.fiber.dispose() }
  }
})
