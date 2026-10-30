// Minimal typing for the part of noVNC's RFB client the agent desktop viewer
// uses (the package ships no declarations).
declare module '@novnc/novnc' {
  export interface RFBOptions {
    wsProtocols?: string[];
    shared?: boolean;
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: RFBOptions);
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    background: string;
    disconnect(): void;
    focus(): void;
    /** Sends text to the remote clipboard (ignored by noVNC while viewOnly). */
    clipboardPasteFrom(text: string): void;
  }
}
