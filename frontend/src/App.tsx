import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table"
import {
  Activity,
  ArrowLeft,
  BellRing,
  CheckCircle2,
  CircleDotDashed,
  Clock3,
  Copy,
  Database,
  Filter,
  Menu,
  Plus,
  Play,
  RotateCcw,
  Save,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import {
  chatStrategy,
  activateStrategy,
  createStrategy,
  deactivateStrategy,
  deleteStrategy,
  duplicateStrategy,
  getStrategies,
  getStrategy,
  getStrategyRun,
  getStrategyRuns,
  resetStrategy,
  saveStrategy,
  startStrategyRun,
  syncStrategyDelivery,
  type ConfigPayload,
  type AllocationConfig,
  type ChatMessage,
  type ExposureMode,
  type ExposurePolicy,
  type MarginPolicy,
  type Parameter,
  type ParameterStatus,
  type PortfolioConfig,
  type ReportDelivery,
  type StrategyDraft,
  type StrategyChatResponse,
  type StrategyLibrary,
  type StrategyRunStatus,
  type StrategySummary,
  type ShortPolicy,
  type UsDataSourcePolicy,
  type UsMarketDataStatus,
} from "@/lib/api"
import { cn } from "@/lib/utils"
import { PolicyNumberInput, useStrategySaveFlow } from "@/strategy-policy"

type StatusFilter = "all" | "enabled" | "available" | "planned"
const usInapplicableParameters = new Set([
  "stock_prefixes",
  "exclude_st",
  "exclude_limit_up",
  "turnover_rate_min",
  "turnover_rate_max",
  "volume_ratio_min",
  "float_market_cap_min",
  "float_market_cap_max",
  "pb_max",
  "ignition_price_10s_min",
  "ignition_volume_ratio_min",
  "fcf_yield_min",
  "revenue_growth_min",
  "profit_growth_min",
  "eps_growth_min",
  "roe_min",
  "roa_min",
  "roic_min",
  "gross_margin_min",
  "net_margin_min",
  "operating_cashflow_positive",
  "free_cashflow_positive",
  "debt_ratio_max",
  "current_ratio_min",
])

const statusCopy: Record<ParameterStatus, { label: string; description: string; variant: "success" | "info" | "warning" }> = {
  live: { label: "已接入", description: "数据源直接提供", variant: "success" },
  derived: { label: "可计算", description: "由行情数据计算", variant: "info" },
  planned: { label: "待接入", description: "仅保存策略意图", variant: "warning" },
}

const operatorCopy = { min: "不低于", max: "不高于", equals: "必须满足", in: "包含" }
const channelCopy: Record<ReportDelivery["channel"], string> = {
  feishu: "飞书",
  telegram: "Telegram",
  discord: "Discord",
  signal: "Signal",
  origin: "任务来源",
  local: "仅本地",
}
const lifecycleCopy: Record<StrategySummary["lifecycle"]["stage"], string> = {
  draft: "草稿",
  backtesting: "回测中",
  paper: "模拟盘",
  live: "实盘",
  paused: "已暂停",
  archived: "已归档",
}

function deliveryTime(delivery: ReportDelivery) {
  return `${String(delivery.hour).padStart(2, "0")}:${String(delivery.minute).padStart(2, "0")}`
}

function deliverySummary(delivery: ReportDelivery) {
  if (!delivery.enabled) return "未开启推送"
  const frequency = delivery.frequency === "weekdays" ? "工作日" : "每天"
  return `${channelCopy[delivery.channel]} · ${frequency} ${deliveryTime(delivery)}`
}

function formatValue(parameter: Parameter, value = parameter.value) {
  if (parameter.kind === "boolean") return value ? "是" : "否"
  if (Array.isArray(value)) return value.join("、") || "未设置"
  if (parameter.kind === "number") {
    const displayed = Number(value || 0) / Number(parameter.scale || 1)
    return `${displayed.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${parameter.unit ? ` ${parameter.unit}` : ""}`
  }
  const option = parameter.options.find((item) => item.value === value)
  return option?.label || String(value ?? "未设置")
}

function ValueEditor({ parameter, onChange }: { parameter: Parameter; onChange: (value: unknown) => void }) {
  if (parameter.kind === "boolean") {
    return <span className="text-xs text-muted-foreground">启用时要求“是”</span>
  }
  if (parameter.kind === "choice") {
    return (
      <Select value={String(parameter.value)} onValueChange={onChange}>
        <SelectTrigger className="h-8 min-w-32 bg-background"><SelectValue /></SelectTrigger>
        <SelectContent>{parameter.options.map((option) => <SelectItem key={option.value} value={option.value} disabled={option.disabled}>{option.label}</SelectItem>)}</SelectContent>
      </Select>
    )
  }
  if (parameter.kind === "multi" || parameter.kind === "tags") {
    return (
      <Input
        className="h-8 min-w-[88px] font-mono text-xs sm:min-w-40"
        value={Array.isArray(parameter.value) ? parameter.value.join("、") : ""}
        onChange={(event) => onChange(event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))}
        placeholder="用顿号分隔"
      />
    )
  }
  if (parameter.kind === "number") {
    return (
      <div className="flex min-w-[88px] items-center gap-1.5 sm:min-w-36 sm:gap-2">
        <Input
          className="h-8 px-2 text-right font-mono text-xs tabular-nums"
          type="number"
          step={parameter.step || 0.01}
          value={Number(parameter.value || 0) / Number(parameter.scale || 1)}
          onChange={(event) => onChange(Number(event.target.value || 0) * Number(parameter.scale || 1))}
        />
        {parameter.unit && <span className="min-w-fit text-xs text-muted-foreground">{parameter.unit}</span>}
      </div>
    )
  }
  return <Input className="h-8 min-w-[88px] sm:min-w-40" value={String(parameter.value ?? "")} onChange={(event) => onChange(event.target.value)} />
}

