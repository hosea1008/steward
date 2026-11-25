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
  const manipulation = ego.manipulation
  if (!manipulation) throw new Error('real console tools require a ctx.ego provider with manipulation support')
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
    description: 'Move the real chassis to one absolute map pose. x/y are meters; heading_deg is degrees from map +X. Requires the named map already loaded and localized. Translation and rotation both default to 0.2; optional linear_speed_m_s is 0.1–0.5 m/s and angular_speed_rad_s is 0.1–0.4 rad/s. Hard caps cannot be overridden. Obstacle avoidance enabled. Use defaults without asking for speeds when the operator omits them. Blocks until success or failure; inspect state before dependent actions. Never retry an uncertain outcome. No object locations or routes are inferred.',
    parameters: {
      linear_speed_m_s: { type: 'number', description: 'Translation speed in m/s, 0.1–0.5; default 0.2. Reject values above the hard cap.' },
      angular_speed_rad_s: { type: 'number', description: 'Rotation speed in rad/s, 0.1–0.4; default 0.2. Reject values above the hard cap.' },
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
    name: 'move_for_duration',
    description: 'Move the real chassis in its own body frame for a finite duration via the NAVIS virtual joystick. forward/backward translate along X; turn_left/turn_right rotate about Z; shift_left/shift_right translate along Y and require a chassis that supports lateral motion. Speed is a positive magnitude: default 0.2, hard maximum 0.5; translation uses m/s and rotation rad/s. Requires a known direction and duration. Treat an explicit operator command as authorization after their site observation; do not routinely ask for clearance or permission again. Report any known concurrent controller/navigation conflict. No map target, localization, path planning or guaranteed obstacle avoidance. Publishes at 10 Hz and sends zero on completion or cancellation. state=succeeded means command transmission completed, NOT measured distance, angle or physical rest; inspect stop evidence and report its limits without adding a routine confirmation step; continue authorized steps after success unless concrete contradictory feedback exists. Never automatically retry an uncertain result.',
    parameters: {
      direction: { type: 'string', required: true, enum: ['forward', 'backward', 'turn_left', 'turn_right', 'shift_left', 'shift_right'], description: 'Direction relative to the chassis; lateral shifts require compatible hardware.' },
      speed: { type: 'number', description: 'Positive magnitude, greater than 0 and at most 0.5. Default 0.2. m/s for translation, rad/s for turning; direction supplies the sign.' },
      duration_s: { type: 'number', required: true, description: 'Movement-command duration after connection/heartbeat, 0.1–60 seconds. No default: obtain it from the operator or current task context; if missing, ask for duration rather than inventing a value. Does not imply precise distance or angle.' },
    }, output, execute: async (args, exec) => json(await motion.moveForDuration(args, exec.signal)),
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
    name: 'wam_operate',
    description: 'Execute a prompt on the real dual-arm Cosmos WAM using live robot cameras. Load the applicable operation skill for material prompts and chunk budgets. Operates at the current manipulation pose. An explicit operator command supplies site-observation authorization; do not routinely request another safety confirmation. Fixed arm speed 25%; no navigation, lift, automatic zero, task classification or retries. state=succeeded means the finite chunk budget ran, NOT that grasp/place succeeded; task_success remains unknown. Inspect feedback and follow the authorized skill workflow after successful execution, without treating unverified task success alone as a confirmation requirement. Failed or interrupted execution latches a fault; stop dependent work.',
    parameters: {
      prompt: { type: 'string', required: true, description: 'Exact operation instruction, 1–10000 characters; obtain material-specific text from a skill.' },
      max_chunks: { type: 'integer', required: true, description: 'Finite number of server inference chunks, 1–1000; use skill or explicit operator value. No unlimited execution.' },
      timeout_s: { type: 'number', required: true, description: 'Whole operation deadline, 0.1–600 seconds, including startup and inference.' },
      control_mode: { type: 'string', enum: ['movej', 'movejs'], description: 'Joint controller mode; default movej. Speed remains fixed.' },
      action_dt: { type: 'number', description: 'Controller step seconds, 0.01–0.2; default 0.02.' },
      model_fps: { type: 'number', description: 'Expected server FPS, 1–60; default 30. Mismatch fails before arm enable.' },
      expected_execute_steps: { type: 'integer', description: 'Optional required server execute_steps, 1–10000; mismatch fails before arm enable.' },
      wait_tol_deg: { type: 'number', description: 'Joint arrival diagnostic tolerance, 0.1–5 degrees; default 3.' },
      smooth_method: { type: 'string', enum: ['none', 'spline', 'savgol', 'butter'], description: 'Trajectory smoothing; default spline.' },
      smooth_factor: { type: 'number', description: 'Spline smoothing factor, 0–1; default 0.1.' },
      v_max: { type: 'number', description: 'Smoother joint velocity limit in rad/s, 0.01–3; default 3, does not override fixed motor speed.' },
      outlier_threshold: { type: 'number', description: 'Trajectory outlier threshold, 0.1–30 degrees; default 10.' },
      savgol_window: { type: 'integer', description: 'Odd filter window, 3–101; default 11, greater than savgol_order.' },
      savgol_order: { type: 'integer', description: 'Filter polynomial order, 1–10; default 2.' },
      butter_cutoff: { type: 'number', description: 'Butterworth cutoff Hz, 0.01–40 and below half controller frequency; default 5.' },
      left_gripper_scale: { type: 'number', description: 'Left gripper width scale, 0–1; default 1.' },
      right_gripper_scale: { type: 'number', description: 'Right gripper width scale, 0–1; default 1.' },
      gripper_close_threshold: { type: 'number', description: 'Width at or below which to close, 0–0.08 meters; default 0.02.' },
      save_prediction_video: { type: 'boolean', description: 'Request and save model prediction video after execution; default false. Uses remaining timeout.' },
    }, output, execute: async (args, exec) => json(await manipulation.operateWam(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'arms_zero',
    description: 'Move both real Piper arms to zero joint angles at fixed 10% speed. open_grippers defaults to true and opens both grippers to device-reported maximum travel and RELEASES held objects; false leaves gripper targets unchanged. A zeroing command includes opening the grippers by default; do not ask whether to open them. Explicit open_grippers=false preserves their targets. An explicit zeroing command authorizes the motion after operator site observation; do not routinely ask for clearance again. No lift movement or automatic hardware fault reset. Success requires five fresh samples within 1 degree and, when opening, 1 mm. Failure/cancellation attempts electronic stop and latches a fault; software fault clearance does not reset device emergency stop.',
    parameters: {
      timeout_s: { type: 'number', required: true, description: 'Whole operation timeout, 0.1–600 seconds; normally 30 seconds.' },
      open_grippers: { type: 'boolean', description: 'Default true: open and release held objects. Omit or use true for ordinary zeroing; use false only to preserve current gripper targets. Do not ask for this optional parameter.' },
    }, output, execute: async (args, exec) => json(await manipulation.zeroArms(args, exec.signal)),
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'body_stop', description: 'Cancel this turn’s motion and wait for feedback. Inspect fault and stop acknowledgement; requesting stop does not establish physical rest. No further motion is admitted in this turn.',
    parameters: {}, output, execute: async (_, exec) => json(await ego.stop(exec.signal)),
  })))
}
