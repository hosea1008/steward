// Real model, isolated fake robot endpoints: ordinary commands need no extra safety approval.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { execFile } from 'node:child_process'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { promisify } from 'node:util'
import { launchProfile } from './profile-test.mjs'

test('DeepSeek executes clear commands and only asks for missing facts on fake hardware', {
  skip: process.env.STEWARD_TEST_DIRECT_COMMANDS !== '1' || !process.env.DEEPSEEK_API_KEY,
  timeout: 420000,
}, async t => {
  const home = await mkdtemp(join(tmpdir(), 'ego-direct-'))
  t.after(() => rm(home, {recursive: true, force: true}))
  const cwd = join(home, 'workspace'), root = resolve(import.meta.dirname, '../..')
  await mkdir(cwd)
  const env = {...process.env, DSH_HOME: home}
  await launchProfile(['--profile', 'ego-real', '--from-default-profile', 'headless', '--dump-default-config'], cwd, env)
  await launchProfile(['plugin', '--profile', 'ego-real', 'add', join(root, 'dsh/packages/dsh-bundle-ego-real')], cwd, env)
  const {stdout} = await promisify(execFile)(process.env.STEWARD_PYTHON || 'python3', [
    join(import.meta.dirname, 'fixtures/direct-commands-live.py'), home,
  ], {env: {...env, PYTHONPATH: join(root, 'src')}, timeout: 360000})
  const results = JSON.parse(stdout)
  assert.equal(results.length, 5)
  for (const result of results) t.diagnostic(JSON.stringify(result))
})
