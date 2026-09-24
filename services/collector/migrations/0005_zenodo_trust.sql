-- collector schema v0005: доверенность Zenodo — MEDIUM.
-- Zenodo почти не модерирует загрузки: в прогоне 23.09 псевдонаучный препринт подтверждал карточку наравне
-- с arXiv. Эффективная доверенность задаётся потолком в trust_rules.yaml (trust_ceiling); здесь каталог
-- приводится в соответствие, чтобы интерфейс и отчёты не показывали прежнее значение.
-- Откат: UPDATE collector.source_catalog SET default_trust = 'HIGH' WHERE source_key = 'zenodo';
UPDATE collector.source_catalog SET default_trust = 'MEDIUM' WHERE source_key = 'zenodo';
