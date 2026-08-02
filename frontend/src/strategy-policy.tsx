import { useCallback, useRef, useState } from "react"

import type { ConfigPayload, ExposureMode, StrategyConfig } from "@/lib/api"

const transitionMessage = "启用多空或杠杆会创建新的策略 revision，并重置回测与模拟盘审批。继续保存？"

export function snapshotStrategyConfig(
  config: StrategyConfig,
  market: "cn" | "us",
): StrategyConfig {
  const snapshot = structuredClone(config)
  if (market !== "us") snapshot.exposure_policy.mode = "LONG_ONLY"
  return snapshot
}

export function needsPolicyTransitionConfirmation(
  persistedMode: ExposureMode,
  nextMode: ExposureMode,
): boolean {
  return persistedMode === "LONG_ONLY" && nextMode !== "LONG_ONLY"
}

export function useStrategySaveFlow({
  config,
  market,
  confirm = () => window.confirm(transitionMessage),
  save,
  onSaved,
}: {
  config: StrategyConfig
  market: "cn" | "us"
  confirm?: () => boolean
  save: (payload: StrategyConfig) => Promise<ConfigPayload>
  onSaved?: (payload: ConfigPayload) => void
}) {
  const baselineRef = useRef<ExposureMode>(config.exposure_policy.mode)
  const pendingRef = useRef<Promise<ConfigPayload | null> | null>(null)
  const [isPending, setIsPending] = useState(false)
  const execute = useCallback(() => {
    if (pendingRef.current) return pendingRef.current
    const operation = (async () => {
      const payload = snapshotStrategyConfig(config, market)
      if (
        needsPolicyTransitionConfirmation(
          baselineRef.current,
          payload.exposure_policy.mode,
        )
        && !confirm()
      ) {
        return null
      }
      setIsPending(true)
      const result = await save(payload)
      baselineRef.current = result.config.exposure_policy.mode
      onSaved?.(result)
      return result
    })()
    pendingRef.current = operation
    const clearPending = () => {
      pendingRef.current = null
      setIsPending(false)
    }
    void operation.then(clearPending, clearPending)
    return operation
  }, [config, confirm, market, onSaved, save])

  const beforeRun = useCallback(async () => {
    const result = await execute()
    if (!result) throw new Error("SAVE_CANCELLED")
    return result
  }, [execute])

  const commitBaseline = useCallback((mode: ExposureMode) => {
    baselineRef.current = mode
  }, [])

  return { save: execute, beforeRun, isPending, commitBaseline }
}

export function parseBoundedNumber(
  raw: string,
  minimum: number,
  maximum: number,
): { value: number | null; error: string | null } {
  if (!raw.trim()) return { value: null, error: "请输入有效数字" }
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return { value: null, error: "请输入有效数字" }
  return { value: Math.min(Math.max(parsed, minimum), maximum), error: null }
}

export function PolicyNumberInput({
  label,
  value,
  minimum,
  maximum,
  step = 1,
  disabled = false,
  inputClassName,
  onCommit,
}: {
  label: string
  value: number
  minimum: number
  maximum: number
  step?: number
  disabled?: boolean
  inputClassName?: string
  onCommit: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  const [error, setError] = useState<string | null>(null)
  const errorId = `policy-number-${label.replaceAll(" ", "-")}-error`

  const commit = () => {
    const parsed = parseBoundedNumber(draft, minimum, maximum)
    setError(parsed.error)
    if (parsed.value === null) return
    setDraft(String(parsed.value))
    onCommit(parsed.value)
  }

  return (
    <span className="grid gap-1">
      <input
        aria-describedby={error ? errorId : undefined}
        aria-invalid={Boolean(error)}
        aria-label={label}
        className={inputClassName}
        disabled={disabled}
        max={maximum}
        min={minimum}
        onBlur={commit}
        onChange={(event) => {
          setDraft(event.target.value)
          setError(event.target.value.trim() ? null : "请输入有效数字")
        }}
        step={step}
        type="number"
        value={draft}
      />
      {error && <span id={errorId} role="alert">{error}</span>}
    </span>
  )
}
