/** Пауза бесконечной анимации (WCAG 2.2.2). */

import { useState } from "react";

const KEY = "ws:motion-paused";

function stored(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

function apply(paused: boolean): void {
  document.documentElement.toggleAttribute("data-motion-paused", paused);
}

/** Восстанавливает выбор до первой отрисовки. */
export function restoreMotion(): void {
  apply(stored());
}

export function useMotionPause(): [boolean, () => void] {
  const [paused, setPaused] = useState(stored);

  function toggle() {
    const next = !paused;
    setPaused(next);
    apply(next);
    try {
      if (next) window.localStorage.setItem(KEY, "1");
      else window.localStorage.removeItem(KEY);
    } catch {
      // хранилище недоступно: выбор действует до перезагрузки
    }
  }

  return [paused, toggle];
}
