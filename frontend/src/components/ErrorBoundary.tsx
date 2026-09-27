/**
 * Keeps one failing section from blanking the whole page.
 *
 * Needed around lazily loaded views: after a redeploy, a page opened before
 * it asks for a chunk hash that no longer exists, and an unhandled rejection
 * from `React.lazy` unmounts everything up to the root.
 */

import { Component, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  fallback: ReactNode;
  children: ReactNode;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
