interface VexaLogoProps {
  className?: string;
}

/** Faceted-gem Vexa mark: outer diamond cut with a girdle facet and a glossy highlight,
 * the "V" rendered as a rising chevron so it doubles as a voice-waveform peak. Shared by
 * the dashboard header and the app sidebar brand — sizing comes entirely from the
 * wrapping element's own CSS class (e.g. `.vx-brand-mark`, `.hermes-logo-mark`). */
export function VexaLogo({ className = 'vx-brand-mark' }: VexaLogoProps) {
  return (
    <span className={className} aria-hidden="true">
      <svg width="100%" height="100%" viewBox="0 0 32 32" fill="none">
        <defs>
          <linearGradient id="vx-logo-gradient" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#4be8ff" />
            <stop offset="48%" stopColor="#2684ff" />
            <stop offset="100%" stopColor="#9b5dff" />
          </linearGradient>
          <linearGradient id="vx-logo-gradient-inner" x1="1" y1="1" x2="0" y2="0">
            <stop offset="0%" stopColor="#9b5dff" />
            <stop offset="55%" stopColor="#2684ff" />
            <stop offset="100%" stopColor="#7af4ff" />
          </linearGradient>
          <filter id="vx-logo-glow" x="-60%" y="-60%" width="220%" height="220%">
            <feDropShadow dx="0" dy="0" stdDeviation="1.4" floodColor="#2ec8ff" floodOpacity=".55" />
          </filter>
        </defs>
        <g filter="url(#vx-logo-glow)">
          <path
            d="M16 3 L27 11.5 L16 29 L5 11.5 Z"
            stroke="url(#vx-logo-gradient)"
            strokeWidth="1.7"
            fill="rgba(38,132,255,.1)"
            strokeLinejoin="round"
          />
          <path d="M9.4 12.1 L22.6 12.1" stroke="url(#vx-logo-gradient)" strokeWidth=".9" opacity=".55" />
          <path
            d="M10.2 14.3 L16 24.4 L21.8 14.3"
            stroke="url(#vx-logo-gradient-inner)"
            strokeWidth="2.3"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path d="M13 9.6 L16 3 L11.2 10.6 Z" fill="rgba(255,255,255,.32)" />
        </g>
      </svg>
    </span>
  );
}
