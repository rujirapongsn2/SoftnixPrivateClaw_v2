import { useCallback, useEffect, useRef, type UIEvent } from "react";

/** Keep the native scrollbar draggable without showing it while idle. */
export function useAutoHideScrollbar() {
  const timers = useRef(new Map<HTMLElement, ReturnType<typeof setTimeout>>());

  useEffect(() => () => {
    for (const [element, timer] of timers.current) {
      clearTimeout(timer);
      element.classList.remove("claw-is-scrolling");
    }
    timers.current.clear();
  }, []);

  return useCallback((event: UIEvent<HTMLElement>) => {
    const element = event.target;
    if (!(element instanceof HTMLElement)) return;
    clearTimeout(timers.current.get(element));
    element.classList.add("claw-is-scrolling");
    timers.current.set(element, setTimeout(() => {
      element.classList.remove("claw-is-scrolling");
      timers.current.delete(element);
    }, 900));
  }, []);
}
