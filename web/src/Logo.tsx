/** Softnix wordmark that swaps to the white variant in dark mode. */
export function SoftnixLogo({ height = 24 }: { height?: number }) {
  return (
    <picture>
      <source srcSet="/logo-softnix-white.png" media="(prefers-color-scheme: dark)" />
      <img src="/logo-softnix.png" alt="Softnix" height={height} style={{ height, width: "auto" }} />
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
