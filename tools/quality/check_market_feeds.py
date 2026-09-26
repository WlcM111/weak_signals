"""Ленты отраслевых СМИ — рыночные источники (шаг 5): проверка и добавление рабочих в WS_COLLECTOR_RSS_FEEDS.

Площадки взяты из источников датасета организаторов (100 строк): чаще всего techcrunch.com — 11 ссылок,
siliconangle.com — 8, theaiinsider.tech — 7, geekwire.com, datacenterdynamics.com, roboticsandautomationnews.com,
eetimes.com, pandaily.com — по 3. TechCrunch уже есть в лентах по умолчанию. Адреса лент SiliconANGLE (ИИ) и
Robotics & Automation News взяты из каталога лент ИИ; остальные — стандартный путь /feed/ и /rss/ этих сайтов.
Ленты 26.09 без ответа заменены отраслевыми СМИ по темам кейса. Каждая лента проверяется запросом (код 200 и формат RSS/Atom): в .env попадают только рабочие, остальные
перечисляются с причиной. PR Newswire не добавляется: пресс-релизы по ТЗ не могут быть основанием.

Запуск из корня проекта: python3 tools/quality/check_market_feeds.py [--write]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_ru_feeds as feeds  # noqa: E402 - общий код проверки и записи лент

feeds.CANDIDATES = [
    # Работали 26.09 (повторная проверка безопасна: уже добавленные ленты пропускаются).
    ("The AI Insider", "https://theaiinsider.tech/feed/"),
    ("GeekWire", "https://www.geekwire.com/feed/"),
    ("DataCenterDynamics", "https://www.datacenterdynamics.com/en/rss/"),
    # Замена лент, не ответивших 26.09 (SiliconANGLE, Robotics & Automation News, EE Times, Pandaily): отраслевые
    # СМИ по темам кейса — ИИ, робототехника, инфраструктура, финтех, кибербезопасность, электроника.
    ("VentureBeat — ИИ", "https://venturebeat.com/category/ai/feed/"),
    ("The Robot Report", "https://www.therobotreport.com/feed/"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ("Finextra", "https://www.finextra.com/rss/headlines.aspx"),
    ("Help Net Security", "https://www.helpnetsecurity.com/feed/"),
    ("Хабр — искусственный интеллект", "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/"),
]

if __name__ == "__main__":
    sys.exit(feeds.main())
