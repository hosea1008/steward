// Exercise the installed Harness, task host, model wire protocol, MCP transport and Python ER together.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { execFile, spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { promisify } from 'node:util'
import { createRequire } from 'node:module'
import { launchProfile } from './profile-test.mjs'

const root = resolve(import.meta.dirname, '../..')
const cliRequire = createRequire(createRequire(import.meta.url).resolve('@deepseek-ai/dsh/package.json'))
const run = promisify(execFile)
const task = '拿起面前的黑盒，抓稳后等我。'
const tools = ['body_stop', 'ego_observe', 'ego_prepare_operation', 'ego_state',
  'mcp__frame__inspect', 'skill_run', 'skill_status']

test('ego-real operator turn carries an actual image, admits once and reports unknown grasp', { timeout: 90000 }, async t => {
  const home = await mkdtemp(join(tmpdir(), 'ego-real-'))
  t.after(() => rm(home, { force: true, recursive: true }))
  const cwd = join(home, 'workspace')
  await mkdir(cwd)
  const er = spawn('python3', ['-u', '-m', 'steward.ego.real', '--backend', 'fake', '--port', '0'],
    { env: { ...process.env, PYTHONPATH: join(root, 'src') }, stdio: ['ignore', 'pipe', 'inherit'] })
  t.after(async () => { if (er.exitCode === null && er.kill()) await once(er, 'exit') })
  const endpoint = String((await once(er.stdout, 'data'))[0]).trim().split(' ').at(-1)
  const seen = []
  const snapshot = {}
  let frameId, jobId, failure
  const gateway = createServer(async (req, res) => {
    try {
      let body = ''
      for await (const part of req) body += part
      const request = JSON.parse(body)
      assert.equal(req.url, '/v1/chat/completions')
      const names = request.tools.map(tool => tool.function.name).sort()
      assert.deepEqual(names, tools)
      const messages = request.messages
      const initial = seen.length === 0
      if (initial) {
        const system = messages.filter(m => ['system', 'developer'].includes(m.role)).map(m => m.content).join('\n')
        assert.match(system, /Steward ego-real/)
        assert.doesNotMatch(system, /You are a coding agent|ROBOT_BRIEF|World Harness/)
        const user = messages.filter(m => m.role === 'user').map(m => m.content)
        assert.equal(user[0], task)
        assert.equal(user.length, 2)
        assert.match(user[1], /^Current runtime context/)
        assert.ok(user[1].includes(cwd))
        snapshot.first_request = { task: user[0], system, tool_parameters: Object.fromEntries(
          request.tools.map(({ function: f }) => [f.name, f.parameters])) }
      }
      const lastTool = messages.filter(m => m.role === 'tool').at(-1)
      const content = typeof lastTool?.content === 'string' ? lastTool.content : JSON.stringify(lastTool?.content)
      let name, args = {}, answer
      switch (seen.length) {
        case 0: name = 'ego_state'; break
        case 1: assert.match(content, /fixture-piper/); name = 'ego_observe'; break
        case 2:
          assert.match(JSON.stringify(messages), /data:image\/(png|jpeg|webp);base64,/)
          frameId = JSON.parse(content.split('\n')[0]).frame_id
          assert.ok(frameId)
          name = 'mcp__frame__inspect'; args = { frame_id: frameId }; break
        case 3:
          assert.match(content, /sha256/)
          assert.ok(content.includes(frameId))
          name = 'ego_prepare_operation'; args = { operation: 'pick_black_box', frame_id: frameId }; break
        case 4:
          assert.equal(JSON.parse(content).kind, 'operation_admission')
          name = 'skill_run'; args = { receipt: JSON.parse(content).receipt }; break
        case 5:
          jobId = JSON.parse(content).id
          assert.ok(jobId)
          await new Promise(resolve => setTimeout(resolve, 750))
          name = 'skill_status'; args = { job: jobId }; break
        case 6: {
          const status = JSON.parse(content)
          assert.equal(status.id, jobId)
          assert.equal(status.state, 'succeeded')
          assert.equal(status.skill, 'pick_black_box')
          assert.equal(status.hold_confirmed, true)
          assert.equal(status.reason, 'chunk_budget_exhausted')
          assert.equal(status.physical_result, 'unknown')
          const { id, frame_id, receipt, ...result } = status
          assert.ok(receipt && frame_id === frameId)
          snapshot.job = { ...result, id: '<job>', frame_id: '<frame>', receipt: '<admission>' }
          answer = '假设备作业完成，抓稳未知。'
          break
        }
        default: assert.fail('unexpected model turn')
      }
      seen.push(name ?? 'final')
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      const delta = name ? { role: 'assistant', tool_calls: [{ index: 0, id: 'call_' + seen.length,
        type: 'function', function: { name, arguments: JSON.stringify(args) } }] } : { role: 'assistant', content: answer }
      for (const [part, finish] of [[delta, null], [{}, name ? 'tool_calls' : 'stop']]) {
        res.write('data: ' + JSON.stringify({ id: 'reply', object: 'chat.completion.chunk', model: 'fixture',
          choices: [{ index: 0, delta: part, finish_reason: finish }] }) + '\n\n')
      }
      res.end('data: [DONE]\n\n')
    } catch (error) {
      failure = error
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: { message: error.message } }))
    }
  })
  gateway.listen(0, '127.0.0.1')
  await once(gateway, 'listening')
  t.after(() => { gateway.closeAllConnections(); gateway.close() })
  const env = { ...process.env, DSH_HOME: home, STEWARD_REAL_HOME: home, STEWARD_REAL_ENDPOINT: endpoint, STEWARD_FIXTURE_KEY: 'fixture' }
  await launchProfile(['--profile', 'ego-real', '--from-default-profile', 'headless', '--dump-default-config'], cwd, env)
  await launchProfile(['plugin', '--profile', 'ego-real', 'add', join(root, 'dsh/packages/dsh-bundle-ego-real')], cwd, env)
  const patch = join(home, 'ea-model.patch.yml')
  const template = await readFile(join(root, 'config/ego-real-ea-model.example.yml'), 'utf8')
  const deployment = [
    { id: 'llm-pi-ai', config: { providers: { fixture: { api: 'openai-completions', apiKeyEnv: 'STEWARD_FIXTURE_KEY',
      baseURL: 'http://127.0.0.1:' + gateway.address().port + '/v1', models: [{ id: 'fixture', input: ['text', 'image'] }] } } } },
    { insert: [{ id: 'frame-reader', name: cliRequire.resolve('@deepseek-ai/dsh-mcp-client'), config: { serverName: 'frame',
      transport: 'stdio', command: process.execPath, args: [join(import.meta.dirname, 'fixtures/real-frame-mcp.mjs'), endpoint] } }] },
  ]
  env.STEWARD_REAL_EA_PROVIDER = 'fixture'
  env.STEWARD_REAL_EA_MODEL = 'fixture'
  await writeFile(patch, template + deployment.map(entry => '- ' + JSON.stringify(entry)).join('\n') + '\n')
  try {
    const turn = await run(join(root, 'scripts/run-real-ego'), [task], { env, cwd, timeout: 40000 })
    if (failure) throw failure
    assert.match(turn.stdout, /抓稳未知/)
    snapshot.tool_sequence = seen
    const expected = join(import.meta.dirname, 'snapshots/ego-real-workstation.json')
    if (process.env.STEWARD_RECORD_REAL_FIXTURE === '1') {
      await writeFile(expected, JSON.stringify(snapshot, null, 2) + '\n')
    } else {
      assert.deepEqual(snapshot, JSON.parse(await readFile(expected, 'utf8')))
    }
    assert.deepEqual(seen, ['ego_state', 'ego_observe', 'mcp__frame__inspect', 'ego_prepare_operation', 'skill_run', 'skill_status', 'final'])
  } catch (error) { throw failure ?? error }
})
