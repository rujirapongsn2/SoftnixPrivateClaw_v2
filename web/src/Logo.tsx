import { useLogoSrc } from "./branding";

/** Softnix wordmark (dark text — the app has no dark theme yet; it's forced to
 * light everywhere, see the `color-scheme: light !important` rule in
 * styles.css). Previously this swapped to a white-text variant on
 * `prefers-color-scheme: dark`, which follows the OS setting independently of
 * that forced-light override — on a device with system dark mode on, that put
 * white text on the app's (always light) background, rendering it invisible.
 *
 * `slot` selects the admin-uploaded logo (Control Plane > Preferences) for that
 * surface; when none is set it falls back to the bundled wordmark. */
export function SoftnixLogo({
  height = 24,
  slot = "login",
}: {
  height?: number;
  slot?: "login" | "chat" | "sidebar";
}) {
  const src = useLogoSrc(slot, "/logo-softnix.png");
  return <img src={src} alt="Softnix" height={height} style={{ height, width: "auto" }} />;
}

/** Icon-only mark (crescent + "S"), cropped from the full wordmark — square,
 * so it fits cleanly in tight slots like the collapsed sidebar rail where the
 * full wordmark would get clipped/squeezed. Uses the admin sidebar logo when
 * set, contained within the square so a non-square upload still fits. */
export function SoftnixMark({ size = 24 }: { size?: number }) {
  const src = useLogoSrc("sidebar", "/logo-softnix-mark.png");
  return (
    <img
      src={src}
      alt="Softnix"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain" }}
    />
  );
}

/** Full product lockup: Softnix logo + "PrivateClaw". */
export function Brand({ height = 24 }: { height?: number }) {
  return (
    <span className="claw-brand">
      <SoftnixLogo height={height} slot="sidebar" />
      <span className="claw-brand-name">PrivateClaw</span>
    </span>
  );
}
