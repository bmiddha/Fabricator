"""First-party compatibility shim for BlueMap's opaque-origin viewer."""

BOOTSTRAP = b"""'use strict';
(() => {
  // BlueMap accesses storage during construction, before useCookies is read.
  // Keep preferences inside this frame's memory, never in the panel's origin.
  for (const name of ['localStorage', 'sessionStorage']) {
    const values = new Map();
    const storage = Object.freeze({
      getItem: key => values.get(String(key)) ?? null,
      setItem: (key, value) => { values.set(String(key), String(value)); },
      removeItem: key => { values.delete(String(key)); },
      clear: () => { values.clear(); },
      key: index => [...values.keys()][index] ?? null,
      get length() { return values.size; }
    });
    Object.defineProperty(globalThis, name, { value: storage, configurable: false });
  }
})();
"""
