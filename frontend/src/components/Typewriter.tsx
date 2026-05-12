/**
 * Client-side typewriter effect.
 *
 * Reveals a string character-by-character to simulate a streaming
 * response. Useful for the most prominent prose fields in the
 * pipeline output panel (headlines, descriptions) where the
 * sequential stagger of whole lines isn't dramatic enough.
 *
 * Why a component (not a hook): renders a single `<span>` so callers
 * can drop it in alongside other inline content without restructuring
 * markup. The component owns its setInterval lifecycle, restart-on-
 * text-change rule, and cancellation — callers don't have to think
 * about effects.
 *
 * Caveats:
 *  - Per-character animation costs a re-render per tick. Fine for
 *    short prose (≤ a few hundred chars); avoid for code blocks /
 *    long tables.
 *  - The typewriter restarts when `text` CHANGES — useful when the
 *    backend updates a field. Pass a stable `text` to lock the
 *    revealed output once it finishes.
 */

import { useEffect, useRef, useState } from "react";

interface TypewriterProps {
  /** The text to reveal. Animation restarts when this changes. */
  text: string;
  /** Characters revealed per second. Default 80 ≈ a fast typist. */
  speed?: number;
  /** Delay before the first character appears, in ms. */
  startDelay?: number;
  /** Class to apply to the wrapping span. */
  className?: string;
  /** Inline style for the wrapping span. */
  style?: React.CSSProperties;
  /** Show a blinking caret while typing. */
  cursor?: boolean;
  /** Fired once when the full text has been revealed. */
  onDone?: () => void;
}

export function Typewriter({
  text,
  speed = 80,
  startDelay = 0,
  className,
  style,
  cursor = false,
  onDone,
}: TypewriterProps) {
  const [revealed, setRevealed] = useState("");
  // We track the latest `text` in a ref so the interval callback
  // always sees the current target without re-binding the interval
  // on every render.
  const targetRef = useRef(text);
  targetRef.current = text;

  useEffect(() => {
    setRevealed("");
    if (!text) {
      onDone?.();
      return;
    }
    // Honor the OS-level reduced-motion preference. Reveal the
    // whole string at once instead of typing it out.
    if (typeof window !== "undefined") {
      const prefersReduce = window.matchMedia?.(
        "(prefers-reduced-motion: reduce)",
      ).matches;
      if (prefersReduce) {
        setRevealed(text);
        onDone?.();
        return;
      }
    }

    const intervalMs = Math.max(8, Math.round(1000 / Math.max(1, speed)));
    let i = 0;
    let timer: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      timer = setInterval(() => {
        i += 1;
        const next = targetRef.current.slice(0, i);
        setRevealed(next);
        if (i >= targetRef.current.length) {
          if (timer) clearInterval(timer);
          timer = null;
          onDone?.();
        }
      }, intervalMs);
    };

    const startTimer =
      startDelay > 0
        ? setTimeout(start, startDelay)
        : (start(), null);

    return () => {
      if (timer) clearInterval(timer);
      if (startTimer) clearTimeout(startTimer);
    };
    // We intentionally exclude `onDone` from the dep list — caller
    // identity changes on every render would otherwise restart the
    // animation on each parent re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, speed, startDelay]);

  return (
    <span className={className} style={style}>
      {revealed}
      {cursor && revealed.length < text.length ? (
        <span
          aria-hidden="true"
          style={{
            display: "inline-block",
            width: "0.5ch",
            marginLeft: 1,
            background: "currentColor",
            animation: "typewriterBlink 0.8s steps(2) infinite",
            // Slightly shorter than line-height so it doesn't poke
            // out of the text row.
            height: "0.95em",
            verticalAlign: "text-bottom",
          }}
        />
      ) : null}
    </span>
  );
}
