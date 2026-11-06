import { Component, type ErrorInfo, type ReactNode } from "react";
import { clearState } from "../storage";

type Props = { children: ReactNode };
type State = { failed: boolean };

/** Without this, one render error unmounts the whole app and the user sees a blank page. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("HerePilot-AI could not render:", error.message, info.componentStack);
  }

  private reset = (): void => {
    clearState();
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="fatal" role="alert">
        <h1>HerePilot-AI hit a problem</h1>
        <p>
          The page couldn't be displayed. This can happen when a conversation saved by an older version no longer fits the
          current app. Clearing the saved conversation usually fixes it.
        </p>
        <div className="fatal-actions">
          <button type="button" className="btn btn-primary" onClick={this.reset}>Clear saved conversation and reload</button>
          <button type="button" className="btn btn-secondary" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </main>
    );
  }
}
