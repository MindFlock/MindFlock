/** A canvas with the murmuration flying on it (lib/flock.ts). Purely
 * decorative, so it is always aria-hidden and never takes the pointer.
 *
 * `apiRef` hands the caller the live handle, which is how the break screen and
 * the idle overlay send the flock home to the logo on their way out. */

import { useEffect, useRef, type RefObject } from "react";
import { startFlock, type FlockHandle, type FlockOptions } from "../../lib/flock";

interface Props {
  id?: string;
  className?: string;
  options?: FlockOptions;
  apiRef?: RefObject<FlockHandle | null>;
}

export function Flock({ id, className, options, apiRef }: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  // `options` is deliberately NOT a dependency: callers pass an object literal,
  // which is a new identity every render, and restarting the flock would jump
  // every bird back to a random position each time the parent re-renders (the
  // break screen re-renders once a second to tick its clock).
  const opts = useRef(options);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const flock = startFlock(canvas, opts.current);
    if (apiRef) apiRef.current = flock;
    return () => {
      flock.stop();
      if (apiRef && apiRef.current === flock) apiRef.current = null;
    };
  }, [apiRef]);
  return <canvas id={id} ref={ref} className={className} aria-hidden="true" />;
}
