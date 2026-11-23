/** Operation admissions extend the shared Ego client using its endpoint, deadline and control lease. */
import { EgoRuntimeClient } from '@steward/dsh-ego-runtime-client'
import { baseUrl, requestJson } from '@steward/http-json'

/** ER-local authorization for one catalog version and observation. */
interface OperationAdmission {
  kind: 'operation_admission'
  receipt: string
  operation: string
  version: string
  frame_id: string
  ttl_s: number
}

export default class WorkstationClient extends EgoRuntimeClient {
  /** Check the workstation and observation without moving. */
  async prepare(operation: string, frameId: string, signal?: AbortSignal): Promise<OperationAdmission> {
    const result = await requestJson<OperationAdmission>(baseUrl(this.config.endpoint), 'POST', '/ego/prepare',
      { owner: this.config.owner, operation, frame_id: frameId }, { timeoutMs: this.config.timeoutMs, signal })
    if (result?.kind !== 'operation_admission' || result.operation !== operation || result.frame_id !== frameId ||
        !result.receipt || typeof result.version !== 'string' || !(result.ttl_s > 0)) {
      throw new Error('ER returned an invalid operation admission')
    }
    return result
  }

  /** Consumed receipts remain valid for retrieving the existing job after a lost reply. */
  async review(receipt: string, signal?: AbortSignal): Promise<void> {
    const result = await requestJson<{ kind: string; receipt: string }>(baseUrl(this.config.endpoint), 'POST',
      '/ego/admission', { owner: this.config.owner, receipt }, { timeoutMs: this.config.timeoutMs, signal })
    if (result?.kind !== 'operation_admission' || result.receipt !== receipt) {
      throw new Error('ER returned a mismatched operation admission')
    }
  }
}
