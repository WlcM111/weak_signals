-- orchestrator schema v0002: отложенный повтор задания.
-- Метод postpone() возвращал задание в очередь без задержки, поэтому воркер забирал его немедленно
-- и лимит из десяти откладываний сгорал за секунды: перезапуск соседнего сервиса (например, прогрев
-- модели в analyzer) всегда заканчивался отказом задания с кодом UPSTREAM_UNAVAILABLE.
-- Колонка хранит время, раньше которого задание не берут в работу.
ALTER TABLE orchestrator.jobs
  ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS jobs_available_idx ON orchestrator.jobs (available_at)
  WHERE status = 'QUEUED';