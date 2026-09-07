import { useEffect, useState } from "react";

/** Subscribe to a CSS media query and re-render when it changes. Used to drive
 * responsive behavior that CSS alone can't express (e.g. forcing the sidebar
 * into a full off-canvas drawer on tablets/phones, gating heavy panels). */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && "matchMedia" in window ? window.matchMedia(query).matches : false,
  );

  useEffect(() => {
    if (!("matchMedia" in window)) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

// Shared breakpoints. Sidebar becomes a drawer at/below the tablet width;
// heaviest surfaces (Admin console, Execution panel) are hidden on phones.
export const MOBILE_QUERY = "(max-width: 1024px)";
export const PHONE_QUERY = "(max-width: 640px)";
