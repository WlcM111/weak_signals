"""Общие списки отсева названий технологий для извлечения и оценки возникновения (этапы 4.2–4.3).

Списки — по фактам замеров 07–10.10.2026: обрывки («deep reinforcement» от «… learning»), словосочетания на глаголах
(«code owns», «framework integrating»), общие темы домена («future directions», «social media») и зрелые
области («federated learning», «anomaly detection») с их вариантами («federated learning for …»).
"""

from __future__ import annotations

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
    """Зрелая область или её вариант с уточнением."""
    return mature_key(phrase) in MATURE


def is_verbish(words: list[str]) -> bool:
    """Словосочетание с глаголом: слово на -ing, которое не название технологии, или глагол действия в конце."""
    if any(word.endswith("ing") and len(word) > 4 and word not in NOUN_ING for word in words):
        return True
    return words[-1] in VERB_TAILS
