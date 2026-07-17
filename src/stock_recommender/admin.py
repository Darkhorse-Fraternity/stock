from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backtest import BacktestInProgressError, get_backtest, list_backtests, start_backtest
from .delivery import pause_hermes_delivery, sync_active_strategy_delivery, sync_hermes_delivery
from .parameters import (
    PARAMETER_CATALOG,
    activate_strategy,
    catalog_payload,
    create_strategy,
    create_strategy_revision,
    deactivate_strategy,
    delete_strategy,
    duplicate_strategy,
    convert_strategy_text,
    default_strategy_config,
    find_strategy_config,
    load_strategy_config,
    load_strategy_store,
    save_strategy_config,
    StrategyLifecycleError,
    strategy_library_payload,
    transition_strategy_stage,
)
from .performance import build_recommendation_performance
from .strategy_chat import chat_strategy
from .strategy_runs import StrategyRunInProgressError, get_strategy_run, list_strategy_runs, start_strategy_run


WEB_ROOT = Path(__file__).with_name("web")
MAX_REQUEST_BYTES = 1_000_000


def health_payload() -> dict:
    config = load_strategy_config()
    active = sum(1 for state in config["parameters"].values() if state.get("enabled"))
    effective = sum(
        1
        for item in PARAMETER_CATALOG
        if config["parameters"][item["id"]].get("enabled") and item["status"] in {"live", "derived"}
    )
    return {
        "status": "ok",
        "strategy": config["name"],
        "revision": config.get("revision", 1),
        "stage": config.get("lifecycle", {}).get("stage", "draft"),
        "approval_gate": config.get("validation", {}).get("approval_gate"),
        "active_parameters": active,
        "effective_parameters": effective,
    }


def _is_active_strategy(strategy_id: str | None) -> bool:
    return bool(strategy_id and load_strategy_store()["active_strategy_id"] == strategy_id)


