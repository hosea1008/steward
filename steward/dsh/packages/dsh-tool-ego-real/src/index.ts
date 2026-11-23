/** Native workstation tools. The ER revalidates admissions atomically before it dispatches a job. */
import type { Context } from '@deepseek-ai/cordis'
import { AttachmentId } from '@deepseek-ai/dsh-attachment'
import type {} from '@deepseek-ai/dsh-llm'
import { defineTool, type InferValue } from '@deepseek-ai/dsh-tools'
import WorkstationClient from './provider.js'

export const name = 'steward-tool-ego-real'
export const inject = ['ego', 'tools', 'attachments', 'llm']

const jsonSchema = { type: 'json' } as const
type Json = InferValue<typeof jsonSchema>
const output = {
  schema: jsonSchema,
  render: (_args: unknown, value: Json) => [{ type: 'text' as const, text: JSON.stringify(value) }],
}
function json(value: unknown): Json {
  return JSON.parse(JSON.stringify(value)) as Json
}

/** Install the native tools and the admission check in this Harness instance. */
export function apply(ctx: Context): void {
  const ego = ctx.ego
  if (!(ego instanceof WorkstationClient)) throw new Error('ego-real requires its workstation provider')
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (exec.name !== 'skill_run') return next()
    const args = exec.arguments
    if (!args || typeof args !== 'object' || !('receipt' in args) || typeof args.receipt !== 'string') {
      return { kind: 'deny', reason: 'prepare an operation first' }
    }
    try {
      await ego.review(args.receipt, exec.signal)
    } catch (error) {
      return { kind: 'deny', reason: String(error) }
    }
    return next()
  })
  ctx.tools.register(defineTool({
    name: 'ego_state', description: 'Read device mode, readiness, fault, active job and operation catalog.',
    parameters: {}, output, execute: async (_, exec) => json(await ego.summary(exec.signal)),
  }))
  ctx.tools.register(defineTool({
    name: 'ego_observe', description: 'Acquire a timestamped workstation image and gripper feedback on demand.',
    parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: false, properties: {
        evidence: jsonSchema,
        attachmentId: { type: 'string', required: true }, bytes: { type: 'integer', required: true },
        width: { type: 'integer', required: true }, height: { type: 'integer', required: true },
        mediaType: { type: 'string', required: true, enum: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] },
      } },
      render: (_, v) => [
        { type: 'text', text: JSON.stringify(v.evidence) },
        { type: 'image', attachment: { attachmentId: AttachmentId(v.attachmentId), mediaType: v.mediaType,
          bytes: v.bytes, width: v.width, height: v.height } },
      ],
    },
    async execute(_, exec) {
      const route = exec.agent?.session.requestHeader()?.config ?? exec.agent?.options
      if (!route?.provider || !route.model ||
          !(await ctx.llm.resolveModelInfo(route.provider, route.model, exec.signal)).inputModalities?.includes('image')) {
        throw new Error('ego_observe requires an image-capable model route')
      }
      const { png_base64, ...evidence } = await ego.observe(exec.signal)
      if (typeof png_base64 !== 'string' || typeof evidence.frame_id !== 'string') throw new Error('invalid ER frame')
      const image = await ctx.attachments.saveImage({ data: Buffer.from(png_base64, 'base64'), mediaType: 'image/png' })
      return { evidence: json(evidence), attachmentId: image.attachmentId, mediaType: image.mediaType,
        bytes: image.bytes, width: image.width, height: image.height }
    },
  }))
  ctx.tools.register(defineTool({
    name: 'ego_prepare_operation', description: 'Check workstation admission for the observed frame. No movement.',
    parameters: { operation: { type: 'string', required: true }, frame_id: { type: 'string', required: true } },
    output, execute: async (args, exec) => json(await ego.prepare(args.operation, args.frame_id, exec.signal)),
  }))
  ctx.tools.register(defineTool({
    name: 'skill_run', description: 'Start the prepared operation asynchronously. Retry the same receipt to recover its job.',
    parameters: { receipt: { type: 'string', required: true } },
    output, execute: async (args, exec) => json(await ego.run(args.receipt, exec.signal)),
  }))
  ctx.tools.register(defineTool({
    name: 'skill_status', description: 'Read execution state, exit reason, hold acknowledgement and physical result.',
    parameters: { job: { type: 'string', required: true } },
    output, execute: async (args, exec) => json(await ego.job(args.job, exec.signal)),
  }))
  ctx.tools.register(defineTool({
    name: 'body_stop', description: 'Revoke outstanding actions and request local stop and hold. Inspect acknowledgement.',
    parameters: {}, output, execute: async (_, exec) => json(await ego.stop(exec.signal)),
  }))
}
