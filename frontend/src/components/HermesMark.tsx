import { VexaLogo } from './VexaLogo';

interface HermesMarkProps {
  className?: string;
}

/** Login-screen mark. Rendered inline rather than as <img src="/favicon.svg">, so a
 * browser's heuristic cache of the favicon can never show an outdated logo here. */
export function HermesMark({ className = '' }: HermesMarkProps) {
  return <VexaLogo className={`hermes-logo-mark${className ? ` ${className}` : ''}`} />;
}
