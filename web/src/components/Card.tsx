// Card primitive — wraps .card with click + keyboard handlers.
//
// Two visual variants:
//   variant="default"  -> legacy .card (StatTile, BatchesPage Tile, etc.)
//   variant="swipe"    -> .card.card--swipe (horizontal carousel cards)
//                         with hover/focus accent border, selected state,
//                         and Enter/Space keyboard activation.
//
// Children render as the card body. data-testid is forwarded to the
// outer wrapper so e2e selectors like `[data-testid^='source-card-']`
// resolve cleanly.

import { KeyboardEvent, MouseEvent, ReactNode } from "react";

export type CardVariant = "default" | "swipe";

export interface CardProps {
  children: ReactNode;
  variant?: CardVariant;
  onClick?: (event: MouseEvent<HTMLDivElement>) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  selected?: boolean;
  "data-testid"?: string;
  "aria-label"?: string;
  role?: string;
}

export function Card(props: CardProps) {
  const {
    children,
    variant = "default",
    onClick,
    onKeyDown,
    selected = false,
    "data-testid": dataTestid,
    "aria-label": ariaLabel,
    role,
  } = props;
  const interactive = Boolean(onClick);
  const className = variant === "swipe" ? "card card--swipe" : "card";
  return (
    <div
      className={className}
      data-testid={dataTestid}
      data-selected={selected ? "true" : undefined}
      aria-label={ariaLabel}
      aria-pressed={interactive ? selected : undefined}
      role={role ?? (interactive ? "button" : undefined)}
      tabIndex={interactive ? 0 : undefined}
      onClick={onClick}
      onKeyDown={(event) => {
        if (interactive && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          onClick?.(event as unknown as MouseEvent<HTMLDivElement>);
        }
        onKeyDown?.(event);
      }}
    >
      {children}
    </div>
  );
}