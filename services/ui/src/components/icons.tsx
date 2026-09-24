/** Линейные иконки интерфейса. */

import type { ReactNode } from "react";

function Svg({ children, size = 20 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export function IconSignal({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 20v-9" />
      <circle cx="12" cy="8.5" r="1.6" />
      <path d="M8.2 11.6a5.2 5.2 0 0 1 7.6 0" />
      <path d="M5.4 8.8a9.2 9.2 0 0 1 13.2 0" />
      <path d="M4 20h2M18 20h2" />
    </Svg>
  );
}

export function IconSearch({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <circle cx="10.5" cy="10.5" r="6.2" />
      <path d="m15.2 15.2 4.8 4.8" />
    </Svg>
  );
}

export function IconModel({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <circle cx="5.5" cy="7" r="2" />
      <circle cx="5.5" cy="17" r="2" />
      <circle cx="18.5" cy="12" r="2.4" />
      <path d="M7.4 7.7 16.2 11M7.4 16.3l8.8-3.3M5.5 9v6" />
    </Svg>
  );
}

export function IconBook({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 6.5C10.2 5 7.6 4.5 4 4.8v13.4c3.6-.3 6.2.2 8 1.7" />
      <path d="M12 6.5c1.8-1.5 4.4-2 8-1.7v13.4c-3.6-.3-6.2.2-8 1.7V6.5Z" />
    </Svg>
  );
}

export function IconArrowLeft({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </Svg>
  );
}

export function IconArrowRight({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Svg>
  );
}

export function IconDownload({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 4v11M7 10.5l5 5 5-5M5 20h14" />
    </Svg>
  );
}

export function IconExternal({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M14 5h5v5M19 5l-8 8M17 14v4a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 5 18V8.5A1.5 1.5 0 0 1 6.5 7H10" />
    </Svg>
  );
}

export function IconChevron({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="m6 9 6 6 6-6" />
    </Svg>
  );
}

export function IconStop({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <circle cx="12" cy="12" r="8" />
      <path d="M9.5 9.5h5v5h-5z" />
    </Svg>
  );
}

export function IconPlus({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 5v14M5 12h14" />
    </Svg>
  );
}

export function IconSource({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20V3.5Z" />
      <path d="M14 3.5V8h4M10 12h5M10 15.5h5" />
    </Svg>
  );
}

export function IconFilter({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M4 6h16M7 12h10M10 18h4" />
    </Svg>
  );
}

export function IconCheck({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    </Svg>
  );
}

export function IconClose({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M7 7l10 10M17 7 7 17" />
    </Svg>
  );
}

export function IconAlert({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 7.5v6" />
      <circle cx="12" cy="16.8" r=".6" />
    </Svg>
  );
}

export function IconInfo({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M12 11v6" />
      <circle cx="12" cy="7.6" r=".6" />
    </Svg>
  );
}

export function IconSpark({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="M3 17h3l2.2-5 3.1 8 3.4-15 2.3 12H21" />
    </Svg>
  );
}

export function IconLayers({ size }: { size?: number }) {
  return (
    <Svg size={size}>
      <path d="m12 4 8.5 4.5L12 13 3.5 8.5 12 4Z" />
      <path d="m3.5 12.5 8.5 4.5 8.5-4.5M3.5 16.5 12 21l8.5-4.5" />
    </Svg>
  );
}
