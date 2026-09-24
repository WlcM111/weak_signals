/** Примитивы интерфейса: панели, статусы, шкалы, баннеры, заглушки. */

import type { ReactNode } from "react";
import { CONFIDENT_SCORE, JOB_STATUS_RU, isTerminal, percent, translate } from "../lib/format";
import { cssVars } from "../lib/style";
import { IconAlert, IconClose, IconInfo } from "./icons";

export function Card({ title, children, className = "" }: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title ? <h3 className="panel__title">{title}</h3> : null}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint, accent = false, text = false, action }: {
  label: string;
  value: string;
  hint?: string;
  accent?: boolean;
  text?: boolean;
  action?: ReactNode;
}) {
  return (
    <div className={`metric ${accent ? "metric--accent" : ""} ${text ? "metric--text" : ""}`}>
      <dt className="metric__label">{label}</dt>
      <dd className="metric__value">{value}</dd>
      {hint ? <dd className="metric__hint">{hint}</dd> : null}
      {action ? <dd className="metric__action">{action}</dd> : null}
    </div>
  );
}

export function Progress({ share, label }: { share: number; label?: string }) {
  const bounded = Math.max(0, Math.min(1, share));
  return (
    <div
      className="progress"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(bounded * 100)}
    >
      <div className="progress__bar" style={{ width: `${bounded * 100}%` }} />
    </div>
  );
}

export function Banner({ kind, title, children }: {
  kind: "error" | "warn" | "info";
  title?: string;
  children?: ReactNode;
}) {
  const Icon = kind === "error" ? IconClose : kind === "warn" ? IconAlert : IconInfo;
  return (
    <div className={`banner banner--${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span className="banner__icon" aria-hidden="true">
        <Icon size={18} />
      </span>
      <div className="banner__body">
        {title ? <strong className="banner__title">{title}</strong> : null}
        {children ? <div className="banner__text">{children}</div> : null}
      </div>
    </div>
  );
}

export function EmptyState({ title, hint, action }: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty__mark" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
        <span />
      </span>
      <strong className="empty__title">{title}</strong>
      {hint ? <span className="empty__hint">{hint}</span> : null}
      {action}
    </div>
  );
}

export function Skeleton({ height = 18, width = "100%" }: { height?: number; width?: string }) {
  return <div className="skeleton" style={{ height, width }} />;
}

export function SkeletonList({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton-list" aria-busy="true" aria-label="Загрузка">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton-list__row">
          <Skeleton height={20} width="46%" />
          <Skeleton height={13} />
          <Skeleton height={13} width="72%" />
        </div>
      ))}
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const tone = !isTerminal(status)
    ? "running"
    : status === "COMPLETED"
      ? "done"
      : status === "PARTIAL"
        ? "partial"
        : status === "FAILED"
          ? "failed"
          : "cancelled";
  return (
    <span className={`status status--${tone}`}>
      <span className="status__dot" aria-hidden="true" />
      {translate(JOB_STATUS_RU, status)}
    </span>
  );
}

export function ScoreDial({ value, caption = "скоринг модели", size = "md" }: {
  value: number;
  caption?: string;
  size?: "sm" | "md" | "lg";
}) {
  const share = Math.max(0, Math.min(1, value));
  const circumference = 2 * Math.PI * 42;
  const offset = circumference * (1 - share);
  return (
    <div
      className={`dial dial--${size} ${share >= CONFIDENT_SCORE ? "dial--high" : ""}`}
      style={cssVars({ "--dial-length": circumference.toFixed(2), "--dial-offset": offset.toFixed(2) })}
    >
      <div className="dial__face">
        <svg className="dial__ring" viewBox="0 0 100 100" aria-hidden="true">
          <circle className="dial__track" cx="50" cy="50" r="42" />
          <circle
            className="dial__value"
            cx="50"
            cy="50"
            r="42"
            strokeDasharray={circumference.toFixed(2)}
            strokeDashoffset={offset.toFixed(2)}
          />
          <circle
            className="dial__threshold"
            cx="50"
            cy="50"
            r="42"
            strokeDasharray={`1.4 ${(circumference - 1.4).toFixed(2)}`}
            strokeDashoffset={(-circumference * CONFIDENT_SCORE).toFixed(2)}
          />
        </svg>
        <span className="dial__number">{percent(value)}</span>
      </div>
      <span className="dial__caption">{caption}</span>
    </div>
  );
}
