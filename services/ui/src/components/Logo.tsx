/** Знак проекта: один сигнал, поднявшийся над ровным шумом, и расходящиеся дуги. */

import { useId } from "react";

export function Logo({ size = 40, title = "Слабые сигналы" }: { size?: number; title?: string }) {
  const gradient = `ws-core-${useId().replace(/:/g, "")}`;
  const paint = `url(#${gradient})`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role={title ? "img" : undefined}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : true}
    >
      {title ? <title>{title}</title> : null}
      <defs>
        <linearGradient id={gradient} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor="#4a086c" />
          <stop offset="55%" stopColor="#7a2ba6" />
          <stop offset="100%" stopColor="#12a8c8" />
        </linearGradient>
      </defs>
      <g stroke="currentColor" strokeWidth="3" strokeLinecap="round" opacity="0.28">
        <line x1="8" y1="44" x2="8" y2="50" />
        <line x1="16" y1="42" x2="16" y2="50" />
        <line x1="24" y1="45" x2="24" y2="50" />
        <line x1="40" y1="43" x2="40" y2="50" />
        <line x1="48" y1="45" x2="48" y2="50" />
        <line x1="56" y1="42" x2="56" y2="50" />
      </g>
      <path d="M32 50 V20" stroke={paint} strokeWidth="5" strokeLinecap="round" fill="none" />
      <path d="M20 26a16 16 0 0 1 24 0" stroke={paint} strokeWidth="3.4" fill="none" strokeLinecap="round" opacity="0.85" />
      <path d="M13 19a25 25 0 0 1 38 0" stroke={paint} strokeWidth="2.6" fill="none" strokeLinecap="round" opacity="0.45" />
      <circle cx="32" cy="17" r="4.6" fill={paint} />
    </svg>
  );
}
