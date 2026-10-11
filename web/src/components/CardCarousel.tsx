// CardCarousel — horizontal scroll-snap row of swipe cards.
//
// Renders a 12 px muted subheader (with optional count chip) above a
// .card-swipe-row container. The container is a CSS grid with
// grid-auto-flow: column + scroll-snap-type: x mandatory, so cards
// snap on touch and trackpad input.
//
// Keyboard navigation: ArrowLeft / ArrowRight on a card moves focus to
// the previous / next card and scrolls it into view. CSS scroll-snap
// alone is mouse/touch only — this is the keyboard-operator escape
// hatch.
//
// Trackpad hygiene: a debounced 'scrollend' listener flips the
// scroll-snap-type to 'none' if the natural scroll velocity is high
// (trackpads that emit many small wheel events get a sticky snap that
// fights the user; touchpads that emit a few large events are fine).

import {
  KeyboardEvent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
} from "react";

export interface CardCarouselProps {
  title: string;
  count?: number;
  "data-testid"?: string;
  children: ReactNode;
  /** When true, the carousel cards stretch full width (one card per row). */
  wide?: boolean;
}

export function CardCarousel(props: CardCarouselProps) {
  const { title, count, "data-testid": dataTestid, children, wide = false } = props;
  const trackRef = useRef<HTMLDivElement>(null);

  // Trackpad hygiene: detect high-velocity scrolling and disable snap.
  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    let velocity = 0;
    let lastX = 0;
    let lastT = 0;
    let snapDisabled = false;
    let resetTimer: ReturnType<typeof setTimeout> | null = null;

    const onScroll = () => {
      const now = performance.now();
      const x = el.scrollLeft;
      const dt = now - lastT;
      if (dt > 0 && dt < 100) {
        velocity = Math.abs(x - lastX) / dt;
        // > 1.5 px/ms is a real trackpad swipe — disable snap.
        if (velocity > 1.5 && !snapDisabled) {
          snapDisabled = true;
          el.style.scrollSnapType = "none";
        }
      }
      lastX = x;
      lastT = now;
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        // Re-arm snap after the user pauses.
        if (snapDisabled) {
          el.style.scrollSnapType = "";
          snapDisabled = false;
        }
        velocity = 0;
      }, 600);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (resetTimer) clearTimeout(resetTimer);
    };
  }, []);

  // Arrow-key navigation between cards.
  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const cards = Array.from(
      (event.currentTarget as HTMLDivElement).querySelectorAll<HTMLElement>(
        ".card[tabindex='0']",
      ),
    );
    const idx = cards.indexOf(target);
    if (idx < 0) return;
    const nextIdx =
      event.key === "ArrowRight"
        ? Math.min(idx + 1, cards.length - 1)
        : Math.max(idx - 1, 0);
    const next = cards[nextIdx];
    if (!next || next === target) return;
    event.preventDefault();
    next.focus();
    next.scrollIntoView({ inline: "start", block: "nearest", behavior: "smooth" });
  }, []);

  return (
    <div data-testid={dataTestid}>
      <div className="subhead">
        <span>{title}</span>
        {typeof count === "number" && (
          <span className="subhead-count tnum">{count}</span>
        )}
      </div>
      <div
        ref={trackRef}
        className={wide ? "card-swipe-row card-swipe-row--wide" : "card-swipe-row"}
        onKeyDown={handleKeyDown}
        role="region"
        aria-label={title}
      >
        {children}
      </div>
    </div>
  );
}