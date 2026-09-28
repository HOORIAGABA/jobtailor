/**
 * Inline SVG, not an icon package.
 *
 * Nine icons is not worth a dependency, a tree-shaking configuration and a
 * font request — and inlining means they inherit `currentColor` and the text
 * size around them for free, which is the whole reason they read as part of a
 * label rather than as clip art stuck beside it.
 *
 * All of them are drawn on a 24-unit grid with a 1.75 stroke, so they sit on
 * the same optical weight as the medium text they accompany. `aria-hidden` on
 * every one: an icon next to a word is decoration, and a screen reader that
 * announces it twice is worse than one that ignores it.
 */
import type { SVGProps } from "react";

type Props = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

/** The mark. A document with a seam down it — the diff, which is the product. */
export function Logo({ size = 22, ...rest }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      <rect
        x="3.25"
        y="2.25"
        width="17.5"
        height="19.5"
        rx="3.25"
        stroke="currentColor"
        strokeWidth="1.75"
      />
      <path
        d="M12 2.25v19.5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeDasharray="2 2.5"
        opacity="0.55"
      />
      <path
        d="M6.6 8h3.2M6.6 12h3.2M6.6 16h2"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        opacity="0.5"
      />
      <path
        d="M14.2 8h3.2M14.2 12h3.2M14.2 16h2.2"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

export const Check = (p: Props) => (
  <Svg {...p}>
    <path d="m4.5 12.5 5 5 10-11" />
  </Svg>
);

export const Cross = (p: Props) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </Svg>
);

export const Alert = (p: Props) => (
  <Svg {...p}>
    <path d="M12 8.5v4.75" />
    <path d="M12 17h.01" />
    <path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
  </Svg>
);

export const Arrow = (p: Props) => (
  <Svg {...p}>
    <path d="M5 12h13M12.5 5.5 19 12l-6.5 6.5" />
  </Svg>
);

export const Upload = (p: Props) => (
  <Svg {...p}>
    <path d="M12 16V4.5M7.5 9 12 4.5 16.5 9" />
    <path d="M4 15v2.5A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5V15" />
  </Svg>
);

export const Doc = (p: Props) => (
  <Svg {...p}>
    <path d="M14 3H7.5A2.5 2.5 0 0 0 5 5.5v13A2.5 2.5 0 0 0 7.5 21h9a2.5 2.5 0 0 0 2.5-2.5V8l-5-5Z" />
    <path d="M14 3v5h5" />
  </Svg>
);

export const Mail = (p: Props) => (
  <Svg {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2.5" />
    <path d="m3.8 7 7.1 5.2a2 2 0 0 0 2.2 0L20.2 7" />
  </Svg>
);

export const Shield = (p: Props) => (
  <Svg {...p}>
    <path d="M12 3 5 5.8v5.4c0 4.2 2.8 7.9 7 9.8 4.2-1.9 7-5.6 7-9.8V5.8L12 3Z" />
    <path d="m9.2 12 2 2 3.6-3.8" />
  </Svg>
);

export const Sun = (p: Props) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4" />
  </Svg>
);

export const Moon = (p: Props) => (
  <Svg {...p}>
    <path d="M20 14.2A8.2 8.2 0 0 1 9.8 4a8.4 8.4 0 1 0 10.2 10.2Z" />
  </Svg>
);
