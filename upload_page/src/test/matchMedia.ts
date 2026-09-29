// jsdom has no window.matchMedia; this stub models only the dark-mode query
// the page asks about, and lets tests flip the simulated OS setting.

import { SYSTEM_DARK_QUERY } from "../colorScheme/colorScheme";

type ChangeListener = (event: MediaQueryListEvent) => void;

const listeners = new Set<ChangeListener>();
let systemPrefersDark = false;

function createMediaQueryList(query: string): MediaQueryList {
  const isDarkQuery = query === SYSTEM_DARK_QUERY;
  return {
    media: query,
    get matches() {
      return isDarkQuery && systemPrefersDark;
    },
    onchange: null,
    addEventListener: (_type: string, listener: ChangeListener) => {
      if (isDarkQuery) listeners.add(listener);
    },
    removeEventListener: (_type: string, listener: ChangeListener) => {
      listeners.delete(listener);
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  } as unknown as MediaQueryList;
}

export function installMatchMediaStub(): void {
  window.matchMedia = createMediaQueryList;
}

export function setSystemPrefersDark(prefersDark: boolean): void {
  systemPrefersDark = prefersDark;
  const event = { matches: prefersDark, media: SYSTEM_DARK_QUERY } as MediaQueryListEvent;
  listeners.forEach((listener) => listener(event));
}

export function resetMatchMediaStub(): void {
  systemPrefersDark = false;
  listeners.clear();
}
