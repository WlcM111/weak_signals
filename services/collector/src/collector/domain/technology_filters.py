"""Общие списки отсева названий технологий для извлечения и оценки возникновения (этапы 4.2–4.3).

Списки — по фактам замеров 07–10.10.2026: обрывки («deep reinforcement» от «… learning»), словосочетания на глаголах
(«code owns», «framework integrating»), общие темы домена («future directions», «social media») и зрелые
области («federated learning», «anomaly detection») с их вариантами («federated learning for …»).
"""

from __future__ import annotations

from collections.abc import Sequence

# Слово после словосочетания делает его обрывком: «deep reinforcement [learning]», «graph neural [networks]».
FRAGMENT_HEADS = frozenset("""learning intelligence network networks computing model models vision processing language
languages reality system systems science engineering""".split())
# Слова, после которых «intelligence»/«learning» в начале словосочетания — хвост общего термина.
FRAGMENT_LEADS = frozenset("artificial machine deep reinforcement federated".split())
# Слова на -ing, которые обычно названия технологий, а не глаголы.
NOUN_ING = frozenset("""computing learning sensing processing switching offloading caching routing networking engineering
manufacturing printing imaging mining training testing monitoring scheduling recycling banking lending trading clearing
packaging cooling charging sequencing fingerprinting streaming encoding decoding rendering mapping tracking hashing
watermarking unlearning reasoning planning grasping tokenization grounding prompting tuning pretraining prototyping
matching labeling forecasting clustering sampling scaling batching pruning parsing indexing logging auditing teaming
phishing spoofing jailbreaking poisoning provisioning onboarding underwriting invoicing factoring crowdfunding financing
staking bridging welding machining casting coating sintering drilling sorting picking farming harvesting spraying
painting inspecting docking landing driving flying walking swimming""".split())
VERB_TAILS = frozenset("""owns built uses makes enables becomes gets raises launches says shows unveils announces introduces
brings adds helps lets wants targets takes""".split())
GENERIC_PHRASES = frozenset({
    # этап 4.4 — по быстрому замеру 10.10.2026 (списки «извлечённые» и «из корпусов»)
    "current state", "data collection", "data processing", "data engineering", "use case", "use cases",
    "public sector", "service quality", "customer experience", "food industry", "medicinal plants", "power plants",
    "nuclear power", "ieee transactions", "now available", "process optimization", "manufacturing process",
    "manufacturing processes", "advanced manufacturing", "advanced manufacturing technologies", "factory environments",
    "manufacturing environments", "chemical manufacturing", "electronics manufacturing", "semiconductor manufacturing",
    "financial sector", "financial services sector", "banking sector", "financial transaction", "financial transactions",
    "electronic payments", "credit card", "credit cards", "stock market", "behavioral biases", "transaction tax",
    "fraud risk", "ai adoption", "ai integration", "ai strategy", "ai risk", "ai risks", "ai deployment",
    "ai governance", "ai act", "secure ai", "trustworthy ai", "sustainable ai", "security testing", "cyber security",
    "security threats", "security risks", "security controls", "security assessment", "advanced security",
    "defense strategies", "mitigation strategies", "software engineering", "systematic security", "threat detection",
    "access control", "resource allocation", "task scheduling", "sensor network", "sensor networks", "iot networks",
    "network architectures", "distribution networks", "energy optimization", "energy efficient", "battery storage",
    "battery energy", "information propagation", "artificial general intelligence", "robot control",
    "robot interactions", "robot collaboration", "humanoid robotics", "efficient edge", "edge deployment",
    "inference optimization", "inference performance", "language model inference", "multimodal llm", "multimodal ai",
    "ai flow",
    "future directions", "future research", "research directions", "social media", "systematic literature",
    "systematic review", "literature review", "customer satisfaction", "financial literacy", "smart home",
    "magnetic resonance", "energy storage", "energy efficiency", "case study", "comparative study",
    "emerging markets", "digital economy", "digital transformation", "sustainable development", "supply chain",
    "risk assessment", "decision making", "real time", "state of the art", "open source", "national security",
    "critical infrastructure", "personal data", "regulatory compliance", "fraud detection", "intrusion detection",
})
MATURE = frozenset({
    "adversarial training", "differential privacy", "federated learning", "anomaly detection", "predictive maintenance",
    "digital twin", "reinforcement learning", "transfer learning", "deep reinforcement learning", "computer vision",
    "natural language processing", "robotic process automation", "smart contract", "explainable ai", "machine vision",
    "swarm robotics", "quantum computing", "zero trust", "biometric authentication", "chatbot", "recommendation system",
    "generative design", "collaborative robot", "cobot", "autonomous vehicle", "augmented reality", "virtual reality",
    "3d printing", "additive manufacturing", "model compression", "knowledge distillation", "neural network pruning",
    "microservice", "data lake", "wearable", "edge computing", "cloud computing", "blockchain", "internet of things",
    "industrial iot", "smart manufacturing", "human robot interaction", "human robot collaboration",
    # этап 4.4 — зрелые области из быстрого замера 10.10.2026
    "imitation learning", "fuzzy logic", "autonomous driving", "mobile robot",
})
_KEY_SPLIT = (" for ", " in ", " on ", " with ", " using ", " via ", " based ", " of ")


