"""Ленты отраслевых СМИ — рыночные источники (шаг 5): проверка и добавление рабочих в WS_COLLECTOR_RSS_FEEDS.

Площадки взяты из источников датасета организаторов (100 строк): чаще всего techcrunch.com — 11 ссылок,
siliconangle.com — 8, theaiinsider.tech — 7, geekwire.com, datacenterdynamics.com, roboticsandautomationnews.com,
eetimes.com, pandaily.com — по 3. TechCrunch уже есть в лентах по умолчанию. Адреса лент SiliconANGLE (ИИ) и
Robotics & Automation News взяты из каталога лент ИИ; остальные — стандартный путь /feed/ и /rss/ этих сайтов.
Каждая лента проверяется запросом (код 200 и формат RSS/Atom): в .env попадают только рабочие, остальные
перечисляются с причиной. PR Newswire не добавляется: пресс-релизы по ТЗ не могут быть основанием.

Запуск из корня проекта: python3 tools/quality/check_market_feeds.py [--write]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_ru_feeds as feeds  # noqa: E402 - общий код проверки и записи лент

feeds.CANDIDATES = [
    ("SiliconANGLE — ИИ", "https://siliconangle.com/category/ai/feed/"),
    ("SiliconANGLE — все материалы", "https://siliconangle.com/feed/"),
    ("The AI Insider", "https://theaiinsider.tech/feed/"),
    ("GeekWire", "https://www.geekwire.com/feed/"),
    ("DataCenterDynamics", "https://www.datacenterdynamics.com/en/rss/"),
    ("Robotics & Automation News", "https://roboticsandautomationnews.com/feed/"),
    ("EE Times", "https://www.eetimes.com/feed/"),
    ("Pandaily", "https://pandaily.com/feed/"),
]

if __name__ == "__main__":
    sys.exit(feeds.main())
