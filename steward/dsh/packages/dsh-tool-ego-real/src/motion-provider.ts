/** Console Motion HTTP adapter for ctx.ego; registering a provider does not register model tools. */
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { EgoMotion, EgoNavigation, JsonObject } from '@steward/dsh-ego'
import { EgoRuntimeClient, type Config as EgoClientConfig } from '@steward/dsh-ego-runtime-client'
import { baseUrl, requestJson } from '@steward/http-json'

/** Runtime connection with a private credential, never a model tool argument. */
export interface Config extends EgoClientConfig {
  token: string
}

export const name = 'steward-ego-motion-provider'
export const Config: z<Config> = z.intersect([
  EgoRuntimeClient.Config,
  z.object({ token: z.string().required() }),
])

/** Publish the robot service for the console's native motion tools.
 * @param ctx - context owning the service registration.
 * @param config - private runtime connection and HTTP deadline.
 */
export function apply(ctx: Context, config: Config): void {
  new MotionClient(ctx, config)
}

/** Publishes motion, cached summary and stop through the shared Ego service.
 * The console profile loads only motion tools; other Ego HTTP methods are not supported by Motion.
 */
class MotionClient extends EgoRuntimeClient {
  readonly motion: EgoMotion
  readonly navigation: EgoNavigation
  private readonly endpoint: string
  private readonly token: string

  constructor(ctx: Context, config: Config) {
    if (!config.token || !Number.isFinite(config.timeoutMs) || config.timeoutMs < 1000) {
      throw new Error('motion provider requires a private token and timeoutMs >= 1000')
    }
    const endpoint = baseUrl(config.endpoint)
    super(ctx, config)
    this.endpoint = endpoint
    this.token = config.token
    this.navigation = {
      readState: (request, signal) => this.request('/navigation-state', { arguments: request }, signal),
    }
    this.motion = {
      navigateToPose: (request, signal) => this.request('/execute', { tool: 'navigate_to_pose', arguments: request }, signal),
      moveLift: (request, signal) => this.request('/execute', { tool: 'lift_move', arguments: request }, signal),
      clearFault: (request, signal) => this.request('/clear-fault', { arguments: request }, signal),
    }
  }

  private request(path: string, body: object, signal?: AbortSignal): Promise<JsonObject> {
    return requestJson<JsonObject>(this.endpoint, 'POST', path, { ...body, token: this.token },
      { timeoutMs: this.config.timeoutMs, signal })
  }

  /** Read Motion's cache; this endpoint neither measures pose nor contacts actuators.
   * @param signal - caller cancellation.
   * @returns configured limits, active operation and cached execution feedback.
   */
  summary(signal?: AbortSignal): Promise<JsonObject> {
    return this.request('/state', {}, signal)
  }

  /** Cancel this console turn's motion and wait for feedback.
   * @param signal - caller cancellation; interruption does not establish physical rest.
   * @returns stop acknowledgements and any retained fault.
   */
  stop(signal?: AbortSignal): Promise<JsonObject> {
    return this.request('/stop', {}, signal)
  }
}