def _catalog_with_delivery_sync(config: dict, *, previous: dict | None = None) -> dict:
    payload = catalog_payload(config)
    if _is_active_strategy(config.get("id")):
        if previous is not None and previous.get("delivery") == config.get("delivery"):
            payload["delivery_sync"] = {"status": "synced", "message": "报告推送配置未变更"}
        else:
            payload["delivery_sync"] = sync_hermes_delivery(config)
    return payload


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "StockAgentAdmin/1.0"

    def do_GET(self) -> None:
        request = urlparse(self.path)
        path = request.path
        if path == "/api/config":
            self._send_json(catalog_payload())
            return
        if path == "/api/strategies":
            self._send_json(strategy_library_payload())
            return
        strategy_id, action = self._strategy_route(path)
        if strategy_id and action is None:
            config = load_strategy_config(strategy_id=strategy_id)
            if config.get("id") != strategy_id:
                self._send_json({"error": "策略不存在"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(catalog_payload(config))
            return
        if path == "/api/health":
            self._send_json(health_payload())
            return
        if path == "/api/performance":
            try:
                days = int((parse_qs(request.query).get("days") or ["30"])[0])
            except ValueError:
                days = 30
            self._send_json(build_recommendation_performance(days=days))
            return
        run_id = self._run_route(path)
        if run_id:
            run = get_strategy_run(run_id)
            if run is None:
                self._send_json({"error": "运行记录不存在"}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json(run)
            return
        backtest_id = self._backtest_route(path)
        if backtest_id:
            backtest = get_backtest(backtest_id)
            if backtest is None:
                self._send_json({"error": "回测记录不存在"}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json(backtest)
            return
        if strategy_id and action == "runs":
            self._send_json({"runs": list_strategy_runs(strategy_id)})
            return
        if strategy_id and action == "backtests":
            self._send_json({"backtests": list_backtests(strategy_id)})
            return
        if path == "/":
            self._send_file(WEB_ROOT / "index.html")
            return
        if path in {"/performance", "/performance/"}:
            self._send_file(WEB_ROOT / "performance.html")
            return
        static_path = (WEB_ROOT / path.lstrip("/")).resolve()
        try:
            static_path.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if static_path.is_file():
            self._send_file(static_path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/strategy/convert":
            self._send_json(convert_strategy_text(body.get("strategy", "")))
            return
        if path == "/api/strategy/chat":
            try:
                strategy = load_strategy_config(strategy_id=body.get("strategy_id"))
                self._send_json(chat_strategy(body.get("messages"), strategy_name=strategy.get("name", "")))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/strategies":
            strategy = create_strategy(body.get("name", "新策略"), description=body.get("description", ""), activate=False)
            payload = catalog_payload(strategy)
            if _is_active_strategy(strategy.get("id")):
                payload["delivery_sync"] = sync_hermes_delivery(strategy)
            self._send_json(payload, status=HTTPStatus.CREATED)
            return
        strategy_id, action = self._strategy_route(path)
        try:
            if strategy_id and action == "activate":
                strategy = find_strategy_config(strategy_id)
                if strategy is None:
                    raise KeyError(strategy_id)
                if strategy["lifecycle"]["stage"] not in {"paper", "live"}:
                    raise StrategyLifecycleError("只有模拟盘或已批准实盘策略可以启用定时运行")
                delivery_sync = sync_hermes_delivery(strategy)
                if delivery_sync["status"] == "error":
                    self._send_json({"error": delivery_sync["message"], "delivery_sync": delivery_sync}, status=HTTPStatus.BAD_GATEWAY)
                    return
                activate_strategy(strategy_id)
                payload = strategy_library_payload()
                payload["delivery_sync"] = delivery_sync
                self._send_json(payload)
                return
            if strategy_id and action == "deactivate":
                if find_strategy_config(strategy_id) is None:
                    raise KeyError(strategy_id)
                if not _is_active_strategy(strategy_id):
                    payload = strategy_library_payload()
                    payload["delivery_sync"] = {"status": "synced", "message": "策略原本未使用"}
                    self._send_json(payload)
                    return
                delivery_sync = pause_hermes_delivery()
                if delivery_sync["status"] == "error":
                    self._send_json({"error": delivery_sync["message"], "delivery_sync": delivery_sync}, status=HTTPStatus.BAD_GATEWAY)
                    return
                deactivate_strategy(strategy_id)
                payload = strategy_library_payload()
                payload["delivery_sync"] = delivery_sync
                self._send_json(payload)
                return
            if strategy_id and action == "duplicate":
                self._send_json(catalog_payload(duplicate_strategy(strategy_id)), status=HTTPStatus.CREATED)
                return
            if strategy_id and action == "revision":
                self._send_json(catalog_payload(create_strategy_revision(strategy_id)), status=HTTPStatus.CREATED)
                return
            if strategy_id and action == "stage":
                updated = transition_strategy_stage(
                    strategy_id,
                    body.get("stage", ""),
                    approved_by=body.get("approved_by", ""),
                )
                self._send_json(catalog_payload(updated))
                return
            if strategy_id and action == "reset":
                existing = load_strategy_config(strategy_id=strategy_id)
                reset = default_strategy_config()
                reset.update({key: existing.get(key) for key in ["id", "name", "description", "created_at", "delivery"]})
                self._send_json(_catalog_with_delivery_sync(save_strategy_config(reset, strategy_id=strategy_id), previous=existing))
                return
            if strategy_id and action == "runs":
                self._send_json(start_strategy_run(strategy_id), status=HTTPStatus.ACCEPTED)
                return
            if strategy_id and action == "backtests":
                self._send_json(start_backtest(strategy_id), status=HTTPStatus.ACCEPTED)
                return
            if strategy_id and action == "sync-delivery":
                strategy = find_strategy_config(strategy_id)
                if strategy is None:
                    raise KeyError(strategy_id)
                self._send_json({"delivery_sync": sync_hermes_delivery(strategy)})
                return
        except KeyError:
            self._send_json({"error": "策略不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        except StrategyRunInProgressError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except BacktestInProgressError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except StrategyLifecycleError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        if path == "/api/config/reset":
            try:
                existing = load_strategy_config()
                reset = default_strategy_config()
                reset.update({key: existing.get(key) for key in ["id", "name", "description", "created_at", "delivery"]})
                saved = save_strategy_config(reset)
            except (ValueError, OSError, StrategyLifecycleError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            self._send_json(_catalog_with_delivery_sync(saved, previous=existing))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        strategy_id, action = self._strategy_route(path)
        if path != "/api/config" and not (strategy_id and action is None):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
            previous = load_strategy_config(strategy_id=strategy_id)
            saved = save_strategy_config(body, strategy_id=strategy_id)
        except (ValueError, OSError, StrategyLifecycleError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(_catalog_with_delivery_sync(saved, previous=previous))

    def do_DELETE(self) -> None:
        strategy_id, action = self._strategy_route(urlparse(self.path).path)
        if not strategy_id or action is not None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            delete_strategy(strategy_id)
        except KeyError:
            self._send_json({"error": "策略不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        payload = strategy_library_payload()
        payload["delivery_sync"] = sync_active_strategy_delivery() if payload["active_strategy_id"] else pause_hermes_delivery()
        self._send_json(payload)

    @staticmethod
    def _strategy_route(path: str) -> tuple[str | None, str | None]:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3 or parts[:2] != ["api", "strategies"]:
            return None, None
        strategy_id = parts[2]
        action = parts[3] if len(parts) == 4 else None
        if len(parts) > 4:
            return None, None
        return strategy_id, action

    @staticmethod
    def _run_route(path: str) -> str | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "runs"]:
            return parts[2]
        return None

    @staticmethod
    def _backtest_route(path: str) -> str | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "backtests"]:
            return parts[2]
        return None

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求必须是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON 顶层必须是对象")
        return payload

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_file(self, path: Path) -> None:
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        print(f"[stock-admin] {self.address_string()} {format % args}")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    sync_active_strategy_delivery()
    server = ThreadingHTTPServer((host, port), AdminHandler)
    print(f"Stock Agent Admin listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock Agent parameter administration server")
    parser.add_argument("--host", default=os.getenv("STOCK_AGENT_ADMIN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STOCK_AGENT_ADMIN_PORT", "8765")))
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
