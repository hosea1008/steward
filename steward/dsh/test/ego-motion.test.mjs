// The production console overlay runs against isolated model and motion HTTP fixtures.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { execFile } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { promisify } from 'node:util'
import { launchProfile } from './profile-test.mjs'

const root = resolve(import.meta.dirname, '../..')
const toolNames = ['arms_zero', 'body_stop', 'clear_motion_fault', 'ego_state', 'get_navigation_state', 'lift_move', 'move_for_duration', 'navigate_to_pose', 'skill', 'wam_operate']
const navigationFeedback = { state: 'succeeded', pose_valid: true, location_lost: false,
  pose: { x: 1.2, y: -.4, heading_deg: 90 }, map_name: 'fixture-map',
  scheduler_status: 3, navigation_status: { status: 3, text: 'task finish', map_name: 'fixture-map' } }
const timedMoveFeedback = {state: 'succeeded', command_stream_complete: true, direction: 'backward',
  speed: .3, speed_unit: 'm/s', duration_s: 2, commands_sent: 20,
  stop: {requested: true, write_acknowledged: true, controller_acknowledged: false, physical_stop_confirmed: false}}
test('console native calls carry tool descriptions and the saved context on every turn', { timeout: 90000 }, async t => {
  const home = await mkdtemp(join(tmpdir(), 'ego-motion-'))
  t.after(() => rm(home, { recursive: true, force: true }))
  const cwd = join(home, 'workspace')
  await mkdir(cwd)
  const requests = [], modelRequests = []
  let failure
  const runtime = createServer(async (req, res) => {
    try {
      let raw = ''
      for await (const data of req) raw += data
      const { token, ...body } = JSON.parse(raw)
      assert.equal(token, 'fixture-token')
      requests.push({ path: req.url, ...body })
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(req.url === '/state' ? { mode: 'fake', map_name: 'fixture-map', fault_id: 'fixture-fault', fault: {error: 'fixture failure'} }
        : req.url === '/navigation-state' ? navigationFeedback : body.tool === 'move_for_duration' ? timedMoveFeedback : { state: 'succeeded', mode: 'fake' }))
    } catch (error) { failure = error; res.writeHead(500); res.end('{}') }
  }).listen(0, '127.0.0.1')
  await once(runtime, 'listening')
  t.after(() => { runtime.closeAllConnections(); runtime.close() })
  const gateway = createServer(async (req, res) => {
    try {
      let raw = ''
      for await (const data of req) raw += data
      const request = JSON.parse(raw)
      assert.deepEqual(request.tools.map(t => t.function.name).sort(), toolNames)
      const step = modelRequests.length
      modelRequests.push(request)
      assert.ok(step <= 13)
      const currentUser = request.messages.filter(m => m.role === 'user' && typeof m.content === 'string'
        && m.content.includes('当前任务上下文')).at(-1)?.content
      assert.ok(currentUser)
      assert.match(currentUser, step < 11 ? /模拟物料位置由操作员提供/ : step <= 12 ? /"task_context": ""/ : /新会话保留的任务资料/)
      if (step >= 11) assert.match(currentUser, /操作员已保存以下 skill 更新/)
      if (step === 13) {
        assert.doesNotMatch(JSON.stringify(request.messages), /在模拟设备依次调用|只读确认当前上下文已清空/)
        assert.ok(!request.messages.some(m => m.role === 'tool'))
      }
      const system = request.messages.filter(m => ['system', 'developer'].includes(m.role)).map(m => m.content).join('\n')
      assert.doesNotMatch(system, /run_code|编写可复用/)
      assert.match(JSON.stringify(request.messages), /manhattan-navigation/)
      if (step === 7) assert.match(JSON.stringify(request.messages.filter(m => m.role === 'tool')), /黑色盒子归还未接入/)
      if (step === 1) {
        const loaded = JSON.stringify(request.messages.filter(m => m.role === 'tool'))
        assert.match(loaded, /X 后 Y 的执行顺序/)
        assert.match(loaded, /cx != tx/)
      }
      const calls = [
        { name: 'skill', arguments: {name: 'manhattan-navigation'} },
        { name: 'ego_state', arguments: {} },
        { name: 'get_navigation_state', arguments: {timeout_s: 5} },
        { name: 'clear_motion_fault', arguments: {fault_id: 'fixture-fault', operator_confirmed_stopped: true, reason: '操作员已确认停稳并检查现场'} },
        { name: 'navigate_to_pose', arguments: {x: 1.2, y: -0.4, heading_deg: 90, map_name: 'fixture-map', timeout_s: 20, position_tolerance_m: 0.05, heading_tolerance_deg: 2} },
        { name: 'lift_move', arguments: {action: 'height', height_mm: 220, timeout_s: 15} },
        { name: 'skill', arguments: {name: 'factory-wam-operation'} },
        { name: 'wam_operate', arguments: {prompt: 'Pick up one box from the material box and hold it steady with the right arm.', max_chunks: 8, timeout_s: 600} },
        { name: 'arms_zero', arguments: {timeout_s: 30, open_grippers: false} },
        { name: 'move_for_duration', arguments: {direction: 'backward', speed: .3, duration_s: 2} },
      ]
      const call = step === 11 ? {name: 'skill', arguments: {name: 'factory-wam-operation'}} : calls[step]
      if (step === 12) assert.match(JSON.stringify(request.messages.filter(m => m.role === 'tool')), /编辑后的模拟规程标记/)
      if (step > 2 && step < 11) assert.match(JSON.stringify(request.messages), /succeeded/)
      if (step === 10) assert.match(JSON.stringify(request.messages.filter(m => m.role === 'tool').at(-1)), /physical_stop_confirmed/)
      if (step === 3) {
        const feedback = request.messages.filter(m => m.role === 'tool').at(-1)
        assert.match(JSON.stringify(feedback), /pose_valid/)
        assert.match(JSON.stringify(feedback), /heading_deg/)
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      const delta = call ? { role: 'assistant', tool_calls: [{ index: 0, id: 'motion-' + step, type: 'function',
        function: { name: call.name, arguments: JSON.stringify(call.arguments) } }] }
        : { role: 'assistant', content: step === 10 ? '模拟设备导航与升降完成，未操作真机。' : step === 12 ? '当前任务上下文已清空。' : '新会话保留任务资料，未携带旧对话。' }
      for (const [part, finish] of [[delta, null], [{}, call ? 'tool_calls' : 'stop']]) {
        res.write('data: ' + JSON.stringify({ id: 'reply', object: 'chat.completion.chunk', model: 'fixture',
          choices: [{ index: 0, delta: part, finish_reason: finish }] }) + '\n\n')
      }
      res.end('data: [DONE]\n\n')
    } catch (error) { failure = error; res.writeHead(400); res.end('{}') }
  }).listen(0, '127.0.0.1')
  await once(gateway, 'listening')
  t.after(() => { gateway.closeAllConnections(); gateway.close() })
  const env = { ...process.env, DSH_HOME: home, STEWARD_FIXTURE_KEY: 'fixture' }
  await launchProfile(['--profile', 'ego-real', '--from-default-profile', 'headless', '--dump-default-config'], cwd, env)
  await launchProfile(['plugin', '--profile', 'ego-real', 'add', join(root, 'dsh/packages/dsh-bundle-ego-real')], cwd, env)
  const { stdout } = await promisify(execFile)(process.env.STEWARD_PYTHON || 'python3', [
    join(import.meta.dirname, 'fixtures/real-motion-chat.py'), home,
    'http://127.0.0.1:' + runtime.address().port, 'http://127.0.0.1:' + gateway.address().port,
  ], { env: { ...env, PYTHONPATH: join(root, 'src') }, timeout: 75000 })
  if (failure) throw failure
  assert.equal(modelRequests.length, 14)
  assert.deepEqual(requests.map(r => r.tool || r.path), ['/state', '/navigation-state', '/clear-fault', 'navigate_to_pose', 'lift_move', 'wam_operate', 'arms_zero', 'move_for_duration'])
  const transcript = JSON.parse(stdout)
  assert.equal(transcript.first.agents.robot.sessionId, transcript.second.agents.robot.sessionId)
  assert.notEqual(transcript.second.agents.robot.sessionId, transcript.third.agents.robot.sessionId)
  const snapshot = { toolDefinitions: modelRequests[0].tools.map(t => t.function), requests,
    toolCalls: transcript.first.messages.filter(e => e.role === 'tool').map(e => ({ tool: e.tool, input: e.input, status: e.status })),
    timedMoveResult: JSON.parse(transcript.first.messages.find(e => e.tool === 'move_for_duration').text),
    navigationResult: JSON.parse(transcript.first.messages.find(e => e.tool === 'get_navigation_state').text),
    answers: transcript.second.messages.filter(e => e.role === 'assistant').map(e => e.text),
    newSessionMessages: transcript.third.messages.map(e => ({role: e.role, text: e.text})),
    contextCleared: modelRequests[11].messages.filter(m => m.role === 'user' && typeof m.content === 'string'
      && m.content.includes('当前任务上下文')).at(-1).content }
  const path = join(import.meta.dirname, 'snapshots/ego-motion.json')
  if (process.env.STEWARD_RECORD_MOTION === '1') await writeFile(path, JSON.stringify(snapshot, null, 2) + '\n')
  else assert.deepEqual(snapshot, JSON.parse(await readFile(path, 'utf8')))
})
