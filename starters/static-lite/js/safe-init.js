/** safeInit — one failing module must never take the site down (NIKA
 * pattern). Every module registers through this wrapper; errors are logged
 * loudly and isolated. */
export function safeInit(name, fn) {
  try {
    fn();
  } catch (error) {
    console.error(`[safeInit] module "${name}" failed:`, error);
  }
}
