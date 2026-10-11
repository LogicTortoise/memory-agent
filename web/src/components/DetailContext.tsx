// DetailContext — single-instance, page-scoped detail panel.
//
// Mounted once in AppShell. Any page calls useDetail().open(title, node)
// when a card is clicked; the panel slides in from the right with the
// provided title + content node. A backdrop covers .app-main; ESC and
// backdrop click close the panel. On close, focus returns to the
// element that opened it (passed via window.__MA_LAST_OPENER__).

import {
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

export interface DetailState {
  title: string;
  content: ReactNode;
  opener: HTMLElement | null;
}

export interface DetailContextValue {
  state: DetailState | null;
  open: (next: { title: string; content: ReactNode }) => void;
  close: () => void;
}

const DetailContext = createContext<DetailContextValue | null>(null);

interface DetailPanelProps {
  state: DetailState | null;
  onClose: () => void;
}

const DetailPanel = forwardRef<HTMLDivElement, DetailPanelProps>(
  function DetailPanel(props, ref) {
    const { state, onClose } = props;
    const open = Boolean(state);
    const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
      // Simple focus trap: keep Tab inside the panel.
      if (event.key !== "Tab") return;
      const root = event.currentTarget;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>(
          "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
        ),
      ).filter((el) => !el.hasAttribute("aria-hidden"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    return (
      <>
        <div
          ref={ref}
          className="detail-panel"
          data-open={open ? "true" : "false"}
          role="dialog"
          aria-modal="true"
          aria-labelledby="ma-detail-title"
          onKeyDown={handleKeyDown}
          tabIndex={-1}
          style={open ? undefined : { display: "none" }}
        >
          <div className="detail-panel__header">
            <h2 id="ma-detail-title">{state?.title ?? ""}</h2>
            <button
              type="button"
              data-testid="detail-panel-close"
              aria-label="Close detail panel"
              onClick={onClose}
            >
              ×
            </button>
          </div>
          <div className="detail-panel__body">{state?.content}</div>
        </div>
        <div
          className="detail-panel__backdrop"
          data-open={open ? "open" : undefined}
          onClick={onClose}
          aria-hidden="true"
        />
      </>
    );
  },
);

interface DetailGlobalWindow {
  __MA_LAST_OPENER__?: HTMLElement;
}

export function DetailProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DetailState | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const open = useCallback(
    (next: { title: string; content: ReactNode }) => {
      const opener =
        typeof document !== "undefined"
          ? (document.activeElement as HTMLElement | null)
          : null;
      setState({ title: next.title, content: next.content, opener });
    },
    [],
  );

  // Stash the opener on the way out so we can restore focus after the
  // close transition completes.
  const close = useCallback(() => {
    setState((current) => {
      if (current?.opener) {
        (window as unknown as DetailGlobalWindow).__MA_LAST_OPENER__ =
          current.opener;
      }
      return null;
    });
  }, []);

  // ESC closes the panel.
  useEffect(() => {
    if (!state) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [state, close]);

  // When the panel opens, focus the first focusable child. When it
  // closes (state transitions from non-null back to null), restore
  // focus to the opener.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (state) {
      const root = panelRef.current;
      if (root) {
        const focusable = root.querySelector<HTMLElement>(
          "h2, button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
        );
        (focusable ?? root).focus();
      }
      wasOpen.current = true;
    } else if (wasOpen.current) {
      wasOpen.current = false;
      const last = (window as unknown as DetailGlobalWindow).__MA_LAST_OPENER__;
      if (last && document.contains(last)) {
        last.focus();
      }
      (window as unknown as DetailGlobalWindow).__MA_LAST_OPENER__ = undefined;
    }
  }, [state]);

  return (
    <DetailContext.Provider value={{ state, open, close }}>
      {children}
      <DetailPanel ref={panelRef} state={state} onClose={close} />
    </DetailContext.Provider>
  );
}

export function useDetail(): DetailContextValue {
  const ctx = useContext(DetailContext);
  if (!ctx) throw new Error("useDetail must be used inside <DetailProvider>");
  return ctx;
}