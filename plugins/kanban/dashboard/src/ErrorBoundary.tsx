/**
 * Kanban dashboard plugin — React error boundary wrapper.
 *
 * Because error boundaries must be class components in React, this file
 * defines a class rather than using hooks. React is obtained at runtime
 * from the host SDK via ``getReact()`` — no bundled import.
 *
 * Usage:
 *   <ErrorBoundary>{children}</ErrorBoundary>
 */

import type * as React from "react";
import { getReact } from "./sdk";

export interface ErrorBoundaryProps {
  children: React.ReactNode;
}

export interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

/**
 * Class-based error boundary that catches render errors in its subtree and
 * shows a simple fallback with a "Reload" button.
 */
export const ErrorBoundary = (function () {
  const ReactRuntime = getReact();
  const { Component, createElement } = ReactRuntime;

  class ErrorBoundaryClass extends Component<
    ErrorBoundaryProps,
    ErrorBoundaryState
  > {
    constructor(props: ErrorBoundaryProps) {
      super(props);
      this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error: Error): ErrorBoundaryState {
      return { hasError: true, error };
    }

    componentDidCatch(error: Error, info: React.ErrorInfo): void {
      // Log so devs can see the stack in the console even in production.
      console.error("[Kanban] ErrorBoundary caught:", error, info);
    }

    render(): React.ReactNode {
      if (this.state.hasError) {
        const message =
          this.state.error?.message || "Something went wrong rendering the kanban board.";
        return createElement(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              padding: "2rem",
              gap: "0.75rem",
              color: "#dc2626",
              fontFamily: "system-ui, sans-serif",
            },
          },
          createElement("p", null, message),
          createElement(
            "button",
            {
              onClick: () => window.location.reload(),
              style: {
                padding: "0.4rem 1rem",
                borderRadius: "0.375rem",
                border: "1px solid currentColor",
                background: "transparent",
                color: "inherit",
                cursor: "pointer",
                fontSize: "0.875rem",
              },
            },
            "Reload",
          ),
        );
      }
      return this.props.children;
    }
  }

  return ErrorBoundaryClass;
})();