import { loadPerformance } from "@/performance/render"

const root = document.querySelector<HTMLElement>("#app")
const parts = location.pathname.split("/").filter(Boolean)
const strategyId = parts.length === 3 && parts[0] === "strategies" && parts[2] === "portfolio"
  ? decodeURIComponent(parts[1])
  : ""

function showError(message: string) {
  if (!root) return
  root.replaceChildren()
  const container = document.createElement("div")
  container.className = "error"
  const title = document.createElement("strong")
  title.textContent = "策略表现加载失败"
  container.append(title, document.createElement("br"), document.createTextNode(message))
  root.append(container)
}

if (!root) {
  throw new Error("performance root is missing")
} else if (!strategyId) {
  showError("URL 缺少策略 ID")
} else {
  void loadPerformance(strategyId, root)
    .then((payload) => { document.title = `${payload.strategy.name}表现 · Stock Agent` })
    .catch((error: Error) => showError(error.message))
}
