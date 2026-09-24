/** Передача CSS-переменных в атрибут style и прокрутка с учётом настроек анимации. */

import type { CSSProperties } from "react";

export function cssVars(vars: Record<string, string | number>): CSSProperties {
  return vars as unknown as CSSProperties;
}

export function scrollBehavior(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}