function DataSourceSettings({
  parameter,
  status,
  onChange,
}: {
  parameter: Parameter
  status: UsMarketDataStatus
  onChange: (value: UsDataSourcePolicy) => void
}) {
  const policy = String(parameter.value || "auto") as UsDataSourcePolicy
  const effectiveSource = policy === "sina"
    ? "sina"
    : status.alpaca_configured
      ? "alpaca"
      : policy === "auto"
        ? "sina"
        : "unavailable"
  const sourceCopy = effectiveSource === "alpaca"
    ? { label: `Alpaca ${status.alpaca_feed.toUpperCase()}`, tone: "text-emerald-700", dot: "bg-emerald-500" }
    : effectiveSource === "sina"
      ? { label: "新浪财经", tone: "text-amber-700", dot: "bg-amber-500" }
      : { label: "不可用", tone: "text-destructive", dot: "bg-destructive" }

  return (
    <section className="mb-5 overflow-hidden rounded-lg border bg-background shadow-xs">
      <div className="grid gap-0 lg:grid-cols-[minmax(280px,0.9fr)_minmax(420px,1.35fr)]">
        <div className="border-b p-5 lg:border-b-0 lg:border-r">
          <div className="mb-4 flex items-start justify-between gap-4">
            <div>
              <div className="mb-1 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Database className="size-3.5" />
                策略级数据路由
              </div>
              <h3 className="text-base font-semibold">美股数据源</h3>
            </div>
            <Badge variant={effectiveSource === "unavailable" ? "warning" : "success"}>
              {effectiveSource === "unavailable" ? "需要配置" : "运行可用"}
            </Badge>
          </div>
          <Select value={policy} onValueChange={(value: UsDataSourcePolicy) => onChange(value)}>
            <SelectTrigger className="w-full bg-background"><SelectValue /></SelectTrigger>
            <SelectContent>
              {parameter.options.map((option) => (
                <SelectItem key={option.value} value={option.value} disabled={option.disabled}>
                  <div className="flex flex-col py-0.5">
                    <span>{option.label}</span>
                    {option.description && <span className="text-[11px] text-muted-foreground">{option.description}</span>}
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            选择会随策略版本保存，回测、推荐、持仓风控和盘中报告使用同一数据路由。
          </p>
        </div>

        <div className="p-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-xs text-muted-foreground">当前实际来源</div>
              <div className={cn("mt-1 flex items-center gap-2 text-sm font-semibold", sourceCopy.tone)}>
                <span className={cn("size-2 rounded-full", sourceCopy.dot)} />
                {sourceCopy.label}
              </div>
            </div>
            <span className="font-mono text-[11px] text-muted-foreground">
              policy/{policy}
            </span>
          </div>

          <div className="relative grid grid-cols-2 gap-3 before:absolute before:left-1/2 before:top-5 before:h-px before:w-8 before:-translate-x-1/2 before:bg-border">
            {status.providers.map((provider) => {
              const active = provider.id === effectiveSource
              return (
                <div key={provider.id} className={cn("relative rounded-md border px-3 py-3", active ? "border-primary/40 bg-primary/[0.035]" : "bg-muted/20")}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">{provider.label}</span>
                    <span className={cn("size-2 rounded-full", provider.available ? "bg-emerald-500" : "bg-muted-foreground/35")} />
                  </div>
                  <div className="mt-1 text-[11px] text-muted-foreground">{provider.plan}</div>
                  <div className="mt-2 text-[11px]">
                    {provider.available ? "已就绪" : "未配置 API Key"}
                  </div>
                </div>
              )
            })}
          </div>

          <div className={cn(
            "mt-4 rounded-md border px-3 py-2.5 text-xs leading-5",
            effectiveSource === "unavailable"
              ? "border-destructive/25 bg-destructive/5 text-destructive"
              : !status.alpaca_configured
                ? "border-amber-200 bg-amber-50 text-amber-800"
                : "border-emerald-200 bg-emerald-50 text-emerald-800",
          )}>
            {!status.alpaca_configured
              ? policy === "auto"
                ? "Alpaca Basic 免费，但仍需 API Key 做身份识别和限流。当前自动模式会使用新浪备用源，配置 Key 后自动切回 Alpaca。"
                : policy === "alpaca"
                  ? "仅 Alpaca 模式当前不可运行：请先在服务器 .env 配置 API Key。生成 Key 不代表开通付费套餐。"
                  : "当前固定使用新浪；Alpaca Basic 免费，但需要服务器 API Key 才能使用。"
              : policy === "sina"
                ? "Alpaca 已就绪，但该策略按配置固定使用新浪。"
                : "Alpaca Basic 已就绪。密钥只保存在服务器环境变量，不进入策略配置。"}
          </div>
        </div>
      </div>
    </section>
  )
}

function ParameterTable({ parameters, onUpdate }: { parameters: Parameter[]; onUpdate: (id: string, patch: Partial<Parameter>) => void }) {
  const columnHelper = createColumnHelper<Parameter>()
  const columns = useMemo(() => [
    columnHelper.display({
      id: "enabled",
      header: "启用",
      cell: ({ row }) => (
        <Switch
          aria-label={`${row.original.enabled ? "停用" : "启用"}${row.original.label}`}
          checked={row.original.enabled}
          onCheckedChange={(enabled) => onUpdate(row.original.id, { enabled, value: row.original.kind === "boolean" && enabled ? true : row.original.value })}
        />
      ),
    }),
    columnHelper.accessor("label", {
      header: "参数",
      cell: ({ row }) => (
        <div className="min-w-[145px] py-0.5 sm:min-w-56">
          <div className="flex items-center gap-2">
            <span className="font-medium text-foreground">{row.original.label}</span>
            {row.original.enabled && row.original.status === "planned" && <CircleDotDashed className="size-3.5 text-amber-600" />}
          </div>
          <p className="parameter-description mt-1 max-w-md text-xs leading-5 text-muted-foreground">{row.original.description}</p>
        </div>
      ),
    }),
    columnHelper.accessor("status", {
      header: "数据状态",
      cell: ({ getValue }) => {
        const status = statusCopy[getValue()]
        return <Badge variant={status.variant}>{status.label}</Badge>
      },
    }),
    columnHelper.accessor("operator", {
      header: "条件",
      cell: ({ getValue }) => <span className="text-xs text-muted-foreground">{operatorCopy[getValue()]}</span>,
    }),
    columnHelper.display({
      id: "value",
      header: "当前值",
      cell: ({ row }) => <ValueEditor parameter={row.original} onChange={(value) => onUpdate(row.original.id, { value })} />,
    }),
    columnHelper.accessor("id", {
      header: "参数键",
      cell: ({ getValue }) => <code className="text-[11px] text-muted-foreground">{getValue()}</code>,
    }),
  ], [columnHelper, onUpdate])

  const table = useReactTable({ data: parameters, columns, getCoreRowModel: getCoreRowModel() })

  return (
    <div className="overflow-hidden rounded-lg border bg-background">
      <Table>
        <TableHeader className="bg-muted/55">
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => <TableHead key={header.id} className={cn(header.id === "enabled" && "w-16", header.id === "id" && "hidden xl:table-cell")}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</TableHead>)}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.length ? table.getRowModel().rows.map((row) => (
            <TableRow key={row.id} data-state={row.original.enabled ? "selected" : undefined} className="data-[state=selected]:bg-primary/[0.025]">
              {row.getVisibleCells().map((cell) => <TableCell key={cell.id} className={cn(cell.column.id === "id" && "hidden xl:table-cell")}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>)}
            </TableRow>
          )) : (
            <TableRow><TableCell colSpan={columns.length} className="h-40 text-center text-muted-foreground">没有符合当前筛选条件的参数</TableCell></TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  )
}

const initialAssistantMessage: ChatMessage = {
  role: "assistant",
  content: "先说说你想实现的选股思路。我会逐步确认股票范围、持有周期、风险和筛选偏好，整理完整后再请你确认生成。",
}

function StrategyMapper({ strategyId, parameters, onApply }: { strategyId: string; parameters: Parameter[]; onApply: (draft: StrategyDraft) => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([initialAssistantMessage])
  const [input, setInput] = useState("")
  const [result, setResult] = useState<StrategyChatResponse | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const mutation = useMutation({
    mutationFn: (nextMessages: ChatMessage[]) => chatStrategy(nextMessages, strategyId),
    onSuccess: (response) => {
      setResult(response)
      setMessages((current) => [...current, { role: "assistant", content: response.message }])
    },
    onError: (error) => toast.error(error.message),
  })

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })
  }, [messages, mutation.isPending])

  const sendMessage = (content = input.trim()) => {
    if (!content || mutation.isPending) return
    const nextMessages = [...messages, { role: "user" as const, content }]
    setMessages(nextMessages)
    setInput("")
    setResult(null)
    mutation.mutate(nextMessages)
  }

  const resetConversation = () => {
    setMessages([initialAssistantMessage])
    setInput("")
    setResult(null)
    mutation.reset()
  }

  const stage = result?.status === "confirmed" ? "参数草案" : result?.status === "review" ? "等待确认" : "需求澄清"
  const draft = result?.status === "confirmed" ? result.draft : null

  return (
    <Sheet>
      <SheetTrigger asChild><Button size="sm" className="size-8 px-0 xl:w-auto xl:px-3"><Sparkles /><span className="hidden xl:inline">AI 策略助手</span><span className="sr-only xl:hidden">AI 策略助手</span></Button></SheetTrigger>
      <SheetContent className="flex w-full flex-col p-0 sm:max-w-2xl">
        <SheetHeader className="border-b px-5 py-4 pr-12 sm:px-6">
          <div className="flex items-start justify-between gap-4">
            <div>
              <SheetTitle>AI 策略助手</SheetTitle>
              <SheetDescription className="mt-1">通过对话澄清策略，确认后才生成参数草案。</SheetDescription>
            </div>
            <Button size="icon" variant="ghost" title="重新开始" onClick={resetConversation}><RotateCcw /><span className="sr-only">重新开始</span></Button>
          </div>
          <div className="flex items-center gap-2 pt-1">
            <Badge variant={result?.status === "confirmed" ? "success" : result?.status === "review" ? "warning" : "info"}>{stage}</Badge>
            {result?.provider === "fallback" && <span className="text-xs text-muted-foreground">基础对话模式</span>}
          </div>
        </SheetHeader>
        <div ref={scrollRef} className="flex-1 overflow-y-auto bg-muted/25 px-4 py-5 sm:px-6">
          <div className="mx-auto max-w-xl space-y-4">
            {messages.map((message, index) => (
              <div key={`${message.role}-${index}`} className={cn("flex gap-2.5", message.role === "user" && "justify-end")}>
                {message.role === "assistant" && <div className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md border bg-background text-primary"><Sparkles className="size-3.5" /></div>}
                <div className={cn("max-w-[85%] whitespace-pre-wrap rounded-lg px-3.5 py-2.5 text-sm leading-6", message.role === "user" ? "bg-primary text-primary-foreground" : "border bg-background text-foreground shadow-xs")}>
                  {message.content}
                </div>
              </div>
            ))}
            {mutation.isPending && (
              <div className="flex gap-2.5">
                <div className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md border bg-background text-primary"><Sparkles className="size-3.5" /></div>
                <div className="flex items-center gap-2 rounded-lg border bg-background px-3.5 py-2.5 text-sm text-muted-foreground shadow-xs"><Activity className="size-3.5 animate-pulse" />正在整理策略</div>
              </div>
            )}

            {draft && (
              <section className="overflow-hidden rounded-lg border bg-background shadow-xs">
                <div className="flex items-center justify-between border-b bg-muted/40 px-4 py-3">
                  <div className="flex items-center gap-2"><SlidersHorizontal className="size-4 text-primary" /><h3 className="text-sm font-semibold">参数草案</h3></div>
                  <Badge variant="outline">{draft.recognized_count} 项</Badge>
                </div>
                <div className="divide-y px-4">
                  {draft.updates.map((update) => {
                    const parameter = parameters.find((item) => item.id === update.id)
                    return (
                      <div key={update.id} className="flex items-start justify-between gap-4 py-3">
                        <div><p className="text-sm font-medium">{parameter?.label || update.id}</p><p className="mt-1 text-xs text-muted-foreground">{update.reason}</p></div>
                        <span className="whitespace-nowrap font-mono text-xs">{parameter ? formatValue(parameter, update.value) : String(update.value)}</span>
                      </div>
                    )
                  })}
                  {!draft.updates.length && <p className="py-6 text-center text-sm text-muted-foreground">{draft.message}</p>}
                </div>
              </section>
            )}
          </div>
        </div>
        <div className="border-t bg-background p-4 sm:px-6">
          {result?.status === "review" && (
            <Button className="mb-3 w-full" variant="secondary" disabled={mutation.isPending} onClick={() => sendMessage("确认生成策略")}><CheckCircle2 />确认生成策略</Button>
          )}
          {draft && (
            <SheetClose asChild>
              <Button className="mb-3 w-full" disabled={!draft.updates.length} onClick={() => onApply(draft)}><CheckCircle2 />应用参数草案</Button>
            </SheetClose>
          )}
          <div className="flex items-end gap-2">
            <Textarea
              className="min-h-11 max-h-32 resize-none"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  sendMessage()
                }
              }}
              placeholder="描述策略或回答问题"
              aria-label="策略对话输入"
            />
            <Button size="icon" className="size-11 shrink-0" disabled={!input.trim() || mutation.isPending} onClick={() => sendMessage()}><Send /><span className="sr-only">发送</span></Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

