-- analyzer schema v0003 (этап 5.0.2): кандидатов анализа — до 500.
-- Этап 5.0 поднял max_candidates до 150 (диапазон домена 5..500), а ограничение таблицы осталось 5..100: вставка анализа
-- падала с нарушением CHECK, StartAnalysis отвечал UNKNOWN, задание из UI завершалось «analyzer недоступен».
ALTER TABLE analyzer.analyses DROP CONSTRAINT IF EXISTS analyses_max_candidates_check;
ALTER TABLE analyzer.analyses ADD CONSTRAINT analyses_max_candidates_check CHECK (max_candidates BETWEEN 5 AND 500);
