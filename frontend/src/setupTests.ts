import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';

// The default 1s for findBy*/waitFor is not enough when the whole suite runs in
// parallel on a loaded CI runner; tests that passed alone timed out there.
configure({ asyncUtilTimeout: 10000 });

window.HTMLElement.prototype.scrollIntoView = function() {};

class TestResizeObserver implements ResizeObserver {
  constructor(_callback: ResizeObserverCallback) {}
  observe(_target: Element, _options?: ResizeObserverOptions) {}
  unobserve(_target: Element) {}
  disconnect() {}
}

globalThis.ResizeObserver = TestResizeObserver as typeof ResizeObserver;
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => undefined;
