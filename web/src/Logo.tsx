/** Softnix wordmark that swaps to the white variant in dark mode. */
export function SoftnixLogo({ height = 24 }: { height?: number }) {
  return (
    <picture>
      <source srcSet="/logo-softnix-white.png" media="(prefers-color-scheme: dark)" />
      <img src="/logo-softnix.png" alt="Softnix" height={height} style={{ height, width: "auto" }} />
    </picture>
  );
}

/** Icon-only mark (crescent + "S"), cropped from the full wordmark — square,
 * so it fits cleanly in tight slots like the collapsed sidebar rail where the
 * full wordmark would get clipped/squeezed. Swaps to the white-text variant
 * in dark mode, same as SoftnixLogo. */
export function SoftnixMark({ size = 24 }: { size?: number }) {
  return (
    <picture>
      <source srcSet="/logo-softnix-mark-white.png" media="(prefers-color-scheme: dark)" />
      <img src="/logo-softnix-mark.png" alt="Softnix" width={size} height={size} />
    </picture>
  );
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
