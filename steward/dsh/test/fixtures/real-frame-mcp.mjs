// Read-only stdio MCP fixture. It receives one frame capability, never the control lease.
import { createInterface } from 'node:readline'
import { createHash } from 'node:crypto'
for await (const line of createInterface({ input: process.stdin })) {
  const { id, method, params } = JSON.parse(line)
  if (id === undefined) continue
  let result
  if (method === 'initialize') {
    result = { protocolVersion: '2025-11-25', capabilities: { tools: {} }, serverInfo: { name: 'frame-fixture', version: '1' } }
  } else if (method === 'tools/list') {
    result = { tools: [{ name: 'inspect', description: 'Read a captured fixture frame by ID and return its byte digest.',
      inputSchema: { type: 'object', properties: { frame_id: { type: 'string' } }, required: ['frame_id'] } }] }
  } else if (method === 'tools/call' && params.name === 'inspect') {
    const url = new URL('/ego/frame', process.argv[2])
    url.searchParams.set('frame_id', params.arguments.frame_id)
    const res = await fetch(url)
    const frame = await res.json()
    const text = res.ok ? JSON.stringify({ frame_id: frame.frame_id, camera: frame.camera,
      sha256: createHash('sha256').update(Buffer.from(frame.png_base64, 'base64')).digest('hex') }) : frame.error
    result = { isError: !res.ok, content: [{ type: 'text', text }] }
  } else {
    console.log(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: 'unsupported method' } }))
    continue
  }
  console.log(JSON.stringify({ jsonrpc: '2.0', id, result }))
}
