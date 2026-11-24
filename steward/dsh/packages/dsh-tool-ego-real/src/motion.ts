/** Parameterized real motion tools for the console's native tool profile. */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool, type InferValue } from '@deepseek-ai/dsh-tools'
import type { JsonObject } from '@steward/dsh-ego'

export const name = 'steward-tool-ego-motion'
export const inject = ['ego', 'tools']

const schema = { type: 'json' } as const
type Json = InferValue<typeof schema>
const output = { schema, render: (_args: unknown, value: Json) => [{ type: 'text' as const, text: JSON.stringify(value) }] }
const json = (value: JsonObject): Json => JSON.parse(JSON.stringify(value)) as Json

/** Register native tools through the normal guarded pipeline.
 * Descriptions and parameter schemas are sent to the model automatically by the Harness.
 * Consumers use only the shared Ego service definition; HTTP and credentials belong to its provider.
 * New tools must also implement runtime validation, cancellation and feedback; see ../README.md.
 * @param ctx - Harness context owning the registrations.
 */
export function apply(ctx: Context): void {
  const ego = ctx.ego
  const motion = ego.motion
  if (!motion) throw new Error('motion tools require a ctx.ego provider with parameterized motion support')
  const navigation = ego.navigation
  if (!navigation) throw new Error('real console tools require a ctx.ego provider with navigation telemetry support')
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'get_navigation_state',
    description: 'Read current chassis navigation feedback from the device, unlike cached ego_state. Returns absolute map x/y in meters, heading_deg from map +X, map name, localization validity, scheduler and navigation status, task identity and progress. Read-only: no movement, stop, reset or fault clearance. Available with a software fault or motion disabled. Require state=succeeded and pose_valid=true before using coordinates; reading successfully does not prove physical rest or navigation success. Receive timestamps are host times; the two topics are not an atomic sensor snapshot.',
    parameters: { timeout_s: { type: 'number', required: true, description: 'Wait for both feedback topics, 0.1–30 seconds; authentication and connection have separate bounded timeouts.' } },
    output, execute: async (args, exec) => json(await navigation.readState(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'ego_state', description: 'Read cached real-motion state, faults, configured map and fixed speed limits. Does not contact actuators or measure the current pose.',
    parameters: {}, output, execute: async (_, exec) => json(await ego.summary(exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'navigate_to_pose',
    description: 'Move the real chassis to one absolute map pose. x/y are meters; heading_deg is degrees from map +X. Requires the named map already loaded and localized. Fixed 0.1 m/s and 0.1 rad/s, obstacle avoidance enabled. Blocks until success or failure; inspect state before dependent actions. Never retry an uncertain outcome. No object locations or routes are inferred.',
    parameters: {
      x: { type: 'number', required: true, description: 'Absolute map X coordinate in meters.' },
      y: { type: 'number', required: true, description: 'Absolute map Y coordinate in meters.' },
      heading_deg: { type: 'number', required: true, description: 'Heading in degrees, -180 to 180.' },
      map_name: { type: 'string', required: true, description: 'Exact name of the currently loaded and localized map.' },
      timeout_s: { type: 'number', required: true, description: 'Execution timeout, 0.1–600 seconds.' },
      position_tolerance_m: { type: 'number', required: true, description: 'Allowed final position error, 0.001–1 meter.' },
      heading_tolerance_deg: { type: 'number', required: true, description: 'Allowed final heading error, 0.1–30 degrees.' },
    }, output, execute: async (args, exec) => json(await motion.navigateToPose(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'lift_move',
    description: 'Move the real lift. action=height sets an absolute integer height in mm (0–750); action=home moves DOWN to the physical limit and establishes zero, and must omit height_mm. Homing is explicit, never automatic. After motor power loss the operator must establish the origin with home before height moves. Fixed 400 RPM. Blocks; require state=succeeded before dependent work.',
    parameters: {
      action: { type: 'string', required: true, enum: ['height', 'home'], description: 'height moves to an absolute height; home explicitly moves down to the limit and establishes zero.' },
      height_mm: { type: 'integer', description: 'Required only for action=height, 0–750 mm.' },
      timeout_s: { type: 'number', required: true, description: 'Execution timeout, 0.1–600 seconds.' },
    }, output, execute: async (args, exec) => json(await motion.moveLift(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'clear_motion_fault',
    description: 'Clear the console software motion-fault latch after the operator explicitly requests recovery and confirms the robot has stopped and the situation has been inspected. First read ego_state and use its current fault_id. Never invent operator confirmation or clear automatically after failure. No hardware reset, homing, movement, or retry is performed; previous failure evidence remains. Rejects active operations and stale fault IDs. Does not undo body_stop or enable motion; after stopping this turn, wait for a new operator turn before motion. A cleared latch does not prove hardware health or establish a lift origin.',
    parameters: {
      fault_id: { type: 'string', required: true, description: 'Exact current fault_id from ego_state; never guess or reuse an older fault ID.' },
      operator_confirmed_stopped: { type: 'boolean', required: true, description: 'Must be true only when the operator has explicitly confirmed physical rest and inspection for this fault.' },
      reason: { type: 'string', required: true, description: 'Operator-provided inspection/recovery reason, 1–1000 characters; not a claim of sensor verification.' },
    }, output, execute: async (args, exec) => json(await motion.clearFault(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'body_stop', description: 'Cancel this turn’s motion and wait for feedback. Inspect fault and stop acknowledgement; requesting stop does not establish physical rest. No further motion is admitted in this turn.',
    parameters: {}, output, execute: async (_, exec) => json(await ego.stop(exec.signal)),
  })))
}
