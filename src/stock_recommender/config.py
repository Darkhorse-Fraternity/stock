from __future__ import annotations

from datetime import timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/clist/get"
DEFAULT_BOARD_CODE = "BK0809"
DEFAULT_BOARD_NAME = "AI智能体"
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_LLM_TIMEOUT_SECONDS = 60
MIN_FLOAT_MARKET_CAP = 2_000_000_000
MAX_FLOAT_MARKET_CAP = 10_000_000_000

STATIC_FALLBACK = [
    {"symbol": "300130", "name": "新国都", "sector": DEFAULT_BOARD_NAME, "base_price": 23.69},
    {"symbol": "300857", "name": "协创数据", "sector": DEFAULT_BOARD_NAME, "base_price": 310.13},
    {"symbol": "002657", "name": "中科金财", "sector": DEFAULT_BOARD_NAME, "base_price": 21.80},
    {"symbol": "002354", "name": "天娱数科", "sector": DEFAULT_BOARD_NAME, "base_price": 8.50},
    {"symbol": "002421", "name": "达实智能", "sector": DEFAULT_BOARD_NAME, "base_price": 4.21},
    {"symbol": "300846", "name": "首都在线", "sector": DEFAULT_BOARD_NAME, "base_price": 24.47},
    {"symbol": "301396", "name": "宏景科技", "sector": DEFAULT_BOARD_NAME, "base_price": 237.00},
    {"symbol": "300058", "name": "蓝色光标", "sector": DEFAULT_BOARD_NAME, "base_price": 15.86},
    {"symbol": "000681", "name": "视觉中国", "sector": DEFAULT_BOARD_NAME, "base_price": 22.41},
    {"symbol": "300418", "name": "昆仑万维", "sector": DEFAULT_BOARD_NAME, "base_price": 43.92},
]