def mature_key(phrase: str) -> str:
    """Ключевая часть названия до уточнения: «federated learning for factories» → «federated learning»."""
    key = " ".join(phrase.lower().replace("-", " ").split())
    for marker in _KEY_SPLIT:
        if marker in f" {key} ":
            key = f" {key} ".split(marker, 1)[0].strip()
    words = key.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith(("ss", "us", "is")):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def is_mature(phrase: str) -> bool:
    """Зрелая область без конкретного применения (этап 4.6, аудит P17).

    Голое название («federated learning») и вариант с общим уточнением («… for systems») — зрелые. Новое применение
    зрелого метода («federated learning for anti-money laundering») — кандидат: зрелость базы — признак, не запрет.
    """
    key = mature_key(phrase)
    if key not in MATURE:
        return False
    full = " ".join(phrase.lower().replace("-", " ").split())
    rest = full[len(key):] if full.startswith(key) else full.replace(key, " ")
    qualifier = [w for w in rest.split() if w not in ("for", "in", "on", "with", "using", "via", "based", "of", "and", "the")]
    return all(w in GENERIC_TOPIC_WORDS or w in GENERIC_QUALIFIERS or len(w) < 3 for w in qualifier)


# Уточнения, которые не делают применение зрелого метода конкретным (те же, что в insight.expand_query).
GENERIC_QUALIFIERS = frozenset("""systems system applications application industry industries business businesses
enterprises enterprise organizations data operations solutions platforms factories factory production processes
manufacturing distributed decisions decision operational""".split())


def is_verbish(words: list[str]) -> bool:
    """Словосочетание с глаголом: слово на -ing, которое не название технологии, или глагол действия в конце."""
    if any(word.endswith("ing") and len(word) > 4 and word not in NOUN_ING for word in words):
        return True
    return words[-1] in VERB_TAILS


# Этап 4.4: ключевые слова темы для выборки корпуса и архива — без общих слов, которые есть почти в любой работе.
GENERIC_TOPIC_WORDS = frozenset("""ai artificial intelligence system systems based using new novel approach approaches method
methods data technology technologies solution solutions application applications platform platforms model models framework
frameworks management software advanced emerging future development integration optimization high performance efficient
smart intelligent digital modern innovative perspective perspectives promising decision making environment environments
machine deep learning analysis research study overview state art use case cases tool tools network networks process
processes level levels support driven enabled powered towards toward improving improved
искусственный искусственного искусственным интеллект интеллекта интеллектом система системы систем системах технология
технологии технологий решение решения решений данные данных обработка обработки методы методов новые перспективные
современные интеллектуальные цифровые развитие применение использование""".split())


def topic_keywords(phrases: Sequence[str], limit: int = 16) -> list[str]:
    """Ключевые слова фраз темы для выборки «хотя бы 2 слова темы»: без общих слов, не короче 3 букв, без повторов."""
    from collector.domain.text_tokens import tokens_of

    words: dict[str, None] = {}
    for phrase in phrases:
        for word in tokens_of(phrase):
            if len(word) >= 3 and word not in GENERIC_TOPIC_WORDS and not word.isdigit():
                words.setdefault(word, None)
    return list(words)[:limit]


def with_variants(words: Sequence[str]) -> list[str]:
    """Слова и их вторая форма числа (robot/robots, agents/agent): индекс хранит слова как в тексте."""
    result: dict[str, None] = {}
    for word in words:
        result.setdefault(word, None)
        if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            result.setdefault(word[:-1], None)
        elif word.isascii() and word.isalpha() and not word.endswith("s"):
            result.setdefault(word + "s", None)
    return list(result)
