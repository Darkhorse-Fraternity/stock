from __future__ import annotations

from datetime import timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_FALLBACK_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
DEFAULT_BOARD_CODE = "BK0800"
DEFAULT_BOARD_NAME = "人工智能"
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_LLM_TIMEOUT_SECONDS = 30
MIN_FLOAT_MARKET_CAP = 2_000_000_000
MAX_FLOAT_MARKET_CAP = 10_000_000_000

STATIC_FALLBACK = [
    {"symbol": "002230", "name": "科大讯飞", "sector": DEFAULT_BOARD_NAME, "base_price": 50.00},
    {"symbol": "000977", "name": "浪潮信息", "sector": DEFAULT_BOARD_NAME, "base_price": 55.00},
    {"symbol": "603019", "name": "中科曙光", "sector": DEFAULT_BOARD_NAME, "base_price": 75.00},
    {"symbol": "300418", "name": "昆仑万维", "sector": DEFAULT_BOARD_NAME, "base_price": 45.00},
    {"symbol": "688256", "name": "寒武纪-U", "sector": DEFAULT_BOARD_NAME, "base_price": 700.00},
    {"symbol": "688041", "name": "海光信息", "sector": DEFAULT_BOARD_NAME, "base_price": 180.00},
    {"symbol": "300308", "name": "中际旭创", "sector": DEFAULT_BOARD_NAME, "base_price": 200.00},
    {"symbol": "002415", "name": "海康威视", "sector": DEFAULT_BOARD_NAME, "base_price": 35.00},
    {"symbol": "300474", "name": "景嘉微", "sector": DEFAULT_BOARD_NAME, "base_price": 90.00},
    {"symbol": "300058", "name": "蓝色光标", "sector": DEFAULT_BOARD_NAME, "base_price": 13.00},
]