const runStatusCopy: Record<StrategyRunStatus, { label: string; variant: "info" | "success" | "warning" }> = {
  queued: { label: "等待执行", variant: "warning" },
  running: { label: "执行中", variant: "info" },
  succeeded: { label: "执行完成", variant: "success" },
  failed: { label: "执行失败", variant: "warning" },
}

function StrategyRunSheet({
  strategyId,
  strategyName,
  buttonVariant = "outline",
  compact = false,
  pendingChanges = false,
  beforeRun,
}: {
  strategyId: string
  strategyName: string
  buttonVariant?: "outline" | "ghost"
  compact?: boolean
  pendingChanges?: boolean
  beforeRun?: () => Promise<unknown>
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const runsQuery = useQuery({
    queryKey: ["strategy-runs", strategyId],
    queryFn: () => getStrategyRuns(strategyId),
    enabled: open,
  })
  const activeRunId = selectedRunId || runsQuery.data?.runs[0]?.id || null

  const runQuery = useQuery({
    queryKey: ["strategy-run", activeRunId],
    queryFn: () => getStrategyRun(activeRunId!),
    enabled: open && Boolean(activeRunId),
    refetchInterval: (query) => ["queued", "running"].includes(query.state.data?.status || "") ? 1500 : false,
  })
  useEffect(() => {
    if (runQuery.data && ["succeeded", "failed"].includes(runQuery.data.status)) {
      queryClient.invalidateQueries({ queryKey: ["strategy-runs", strategyId] })
    }
  }, [queryClient, runQuery.data, strategyId])

  const startMutation = useMutation({
    mutationFn: async () => {
      if (beforeRun) await beforeRun()
      return startStrategyRun(strategyId)
    },
    onSuccess: (run) => {
      setSelectedRunId(run.id)
      queryClient.setQueryData(["strategy-run", run.id], run)
      queryClient.invalidateQueries({ queryKey: ["strategy-runs", strategyId] })
      toast.success("策略已开始执行")
    },
    onError: (error) => { if (error.message !== "SAVE_CANCELLED") toast.error(error.message) },
  })
  const currentRun = runQuery.data
  const running = currentRun?.status === "queued" || currentRun?.status === "running"
  const status = currentRun ? runStatusCopy[currentRun.status] : null

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild><Button size="sm" variant={buttonVariant} className={cn(compact && "size-8 px-0 xl:w-auto xl:px-3")}><Play /><span className={cn(compact && "hidden xl:inline")}>立即执行</span>{compact && <span className="sr-only xl:hidden">立即执行</span>}</Button></SheetTrigger>
      <SheetContent className="flex w-full flex-col p-0 sm:max-w-3xl">
        <SheetHeader className="border-b px-5 py-4 pr-12 sm:px-6">
          <div className="flex items-start justify-between gap-4">
            <div><SheetTitle>运行效果</SheetTitle><SheetDescription className="mt-1">{strategyName} · 单次试跑，不切换当前使用策略，不发送消息{pendingChanges ? " · 将先保存当前修改" : ""}</SheetDescription></div>
            {status && <Badge variant={status.variant}>{status.label}</Badge>}
          </div>
        </SheetHeader>
        <div className="flex items-center gap-2 border-b bg-muted/25 px-4 py-3 sm:px-6">
          <Select value={activeRunId || ""} onValueChange={setSelectedRunId} disabled={!runsQuery.data?.runs.length}>
            <SelectTrigger className="min-w-0 flex-1 bg-background"><SelectValue placeholder="暂无运行记录" /></SelectTrigger>
            <SelectContent>
              {runsQuery.data?.runs.map((run) => <SelectItem key={run.id} value={run.id}>{new Date(run.created_at).toLocaleString("zh-CN")} · {runStatusCopy[run.status].label}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button disabled={running || startMutation.isPending} onClick={() => startMutation.mutate()}><Play />{running ? "执行中" : pendingChanges ? "保存并执行" : "立即执行"}</Button>
        </div>
        <div className="flex-1 overflow-y-auto bg-muted/20 p-4 sm:p-6">
          {running && <div className="grid min-h-64 place-items-center"><div className="text-center text-sm text-muted-foreground"><Activity className="mx-auto mb-3 size-5 animate-pulse text-primary" />正在获取行情并生成报告</div></div>}
          {!currentRun && !runQuery.isLoading && <div className="grid min-h-64 place-items-center text-sm text-muted-foreground">暂无运行记录</div>}
          {runQuery.isLoading && <div className="grid min-h-64 place-items-center text-sm text-muted-foreground"><Activity className="size-4 animate-pulse" /></div>}
          {currentRun?.status === "failed" && <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{currentRun.error || "策略执行失败"}</div>}
          {currentRun?.status === "succeeded" && (
            <article className="rounded-lg border bg-background shadow-xs">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b px-4 py-3 text-xs text-muted-foreground">
                <span>{currentRun.completed_at ? new Date(currentRun.completed_at).toLocaleString("zh-CN") : ""}</span>
                <span>耗时 {currentRun.duration_seconds ?? 0} 秒</span>
                <span>策略：{currentRun.strategy_name}</span>
              </div>
              <pre className="whitespace-pre-wrap break-words p-4 font-sans text-sm leading-7 text-foreground sm:p-6">{currentRun.report || "本次运行没有输出内容"}</pre>
            </article>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function PortfolioNumberField({
  label,
  description,
  value,
  suffix,
  step = 1,
  disabled = false,
  onChange,
}: {
  label: string
  description: string
  value: number
  suffix?: string
  step?: number
  disabled?: boolean
  onChange: (value: number) => void
}) {
  return (
    <label className="grid gap-2 rounded-md border bg-muted/15 p-4">
      <span><span className="block text-sm font-medium">{label}</span><span className="mt-1 block text-xs leading-5 text-muted-foreground">{description}</span></span>
      <span className="flex items-center gap-2"><Input disabled={disabled} type="number" step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} /><span className="min-w-10 text-xs text-muted-foreground">{suffix}</span></span>
    </label>
  )
}

function PortfolioSettings({ portfolio, allocation, strategyId, market, onChange, onAllocationChange }: { portfolio: PortfolioConfig; allocation: AllocationConfig; strategyId: string; market: "cn" | "us"; onChange: (patch: Partial<PortfolioConfig>) => void; onAllocationChange: (patch: Partial<AllocationConfig>) => void }) {
  const field = (key: keyof PortfolioConfig) => (value: number) => onChange({ [key]: value } as Partial<PortfolioConfig>)
  const allocationField = (key: keyof AllocationConfig) => (value: number) => onAllocationChange({ [key]: value } as Partial<AllocationConfig>)
  const performanceUrl = `/strategies/${encodeURIComponent(strategyId)}/portfolio`
  const isUs = market === "us"
  const currency = isUs ? "USD" : "元"
  return (
    <div className="max-w-5xl space-y-5">
      <section className="overflow-hidden rounded-lg border bg-background shadow-xs">
        <div className="flex flex-col justify-between gap-4 border-b px-5 py-4 sm:flex-row sm:items-center sm:px-6">
          <div className="flex items-start gap-3"><div className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary"><ShieldCheck className="size-4" /></div><div><h3 className="text-sm font-semibold">板块状态与仓位预算</h3><p className="mt-1 text-xs text-muted-foreground">{allocation.model} · 板块广度 → 目标仓位 → 个股绝对动量</p></div></div>
          <Switch aria-label="启用板块状态控制" checked={allocation.enabled} onCheckedChange={(enabled) => onAllocationChange({ enabled })} />
        </div>
        <div className={cn("space-y-5 px-5 py-5 sm:px-6", !allocation.enabled && "opacity-55")}>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <PortfolioNumberField label="多数广度阈值" description="20日、60日和趋势广度达到该比例才算通过" value={allocation.breadth_threshold_pct} suffix="%" step={1} onChange={allocationField("breadth_threshold_pct")} />
            <PortfolioNumberField label="强势目标仓位" description="三项广度信号全部通过" value={allocation.risk_on_exposure_pct} suffix="%" step={5} onChange={allocationField("risk_on_exposure_pct")} />
            <PortfolioNumberField label="震荡目标仓位" description="三项广度信号中两项通过" value={allocation.neutral_exposure_pct} suffix="%" step={5} onChange={allocationField("neutral_exposure_pct")} />
            <PortfolioNumberField disabled label="弱势目标仓位" description="少于两项通过时保留现金" value={allocation.risk_off_exposure_pct} suffix="%" onChange={allocationField("risk_off_exposure_pct")} />
            <PortfolioNumberField label="个股20日动量" description="低于该绝对收益率的股票不得入场" value={allocation.minimum_candidate_momentum20_pct} suffix="%" step={0.5} onChange={allocationField("minimum_candidate_momentum20_pct")} />
            <PortfolioNumberField label="个股趋势门槛" description="0 至 2；至少满足一层均线趋势" value={allocation.minimum_candidate_trend} suffix="/2" step={1} onChange={allocationField("minimum_candidate_trend")} />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="flex items-center justify-between gap-4 rounded-md border bg-muted/15 p-4"><span><span className="block text-sm font-medium">弱市退出</span><span className="mt-1 block text-xs text-muted-foreground">状态转为弱势或数据不足时生成退出单</span></span><Switch checked={allocation.exit_on_risk_off} onCheckedChange={(exit_on_risk_off) => onAllocationChange({ exit_on_risk_off })} /></label>
            <label className="flex items-center justify-between gap-4 rounded-md border bg-muted/15 p-4"><span><span className="block text-sm font-medium">按目标仓位再平衡</span><span className="mt-1 block text-xs text-muted-foreground">状态降级时先退出信号最弱的持仓</span></span><Switch checked={allocation.rebalance_to_target_exposure} onCheckedChange={(rebalance_to_target_exposure) => onAllocationChange({ rebalance_to_target_exposure })} /></label>
          </div>
        </div>
      </section>
      <section className="overflow-hidden rounded-lg border bg-background shadow-xs">
        <div className="flex flex-col justify-between gap-4 border-b px-5 py-4 sm:flex-row sm:items-center sm:px-6">
          <div className="flex items-start gap-3"><div className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary"><Database className="size-4" /></div><div><h3 className="text-sm font-semibold">策略持仓 Pipeline</h3><p className="mt-1 text-xs text-muted-foreground">信号 → 意图 → 风控 → 模拟成交 → 持仓 → 退出 → 账本</p></div></div>
          <div className="flex items-center gap-3"><Button asChild size="sm" variant="outline"><a href={performanceUrl} target="_blank" rel="noreferrer">查看策略表现</a></Button><Switch aria-label="启用策略持仓" checked={portfolio.enabled} onCheckedChange={(enabled) => onChange({ enabled })} /></div>
        </div>
        <div className={cn("space-y-6 px-5 py-5 sm:px-6", !portfolio.enabled && "opacity-55")}>
          <div><h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">组合与入场</h4><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <PortfolioNumberField disabled label="最大持仓" description="硬上限；每只股票占用一个独立槽位" value={portfolio.max_positions} suffix="只" onChange={field("max_positions")} />
            <PortfolioNumberField label="初始资金" description="每个策略独立的模拟账户本金" value={portfolio.initial_cash} suffix={currency} step={10000} onChange={field("initial_cash")} />
            <PortfolioNumberField label="目标权重" description="按下单前冻结净值计算，未用资金保留现金" value={portfolio.target_weight_pct} suffix="%" step={0.5} onChange={field("target_weight_pct")} />
            <PortfolioNumberField label="信号失效" description="连续多少次日评估无效后退出" value={portfolio.signal_invalid_days} suffix="天" onChange={field("signal_invalid_days")} />
          </div></div>
          <div><h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">退出与替换</h4><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <PortfolioNumberField label="固定止损" description="相对含费成本触发退出" value={portfolio.stop_loss_pct} suffix="%" step={0.5} onChange={field("stop_loss_pct")} />
            <PortfolioNumberField label="追踪止盈激活" description="从成本上涨到该幅度后开始追踪峰值" value={portfolio.trailing_activation_pct} suffix="%" step={0.5} onChange={field("trailing_activation_pct")} />
            <PortfolioNumberField label="峰值回撤退出" description="追踪激活后相对峰值的退出距离" value={portfolio.trailing_drawdown_pct} suffix="%" step={0.5} onChange={field("trailing_drawdown_pct")} />
            <PortfolioNumberField label="替换分差" description="新候选至少高出当前持仓的信号分" value={portfolio.replacement_score_delta} suffix="分" step={0.01} onChange={field("replacement_score_delta")} />
          </div></div>
          <div><h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">组合回撤闸门</h4><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <PortfolioNumberField label="预警线" description="停止新开仓并限制组合暴露" value={portfolio.warning_drawdown_pct} suffix="%" step={0.5} onChange={field("warning_drawdown_pct")} />
            <PortfolioNumberField label="只减仓线" description="退出全部当前可卖持仓" value={portfolio.derisk_drawdown_pct} suffix="%" step={0.5} onChange={field("derisk_drawdown_pct")} />
            <PortfolioNumberField label="人工暂停线" description="暂停信号与新订单，保留风控执行" value={portfolio.halt_drawdown_pct} suffix="%" step={0.5} onChange={field("halt_drawdown_pct")} />
            <PortfolioNumberField label="预警最大暴露" description="预警状态允许的最高股票仓位" value={portfolio.warning_max_exposure_pct} suffix="%" step={1} onChange={field("warning_max_exposure_pct")} />
          </div></div>
          <div><h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">模拟成交成本</h4>{isUs && <p className="mb-3 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900">美股由市场适配器执行整股、允许日内卖出和免佣模拟；保留滑点与成交参与率。</p>}<div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <PortfolioNumberField disabled={isUs} label="佣金率" description="买卖双边；另计最低佣金" value={isUs ? 0 : portfolio.commission_rate_pct} suffix="%" step={0.001} onChange={field("commission_rate_pct")} />
            <PortfolioNumberField disabled={isUs} label="最低佣金" description="单笔最低佣金金额" value={isUs ? 0 : portfolio.minimum_commission_cny} suffix={currency} step={1} onChange={field("minimum_commission_cny")} />
            <PortfolioNumberField disabled={isUs} label="卖出印花税" description="仅卖出方向计费" value={isUs ? 0 : portfolio.stamp_duty_rate_pct} suffix="%" step={0.001} onChange={field("stamp_duty_rate_pct")} />
            <PortfolioNumberField label="滑点" description="在下一可执行行情基础上的保守偏移" value={portfolio.slippage_bps} suffix="bps" step={1} onChange={field("slippage_bps")} />
          </div></div>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="grid gap-2 rounded-md border bg-muted/15 p-4"><span><span className="block text-sm font-medium">基准代码</span><span className="mt-1 block text-xs text-muted-foreground">用于对比策略超额收益</span></span><Input value={portfolio.benchmark_symbol} onChange={(event) => onChange({ benchmark_symbol: event.target.value })} /></label>
            <label className="grid gap-2 rounded-md border bg-muted/15 p-4"><span><span className="block text-sm font-medium">基准名称</span><span className="mt-1 block text-xs text-muted-foreground">策略表现页显示名称</span></span><Input value={portfolio.benchmark_name} onChange={(event) => onChange({ benchmark_name: event.target.value })} /></label>
          </div>
        </div>
      </section>
      <p className="text-xs leading-6 text-muted-foreground">订单不会使用产生信号的同一时点价格成交；{isUs ? "美股按整股模拟并允许日内卖出" : "A 股买入遵守整手与 T+1"}，卖出受可卖数量、市场规则和成交量参与率约束。策略版本变化会撤销旧版本未成交订单。</p>
    </div>
  )
}

function PolicyNumberField({ label, value, minimum = 0, maximum, suffix = "%", disabled = false, step = 1, onChange }: { label: string; value: number; minimum?: number; maximum: number; suffix?: string; disabled?: boolean; step?: number; onChange: (value: number) => void }) {
  return (
    <label className={cn("grid gap-2 border-l-2 border-slate-200 bg-slate-50/65 px-3 py-3", disabled && "opacity-50")}>
      <span className="flex items-start justify-between gap-3 text-xs">
        <span className="font-medium text-foreground">{label}</span>
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">系统上限 {maximum}{suffix}</span>
      </span>
      <span className="flex items-center gap-2">
        <PolicyNumberInput key={`${label}-${value}`} label={label} value={value} minimum={minimum} maximum={maximum} step={step} disabled={disabled} inputClassName="h-9 w-full rounded-md border border-input bg-transparent px-3 text-right font-mono text-sm tabular-nums shadow-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50" onCommit={onChange} />
        <span className="min-w-8 text-xs text-muted-foreground">{suffix}</span>
      </span>
    </label>
  )
}

function LongShortPolicySettings({ market, exposure, margin, short, onExposureChange, onMarginChange, onShortChange }: { market: "cn" | "us"; exposure: ExposurePolicy; margin: MarginPolicy; short: ShortPolicy; onExposureChange: (patch: Partial<ExposurePolicy>) => void; onMarginChange: (patch: Partial<MarginPolicy>) => void; onShortChange: (patch: Partial<ShortPolicy>) => void }) {
  const locked = market !== "us"
  const mode: ExposureMode = locked ? "LONG_ONLY" : exposure.mode
  const shortDisabled = mode !== "LONG_SHORT"
  const exposureField = (key: keyof ExposurePolicy) => (value: number) => onExposureChange({ [key]: value } as Partial<ExposurePolicy>)
  const marginField = (key: keyof MarginPolicy) => (value: number) => onMarginChange({ [key]: value } as Partial<MarginPolicy>)
  const shortField = (key: keyof ShortPolicy) => (value: number) => onShortChange({ [key]: value } as Partial<ShortPolicy>)
  const gross = mode === "LONG_ONLY" ? Math.min(exposure.max_gross_exposure_pct, 100) : mode === "LONG_LEVERAGED" ? Math.min(exposure.max_gross_exposure_pct, 120) : exposure.max_gross_exposure_pct
  const long = mode === "LONG_ONLY" ? Math.min(exposure.max_long_exposure_pct, 100) : exposure.max_long_exposure_pct
  const shortLimit = mode === "LONG_SHORT" ? exposure.max_short_exposure_pct : 0

  return (
    <section className="risk-policy-panel overflow-hidden border border-slate-300 bg-background shadow-xs">
      <div className="grid border-b border-slate-300 lg:grid-cols-[minmax(260px,0.8fr)_minmax(440px,1.4fr)]">
        <div className="border-b bg-[#10243e] px-5 py-5 text-white lg:border-b-0 lg:border-r lg:border-slate-500">
          <div className="font-mono text-[10px] font-semibold tracking-[0.18em] text-sky-200">RISK BOUNDARY / STRATEGY</div>
          <h3 className="mt-2 text-lg font-semibold">多空与杠杆</h3>
          <p className="mt-2 text-xs leading-5 text-slate-300">策略级风险边界独立于持仓 Pipeline。所有数值仍受系统硬上限约束。</p>
          <div className="mt-5">
            <label className="mb-2 block text-xs font-medium text-slate-200" htmlFor="exposure-mode">敞口模式</label>
            <Select disabled={locked} value={mode} onValueChange={(value: ExposureMode) => onExposureChange({ mode: value })}>
              <SelectTrigger id="exposure-mode" className="w-full border-slate-500 bg-slate-900/40 text-white"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="LONG_ONLY">只做多 · 最高 100%</SelectItem>
                <SelectItem value="LONG_LEVERAGED">杠杆做多 · 最高 120%</SelectItem>
                <SelectItem value="LONG_SHORT">多空组合 · 总敞口最高 150%</SelectItem>
              </SelectContent>
            </Select>
            {locked && <p className="mt-3 border-l-2 border-amber-300 pl-3 text-xs leading-5 text-amber-100">当前市场强制 LONG_ONLY：市场对应的融资与借券支持尚未接通。</p>}
          </div>
        </div>
        <div className="exposure-tape grid grid-cols-2 divide-x divide-y divide-slate-200 sm:grid-cols-4 sm:divide-y-0" aria-label="策略敞口边界">
          {[
            ["LONG", long, "多头上限"],
            ["SHORT", shortLimit, "空头上限"],
            ["GROSS", gross, "总敞口"],
            ["NET", exposure.max_net_exposure_pct, "净敞口"],
          ].map(([key, value, label]) => (
            <div key={String(key)} className="min-w-0 px-4 py-5">
              <div className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground">{key}</div>
              <strong className="mt-2 block font-mono text-xl tabular-nums text-[#10243e]">{Number(value).toFixed(0)}%</strong>
              <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
              <div className="mt-3 h-1 overflow-hidden bg-slate-100"><span className={cn("block h-full", key === "SHORT" ? "bg-[#c83e4c]" : "bg-[#2866ae]")} style={{ width: `${Math.min(Number(value) / 1.5, 100)}%` }} /></div>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-7 px-4 py-5 sm:px-6">
        <div>
          <h4 className="mb-3 font-mono text-[11px] font-semibold tracking-[0.12em] text-muted-foreground">EXPOSURE LIMITS</h4>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            <PolicyNumberField label="最多持仓" value={exposure.max_positions} minimum={1} maximum={10} suffix="只" onChange={exposureField("max_positions")} />
            <PolicyNumberField label="总敞口" value={exposure.max_gross_exposure_pct} maximum={150} onChange={exposureField("max_gross_exposure_pct")} />
            <PolicyNumberField label="净敞口" value={exposure.max_net_exposure_pct} maximum={120} onChange={exposureField("max_net_exposure_pct")} />
            <PolicyNumberField label="多头敞口" value={exposure.max_long_exposure_pct} maximum={120} onChange={exposureField("max_long_exposure_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="空头敞口" value={exposure.max_short_exposure_pct} maximum={30} onChange={exposureField("max_short_exposure_pct")} />
            <PolicyNumberField label="单只多头" value={exposure.max_long_position_pct} maximum={15} onChange={exposureField("max_long_position_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="单只空头" value={exposure.max_short_position_pct} maximum={5} onChange={exposureField("max_short_position_pct")} />
          </div>
        </div>

        <div>
          <h4 className="mb-3 font-mono text-[11px] font-semibold tracking-[0.12em] text-muted-foreground">MARGIN &amp; CARRY</h4>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            <PolicyNumberField label="维持保证金率" value={margin.maintenance_margin_pct} minimum={0.01} maximum={100} onChange={marginField("maintenance_margin_pct")} />
            <PolicyNumberField label="清算缓冲" value={margin.liquidation_buffer_pct} maximum={100} onChange={marginField("liquidation_buffer_pct")} />
            <PolicyNumberField label="融资年化" value={margin.financing_apr_pct} maximum={100} step={0.1} onChange={marginField("financing_apr_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="借券年化估算" value={short.estimated_borrow_apr_pct} maximum={100} step={0.1} onChange={shortField("estimated_borrow_apr_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="成本压力倍数" value={short.cost_stress_multiplier} minimum={1} maximum={100} suffix="×" step={0.1} onChange={shortField("cost_stress_multiplier")} />
          </div>
        </div>

        <div className={cn(shortDisabled && "opacity-50")}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h4 className="font-mono text-[11px] font-semibold tracking-[0.12em] text-muted-foreground">SHORT RISK ONLY</h4>
            {shortDisabled && <span className="text-xs text-muted-foreground">切换到 LONG_SHORT 后可配置</span>}
          </div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            <PolicyNumberField disabled={shortDisabled} label="空头止损" value={short.stop_loss_pct} minimum={0.01} maximum={100} step={0.5} onChange={shortField("stop_loss_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="追踪止盈激活" value={short.trailing_activation_pct} minimum={0.01} maximum={100} step={0.5} onChange={shortField("trailing_activation_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="追踪反弹退出" value={short.trailing_rebound_pct} minimum={0.01} maximum={100} step={0.5} onChange={shortField("trailing_rebound_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="事件禁入窗口" value={short.event_blackout_sessions} maximum={252} suffix="场" onChange={shortField("event_blackout_sessions")} />
            <PolicyNumberField disabled={shortDisabled} label="逼空单日涨幅" value={short.squeeze_rise_pct} minimum={0.01} maximum={100} step={0.5} onChange={shortField("squeeze_rise_pct")} />
            <PolicyNumberField disabled={shortDisabled} label="逼空量比" value={short.squeeze_volume_ratio} minimum={0.01} maximum={100} suffix="×" step={0.1} onChange={shortField("squeeze_volume_ratio")} />
            <PolicyNumberField disabled={shortDisabled} label="20日波动率" value={short.maximum_volatility_20d_pct} minimum={0.01} maximum={1000} step={1} onChange={shortField("maximum_volatility_20d_pct")} />
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-3">
            {([
              ["require_shortable", "必须可卖空", short.require_shortable],
              ["require_easy_to_borrow", "必须易借券", short.require_easy_to_borrow],
              ["block_on_borrow_data_missing", "缺少借券数据时阻断", short.block_on_borrow_data_missing],
            ] as [keyof ShortPolicy, string, boolean][]).map(([key, label, checked]) => (
              <label key={String(key)} className="flex items-center justify-between gap-3 border border-slate-200 px-3 py-3 text-sm">
                <span>{label}</span>
                <Switch disabled={shortDisabled} checked={Boolean(checked)} onCheckedChange={(value) => onShortChange({ [key]: value } as Partial<ShortPolicy>)} />
              </label>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

function DeliverySettings({
  delivery,
  isActive,
  dirty,
  syncStatus,
  onChange,
  onSync,
  syncing,
}: {
  delivery: ReportDelivery
  isActive: boolean
  dirty: boolean
  syncStatus: ConfigPayload["delivery_sync"] | null
  onChange: (patch: Partial<ReportDelivery>) => void
  onSync: () => void
  syncing: boolean
}) {
  const needsTarget = ["feishu", "telegram", "discord", "signal"].includes(delivery.channel)
  return (
    <section className="max-w-4xl overflow-hidden rounded-lg border bg-background shadow-xs">
      <div className="flex flex-col justify-between gap-4 border-b px-5 py-4 sm:flex-row sm:items-center sm:px-6">
        <div className="flex items-start gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary"><BellRing className="size-4" /></div>
          <div><h3 className="text-sm font-semibold">定时推送</h3><p className="mt-1 text-xs text-muted-foreground">{deliverySummary(delivery)}</p></div>
        </div>
        <Switch aria-label="启用报告推送" checked={delivery.enabled} onCheckedChange={(enabled) => onChange({ enabled })} />
      </div>

      <div className={cn("divide-y", !delivery.enabled && "opacity-55")}>
        <div className="grid gap-4 px-5 py-5 sm:grid-cols-[180px_1fr] sm:px-6">
          <div><p className="text-sm font-medium">接收渠道</p><p className="mt-1 text-xs leading-5 text-muted-foreground">报告完成后的投递位置</p></div>
          <div className="grid gap-3 sm:grid-cols-[180px_1fr]">
            <Select disabled={!delivery.enabled} value={delivery.channel} onValueChange={(channel: ReportDelivery["channel"]) => onChange({ channel })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>{Object.entries(channelCopy).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
            </Select>
            <Input disabled={!delivery.enabled || !needsTarget} value={delivery.target} onChange={(event) => onChange({ target: event.target.value })} placeholder={needsTarget ? "接收会话或频道 ID" : "无需填写接收目标"} />
          </div>
        </div>

        <div className="grid gap-4 px-5 py-5 sm:grid-cols-[180px_1fr] sm:px-6">
          <div><p className="text-sm font-medium">推送时间</p><p className="mt-1 text-xs leading-5 text-muted-foreground">使用北京时间</p></div>
          <div className="grid max-w-md gap-3 sm:grid-cols-2">
            <Select disabled={!delivery.enabled} value={delivery.frequency} onValueChange={(frequency: ReportDelivery["frequency"]) => onChange({ frequency })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="daily">每天</SelectItem><SelectItem value="weekdays">仅工作日</SelectItem></SelectContent>
            </Select>
            <Input
              disabled={!delivery.enabled}
              type="time"
              value={deliveryTime(delivery)}
              onChange={(event) => {
                const [hour, minute] = event.target.value.split(":").map(Number)
                if (Number.isFinite(hour) && Number.isFinite(minute)) onChange({ hour, minute })
              }}
            />
          </div>
        </div>

        <div className="grid gap-4 px-5 py-5 sm:grid-cols-[180px_1fr] sm:px-6">
          <div><p className="text-sm font-medium">推送范围</p><p className="mt-1 text-xs leading-5 text-muted-foreground">控制非正常报告是否通知</p></div>
          <div className="space-y-4">
            <label className="flex items-center justify-between gap-4"><span><span className="block text-sm">无匹配股票</span><span className="mt-1 block text-xs text-muted-foreground">没有候选时仍发送结果</span></span><Switch disabled={!delivery.enabled} checked={delivery.push_on_empty} onCheckedChange={(push_on_empty) => onChange({ push_on_empty })} /></label>
            <label className="flex items-center justify-between gap-4"><span><span className="block text-sm">数据或模型异常</span><span className="mt-1 block text-xs text-muted-foreground">执行失败或数据不完整时发送告警</span></span><Switch disabled={!delivery.enabled} checked={delivery.push_on_error} onCheckedChange={(push_on_error) => onChange({ push_on_error })} /></label>
          </div>
        </div>
      </div>

      <div className="flex flex-col justify-between gap-3 border-t bg-muted/30 px-5 py-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:px-6">
        <span>{!isActive ? "启用策略后同步到 Hermes" : dirty ? "保存配置后同步到 Hermes" : !delivery.enabled ? "报告推送已关闭" : syncStatus?.message || "已由定时任务读取"}</span>
        {isActive && !dirty && <Button size="sm" variant="ghost" disabled={syncing} onClick={onSync}><BellRing />{syncing ? "同步中" : "重新同步"}</Button>}
      </div>
    </section>
  )
}

function LoadingState() {
  return <div className="grid min-h-screen place-items-center bg-muted/30"><div className="flex items-center gap-3 text-sm text-muted-foreground"><Activity className="size-4 animate-pulse" />读取参数配置</div></div>
}

function Dashboard({ initialData, isActive, onBack }: { initialData: ConfigPayload; isActive: boolean; onBack: () => void }) {
  const queryClient = useQueryClient()
  const [parameters, setParameters] = useState<Parameter[]>(initialData.parameters)
  const [config, setConfig] = useState<ConfigPayload["config"]>(initialData.config)
  const [activeGroup, setActiveGroup] = useState("universe")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")
  const [search, setSearch] = useState("")
  const [dirty, setDirty] = useState(false)
  const [draftGeneration, setDraftGeneration] = useState(0)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [deliverySync, setDeliverySync] = useState<ConfigPayload["delivery_sync"] | null>(initialData.delivery_sync || null)
  const [dataSourceStatus, setDataSourceStatus] = useState(initialData.us_market_data)

  const markDirty = () => {
    setDirty(true)
    setDraftGeneration((current) => current + 1)
  }

  const updateParameter = (id: string, patch: Partial<Parameter>) => {
    setParameters((current) => current.map((item) => item.id === id ? { ...item, ...patch, effective: (patch.enabled ?? item.enabled) && item.status !== "planned" } : item))
    markDirty()
  }

  const updateDelivery = (patch: Partial<ReportDelivery>) => {
    setConfig((current) => ({ ...current, delivery: { ...current.delivery, ...patch } }))
    markDirty()
  }

  const updatePortfolio = (patch: Partial<PortfolioConfig>) => {
    setConfig((current) => ({ ...current, portfolio: { ...current.portfolio, ...patch } }))
    markDirty()
  }

  const updateAllocation = (patch: Partial<AllocationConfig>) => {
    setConfig((current) => ({ ...current, allocation: { ...current.allocation, ...patch } }))
    markDirty()
  }

  const updateExposurePolicy = (patch: Partial<ExposurePolicy>) => {
    setConfig((current) => ({ ...current, exposure_policy: { ...current.exposure_policy, ...patch } }))
    markDirty()
  }

  const updateMarginPolicy = (patch: Partial<MarginPolicy>) => {
    setConfig((current) => ({ ...current, margin_policy: { ...current.margin_policy, ...patch } }))
    markDirty()
  }

  const updateShortPolicy = (patch: Partial<ShortPolicy>) => {
    setConfig((current) => ({ ...current, short_policy: { ...current.short_policy, ...patch } }))
    markDirty()
  }

  const activeCount = parameters.filter((item) => item.enabled).length
  const marketCode = String(parameters.find((item) => item.id === "market")?.value || "cn") === "us" ? "us" : "cn"
  const dataSourceParameter = parameters.find((item) => item.id === "us_data_source")
  const isParameterApplicable = (item: Parameter) => (
    marketCode !== "us"
    || (item.applicable !== false && !usInapplicableParameters.has(item.id))
  )
  const effectiveCount = parameters.filter((item) => isParameterApplicable(item) && item.effective).length
  const plannedActiveCount = parameters.filter((item) => isParameterApplicable(item) && item.enabled && item.status === "planned").length
  const availableCount = parameters.filter((item) => isParameterApplicable(item) && item.status !== "planned").length
  const visibleParameters = useMemo(() => parameters.filter((item) => {
    if (item.id === "us_data_source") return false
    if (marketCode === "us" && (item.applicable === false || usInapplicableParameters.has(item.id))) return false
    if (activeGroup !== "all" && item.group !== activeGroup) return false
    if (statusFilter === "enabled" && !item.enabled) return false
    if (statusFilter === "available" && item.status === "planned") return false
    if (statusFilter === "planned" && item.status !== "planned") return false
    const needle = search.trim().toLowerCase()
    return !needle || `${item.label} ${item.description} ${item.id}`.toLowerCase().includes(needle)
  }).map((item) => {
    if (marketCode !== "us") return item
    if (["price_min", "price_max"].includes(item.id)) return { ...item, unit: "$" }
    if (["turnover_min", "float_market_cap_min", "float_market_cap_max", "total_market_cap_min", "total_market_cap_max"].includes(item.id)) return { ...item, unit: "亿美元" }
    return item
  }), [parameters, activeGroup, statusFilter, search, marketCode])

  const states = Object.fromEntries(parameters.map((item) => [item.id, { enabled: item.enabled, value: item.value }]))
  const saveFlow = useStrategySaveFlow({
    config: { ...config, parameters: states },
    market: marketCode,
    draftGeneration,
    save: (payload) => saveStrategy(config.id!, payload),
    onSaved: (payload, reconciliation) => {
      if (reconciliation.draftChanged) {
        setConfig((current) => ({
          ...current,
          id: payload.config.id,
          revision: payload.config.revision,
          lifecycle: payload.config.lifecycle,
          created_at: payload.config.created_at,
          updated_at: payload.config.updated_at,
        }))
      } else {
        setParameters(payload.parameters)
        setConfig(payload.config)
        setDirty(false)
      }
      setDeliverySync(payload.delivery_sync || null)
      setDataSourceStatus(payload.us_market_data)
      queryClient.setQueryData(["strategy", config.id], payload)
      queryClient.invalidateQueries({ queryKey: ["strategies"] })
      toast.success("策略配置已保存", { description: reconciliation.draftChanged ? "保存期间有新的修改，请再次保存后再运行" : isActive ? payload.delivery_sync?.message || "下一次选股会读取这份策略" : "启用策略后生效" })
      if (payload.delivery_sync?.status === "error") toast.error("报告推送同步失败", { description: payload.delivery_sync.message })
    },
  })

  const saveIfConfirmed = () => {
    void saveFlow.save().catch((error: Error) => toast.error(error.message))
  }

  const saveIfConfirmedAsync = () => saveFlow.beforeRun()

  const resetMutation = useMutation({
    mutationFn: () => resetStrategy(config.id!),
    onSuccess: (payload) => {
      setParameters(payload.parameters)
      setConfig(payload.config)
      setDeliverySync(payload.delivery_sync || null)
      setDataSourceStatus(payload.us_market_data)
      saveFlow.commitBaseline(payload.config.exposure_policy.mode)
      setDirty(false)
      queryClient.setQueryData(["strategy", config.id], payload)
      queryClient.invalidateQueries({ queryKey: ["strategies"] })
      toast.success("已恢复默认参数")
    },
    onError: (error) => toast.error(error.message),
  })

  const activateMutation = useMutation({
    mutationFn: () => activateStrategy(config.id!),
    onSuccess: (library) => {
      queryClient.setQueryData(["strategies"], library)
      setDeliverySync(library.delivery_sync || null)
      toast.success("策略已启用", { description: library.delivery_sync?.message || "下一次选股任务将使用这条策略" })
      if (library.delivery_sync?.status === "error") toast.error("报告推送同步失败", { description: library.delivery_sync.message })
    },
    onError: (error) => toast.error(error.message),
  })

  const syncDeliveryMutation = useMutation({
    mutationFn: () => syncStrategyDelivery(config.id!),
    onSuccess: ({ delivery_sync }) => {
      setDeliverySync(delivery_sync)
      if (delivery_sync.status === "error") toast.error("报告推送同步失败", { description: delivery_sync.message })
      else toast.success(delivery_sync.message)
    },
    onError: (error) => toast.error(error.message),
  })

  const applyDraft = (draft: StrategyDraft) => {
    setParameters((current) => current.map((item) => {
      const update = draft.updates.find((candidate) => candidate.id === item.id)
      return update ? { ...item, enabled: true, value: update.value, effective: item.status !== "planned" } : item
    }))
    markDirty()
    toast.success(`已应用 ${draft.updates.length} 项草案`, { description: "检查后点击保存参数" })
  }

  const groups = initialData.groups
  const navItems = [{ id: "all", label: "全部参数", description: "完整参数目录" }, ...groups, { id: "exposure", label: "多空与杠杆", description: "敞口、保证金与借券" }, { id: "portfolio", label: "持仓 Pipeline", description: "组合、退出与风控" }, { id: "delivery", label: "报告推送", description: "定时与接收渠道" }]
  const deliveryView = activeGroup === "delivery"
  const exposureView = activeGroup === "exposure"
  const portfolioView = activeGroup === "portfolio"
  const settingsView = deliveryView || exposureView || portfolioView

  return (
    <div className="min-h-screen bg-muted/25 text-foreground">
      <aside className={cn("fixed inset-y-0 left-0 z-40 w-64 border-r bg-sidebar transition-transform lg:translate-x-0", mobileNavOpen ? "translate-x-0" : "-translate-x-full")}>
        <button className="flex h-16 w-full items-center gap-3 border-b px-5 text-left" onClick={onBack}>
          <div className="grid size-8 place-items-center rounded-md bg-primary text-primary-foreground"><Activity className="size-4" /></div>
          <div className="flex-1"><div className="text-sm font-semibold">Stock Agent</div><div className="text-[11px] text-muted-foreground">返回策略库</div></div>
          <ArrowLeft className="size-4 text-muted-foreground" />
        </button>
        <nav className="p-3">
          <p className="px-2 pb-2 pt-1 text-[11px] font-medium text-muted-foreground">策略配置</p>
          {navItems.map((group) => {
            const special = group.id === "delivery" || group.id === "exposure" || group.id === "portfolio"
            const count = special ? 0 : group.id === "all" ? parameters.length : parameters.filter((item) => item.group === group.id).length
            const enabled = special ? 0 : group.id === "all" ? activeCount : parameters.filter((item) => item.group === group.id && item.enabled).length
            return (
              <button key={group.id} className={cn("mb-0.5 flex w-full items-center justify-between rounded-md px-2.5 py-2 text-left text-sm transition-colors", activeGroup === group.id ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground" : "text-sidebar-foreground/75 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground")} onClick={() => { setActiveGroup(group.id); setMobileNavOpen(false) }}>
                <span>{group.label}</span>{group.id === "delivery" ? <BellRing className={cn("size-3.5", config.delivery.enabled ? "text-emerald-600" : "text-muted-foreground")} /> : group.id === "exposure" ? <SlidersHorizontal className="size-3.5 text-primary" /> : group.id === "portfolio" ? <Database className={cn("size-3.5", config.portfolio.enabled ? "text-emerald-600" : "text-muted-foreground")} /> : <span className="font-mono text-[10px] text-muted-foreground">{enabled}/{count}</span>}
              </button>
            )
          })}
        </nav>
        <div className="absolute inset-x-4 bottom-4 rounded-md border bg-background p-3">
          <div className="mb-2 flex items-center justify-between text-xs"><span className="text-muted-foreground">数据接入</span><span className="font-mono">{availableCount}/{parameters.length}</span></div>
          <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
            <span className="bg-emerald-500" style={{ width: `${parameters.length ? parameters.filter((item) => item.status === "live").length / parameters.length * 100 : 0}%` }} />
            <span className="bg-sky-500" style={{ width: `${parameters.length ? parameters.filter((item) => item.status === "derived").length / parameters.length * 100 : 0}%` }} />
            <span className="flex-1 bg-amber-400" />
          </div>
          <div className="mt-2 flex items-center gap-3 text-[10px] text-muted-foreground"><span>直接</span><span>计算</span><span>待接入</span></div>
        </div>
      </aside>
      {mobileNavOpen && <button className="fixed inset-0 z-30 bg-black/20 lg:hidden" aria-label="关闭菜单" onClick={() => setMobileNavOpen(false)} />}

      <div className="lg:pl-64">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between border-b bg-background/95 px-4 backdrop-blur sm:px-6">
          <div className="flex min-w-0 items-center gap-2 sm:gap-3">
            <Button size="icon" variant="ghost" className="lg:hidden" onClick={() => setMobileNavOpen(true)}><Menu /></Button>
            <Button size="icon" variant="ghost" className="hidden lg:inline-flex" onClick={onBack}><ArrowLeft /><span className="sr-only">返回策略库</span></Button>
            <div className="min-w-0"><div className="flex items-center gap-2"><h1 className="truncate text-sm font-semibold">{config.name}</h1>{isActive && <Badge variant="success" className="hidden sm:inline-flex">使用中</Badge>}</div><p className="truncate text-xs text-muted-foreground">{config.signal.model} · {config.signal.run_time} · {dirty ? "有未保存修改" : config.updated_at ? `更新于 ${new Date(config.updated_at).toLocaleString("zh-CN")}` : "尚未保存"}</p></div>
          </div>
          <div className="flex items-center gap-1 sm:gap-2">
            {!isActive && <Button variant="outline" size="sm" disabled={activateMutation.isPending} onClick={() => activateMutation.mutate()}><ShieldCheck />启用策略</Button>}
            <Button variant="outline" size="sm" className="hidden sm:inline-flex" disabled={resetMutation.isPending} onClick={() => window.confirm("恢复默认参数？") && resetMutation.mutate()}><RotateCcw />恢复默认</Button>
            <StrategyRunSheet strategyId={config.id!} strategyName={config.name} compact pendingChanges={dirty} beforeRun={saveIfConfirmedAsync} />
            <StrategyMapper strategyId={config.id!} parameters={parameters} onApply={applyDraft} />
            <Button size="sm" className="size-8 px-0 sm:w-auto sm:px-3" disabled={!dirty || saveFlow.isPending} onClick={saveIfConfirmed}><Save /><span className="hidden sm:inline">{saveFlow.isPending ? "保存中" : "保存配置"}</span><span className="sr-only sm:hidden">保存配置</span></Button>
          </div>
        </header>

        <main className="mx-auto max-w-[1500px] px-4 py-6 sm:px-6 lg:px-8">
          <section className="mb-6 border-b pb-6">
            <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
              <div><div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground"><ShieldCheck className="size-3.5" />{isActive ? "当前使用" : "未使用"}</div><h2 className="text-xl font-semibold tracking-tight">{deliveryView ? "报告推送" : exposureView ? "多空与杠杆" : portfolioView ? "持仓 Pipeline" : "筛选参数"}</h2><p className="mt-1.5 text-sm text-muted-foreground">{deliveryView ? "配置报告的推送时间、渠道和通知范围。" : exposureView ? "定义策略的多空模式、敞口硬边界、保证金与借券风险。" : portfolioView ? "配置每个策略独立的模拟组合、退出规则、回撤闸门和成交成本。" : "已接入参数会直接参与过滤；待接入参数用于保存下一版策略意图。"}</p></div>
              {!settingsView && <div className="grid grid-cols-3 divide-x rounded-lg border bg-background px-1 shadow-xs">
                <div className="px-4 py-2.5"><div className="font-mono text-lg font-semibold tabular-nums">{activeCount}</div><div className="text-[11px] text-muted-foreground">已启用</div></div>
                <div className="px-4 py-2.5"><div className="font-mono text-lg font-semibold tabular-nums text-emerald-700">{effectiveCount}</div><div className="text-[11px] text-muted-foreground">实际生效</div></div>
                <div className="px-4 py-2.5"><div className="font-mono text-lg font-semibold tabular-nums text-amber-700">{plannedActiveCount}</div><div className="text-[11px] text-muted-foreground">等待数据</div></div>
              </div>}
            </div>
          </section>

          {exposureView ? (
            <LongShortPolicySettings market={marketCode} exposure={config.exposure_policy} margin={config.margin_policy} short={config.short_policy} onExposureChange={updateExposurePolicy} onMarginChange={updateMarginPolicy} onShortChange={updateShortPolicy} />
          ) : portfolioView ? (
            <PortfolioSettings portfolio={config.portfolio} allocation={config.allocation} strategyId={config.id!} market={marketCode} onChange={updatePortfolio} onAllocationChange={updateAllocation} />
          ) : deliveryView ? (
            <DeliverySettings
              delivery={config.delivery}
              isActive={isActive}
              dirty={dirty}
              syncStatus={deliverySync}
              onChange={updateDelivery}
              onSync={() => syncDeliveryMutation.mutate()}
              syncing={syncDeliveryMutation.isPending}
            />
          ) : <section>
            {marketCode === "us" && dataSourceParameter && (activeGroup === "all" || activeGroup === "universe") && (
              <DataSourceSettings
                parameter={dataSourceParameter}
                status={dataSourceStatus}
                onChange={(value) => updateParameter("us_data_source", { enabled: true, value })}
              />
            )}
            <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
              <div className="relative w-full max-w-md"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input className="pl-9" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索参数名称、说明或参数键" /></div>
              <div className="flex items-center gap-2 overflow-x-auto pb-1 xl:pb-0">
                <Filter className="hidden size-4 text-muted-foreground sm:block" />
                {([
                  ["all", "全部"], ["enabled", "已启用"], ["available", "已接入"], ["planned", "待接入"],
                ] as [StatusFilter, string][]).map(([value, label]) => <Button key={value} size="sm" variant={statusFilter === value ? "secondary" : "ghost"} onClick={() => setStatusFilter(value)}>{label}</Button>)}
                <div className="mx-1 h-5 w-px bg-border" />
                <Badge variant="outline" className="h-8 px-3 font-mono">{visibleParameters.length} rows</Badge>
              </div>
            </div>
            <ParameterTable parameters={visibleParameters} onUpdate={updateParameter} />
            <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5"><Database className="size-3.5 text-emerald-600" />已接入：数据源直接提供</span>
              <span className="flex items-center gap-1.5"><Settings2 className="size-3.5 text-sky-600" />可计算：由当前行情推导</span>
              <span className="flex items-center gap-1.5"><Clock3 className="size-3.5 text-amber-600" />待接入：保存但不执行</span>
            </div>
          </section>}
        </main>
      </div>
    </div>
  )
}

function StrategyLibraryView({ library, onEdit }: { library: StrategyLibrary; onEdit: (id: string) => void }) {
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(true)
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const liveStrategy = library.strategies.find((strategy) => strategy.is_active)

  const refreshLibrary = (next?: StrategyLibrary) => {
    if (next) queryClient.setQueryData(["strategies"], next)
    else queryClient.invalidateQueries({ queryKey: ["strategies"] })
  }
  const createMutation = useMutation({
    mutationFn: () => createStrategy(name.trim(), description.trim()),
    onSuccess: (payload) => {
      refreshLibrary()
      toast.success("策略已创建")
      onEdit(payload.config.id!)
    },
    onError: (error) => toast.error(error.message),
  })
  const usageMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => enabled ? activateStrategy(id) : deactivateStrategy(id),
    onSuccess: (next, variables) => {
      refreshLibrary(next)
      toast.success(variables.enabled ? "已切换使用策略" : "已停止使用策略", { description: next.delivery_sync?.message })
      if (next.delivery_sync?.status === "error") toast.error("报告推送同步失败", { description: next.delivery_sync.message })
    },
    onError: (error) => toast.error(error.message),
  })
  const duplicateMutation = useMutation({
    mutationFn: duplicateStrategy,
    onSuccess: (payload) => { refreshLibrary(); toast.success("策略已复制"); onEdit(payload.config.id!) },
    onError: (error) => toast.error(error.message),
  })
  const deleteMutation = useMutation({
    mutationFn: deleteStrategy,
    onSuccess: (next) => { refreshLibrary(next); toast.success("策略已删除") },
    onError: (error) => toast.error(error.message),
  })

  return (
    <div className="min-h-screen bg-muted/25">
      <header className="flex h-16 items-center justify-between border-b bg-background px-4 sm:px-8">
        <div className="flex items-center gap-3">
          <div className="grid size-8 place-items-center rounded-md bg-primary text-primary-foreground"><Activity className="size-4" /></div>
          <div><h1 className="text-sm font-semibold">Stock Agent</h1><p className="text-[11px] text-muted-foreground">策略配置中心</p></div>
        </div>
        <Button onClick={() => setCreating(true)}><Plus />新建策略</Button>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-8">
        <div className="mb-7 flex items-end justify-between border-b pb-6">
          <div><p className="mb-2 text-xs font-medium text-muted-foreground">STRATEGY LIBRARY</p><h2 className="text-2xl font-semibold tracking-tight">策略库</h2></div>
          <div className="text-right"><div className="text-sm font-semibold">{liveStrategy?.name || "尚未设置"}</div><div className="mt-1 text-xs text-muted-foreground">当前使用 · 共 {library.strategies.length} 个策略</div></div>
        </div>

        {creating && (
          <section className="mb-8 rounded-lg border bg-background p-5 shadow-xs">
            <div className="mb-5 flex items-center justify-between"><div><h3 className="font-semibold">创建策略</h3><p className="mt-1 text-xs text-muted-foreground">创建后配置参数和推送，确认后再启用</p></div>{library.strategies.length > 0 && <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>取消</Button>}</div>
            <div className="grid gap-4 sm:grid-cols-[1fr_1.4fr]">
              <div><label className="mb-2 block text-sm font-medium" htmlFor="strategy-name">策略名称</label><Input id="strategy-name" autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：低估值动量" /></div>
              <div><label className="mb-2 block text-sm font-medium" htmlFor="strategy-description">策略说明</label><Input id="strategy-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="可选" /></div>
            </div>
            <div className="mt-5 flex justify-end"><Button disabled={!name.trim() || createMutation.isPending} onClick={() => createMutation.mutate()}><SlidersHorizontal />{createMutation.isPending ? "创建中" : "创建并配置参数"}</Button></div>
          </section>
        )}

        <section className="space-y-3">
          {library.strategies.map((strategy) => (
            <StrategyRow
              key={strategy.id}
              strategy={strategy}
              onEdit={() => onEdit(strategy.id)}
              onUsageChange={(enabled) => usageMutation.mutate({ id: strategy.id, enabled })}
              usagePending={usageMutation.isPending}
              onDuplicate={() => duplicateMutation.mutate(strategy.id)}
              onDelete={() => window.confirm(`删除策略“${strategy.name}”？`) && deleteMutation.mutate(strategy.id)}
            />
          ))}
          {!library.strategies.length && !creating && <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">暂无策略</div>}
        </section>
      </main>
    </div>
  )
}

function StrategyRow({ strategy, onEdit, onUsageChange, usagePending, onDuplicate, onDelete }: { strategy: StrategySummary; onEdit: () => void; onUsageChange: (enabled: boolean) => void; usagePending: boolean; onDuplicate: () => void; onDelete: () => void }) {
  return (
    <div className="flex flex-col gap-4 rounded-lg border bg-background p-4 shadow-xs sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <div className={cn("mt-0.5 grid size-9 shrink-0 place-items-center rounded-md", strategy.is_active ? "bg-emerald-50 text-emerald-700" : "bg-muted text-muted-foreground")}><SlidersHorizontal className="size-4" /></div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><h3 className="truncate font-medium">{strategy.name}</h3><Badge variant="outline">{strategy.market.label}</Badge><Badge variant={strategy.lifecycle.stage === "live" ? "success" : strategy.lifecycle.stage === "paper" ? "info" : "secondary"}>{lifecycleCopy[strategy.lifecycle.stage]}</Badge><span className="font-mono text-[10px] text-muted-foreground">v{strategy.revision}</span></div>
          <p className="mt-1 truncate text-xs text-muted-foreground">{strategy.description || "暂无说明"}</p>
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">{strategy.signal.model} · {strategy.signal.run_time} · 前一交易日收盘数据</p>
        </div>
      </div>
      <div className="flex items-center gap-5 text-xs text-muted-foreground"><span><strong className="mr-1 font-mono text-foreground">{strategy.active_parameters}</strong>参数</span><span className="hidden items-center gap-1.5 lg:flex"><BellRing className="size-3.5" />{deliverySummary(strategy.delivery)}</span><span>{strategy.updated_at ? new Date(strategy.updated_at).toLocaleDateString("zh-CN") : "未更新"}</span></div>
      <div className="flex w-full flex-wrap items-center justify-end gap-1 self-stretch sm:w-auto sm:flex-nowrap sm:self-auto">
        <label className="mb-2 flex w-full items-center justify-between rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground sm:mb-0 sm:mr-1 sm:w-auto sm:justify-start sm:border-0 sm:bg-transparent sm:px-0 sm:py-0" title={strategy.is_active ? "停止使用此策略" : "使用此策略"}>
          <span className="whitespace-nowrap">是否使用</span>
          <Switch aria-label={`是否使用策略 ${strategy.name}`} checked={strategy.is_active} disabled={usagePending} onCheckedChange={onUsageChange} />
        </label>
        <StrategyRunSheet strategyId={strategy.id} strategyName={strategy.name} buttonVariant="ghost" />
        <Button size="sm" variant="outline" onClick={onEdit}>配置参数</Button>
        <Button size="icon" variant="ghost" title="复制策略" onClick={onDuplicate}><Copy /><span className="sr-only">复制策略</span></Button>
        <Button size="icon" variant="ghost" title="删除策略" className="text-muted-foreground hover:text-destructive" onClick={onDelete}><Trash2 /><span className="sr-only">删除策略</span></Button>
      </div>
    </div>
  )
}

function StrategyEditor({ strategyId, library, onBack }: { strategyId: string; library: StrategyLibrary; onBack: () => void }) {
  const query = useQuery({ queryKey: ["strategy", strategyId], queryFn: () => getStrategy(strategyId) })
  if (query.isLoading) return <LoadingState />
  if (query.isError) return <div className="grid min-h-screen place-items-center"><div className="text-center"><p className="font-medium">策略加载失败</p><p className="mt-2 text-sm text-muted-foreground">{query.error.message}</p><Button className="mt-4" variant="outline" onClick={onBack}>返回策略库</Button></div></div>
  if (!query.data) return <LoadingState />
  return <Dashboard key={query.data.config.updated_at || strategyId} initialData={query.data} isActive={library.active_strategy_id === strategyId} onBack={onBack} />
}

function App() {
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(null)
  const query = useQuery({ queryKey: ["strategies"], queryFn: getStrategies })
  if (query.isLoading) return <LoadingState />
  if (query.isError) return <div className="grid min-h-screen place-items-center"><div className="text-center"><p className="font-medium">策略库加载失败</p><p className="mt-2 text-sm text-muted-foreground">{query.error.message}</p><Button className="mt-4" variant="outline" onClick={() => query.refetch()}>重新加载</Button></div></div>
  if (!query.data) return <LoadingState />
  if (selectedStrategyId) return <StrategyEditor strategyId={selectedStrategyId} library={query.data} onBack={() => setSelectedStrategyId(null)} />
  return <StrategyLibraryView library={query.data} onEdit={setSelectedStrategyId} />
}

export default App
