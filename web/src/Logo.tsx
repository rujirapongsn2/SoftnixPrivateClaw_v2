/** Softnix wordmark (dark text — the app has no dark theme yet; it's forced to
 * light everywhere, see the `color-scheme: light !important` rule in
 * styles.css). Previously this swapped to a white-text variant on
 * `prefers-color-scheme: dark`, which follows the OS setting independently of
 * that forced-light override — on a device with system dark mode on, that put
 * white text on the app's (always light) background, rendering it invisible. */
export function SoftnixLogo({ height = 24 }: { height?: number }) {
  return <img src="/logo-softnix.png" alt="Softnix" height={height} style={{ height, width: "auto" }} />;
}

/** Icon-only mark (crescent + "S"), cropped from the full wordmark — square,
 * so it fits cleanly in tight slots like the collapsed sidebar rail where the
 * full wordmark would get clipped/squeezed. */
export function SoftnixMark({ size = 24 }: { size?: number }) {
  return <img src="/logo-softnix-mark.png" alt="Softnix" width={size} height={size} />;
}

/** Full product lockup: Softnix logo + "PrivateClaw". */
export function Brand({ height = 24 }: { height?: number }) {
  return (
    <span className="claw-brand">
      <SoftnixLogo height={height} />
      <span className="claw-brand-name">PrivateClaw</span>
    </span>
  );
}
