/**
 * The onboarding checklist.
 *
 * Every step shows real state, never a guess: a tick means the API confirmed
 * it. `pending` steps are visible from the start so the customer can see how
 * far they have to go — a wizard that reveals one step at a time feels
 * longer than it is.
 *
 * `unavailable` is deliberately distinct from `pending`. Business Profile
 * needs a separate Google approval we do not have yet, and rendering it as a
 * step that never completes would read as something broken.
 */

export type StepState = "done" | "active" | "pending" | "unavailable" | "skipped";

export interface Step {
  label: string;
  state: StepState;
  detail?: string;
}

const MARK: Record<StepState, string> = {
  done: "✓",
  active: "●",
  pending: "○",
  unavailable: "–",
  skipped: "–",
};

export function Stepper({ steps }: { steps: Step[] }) {
  return (
    <ol className="stepper">
      {steps.map((step) => (
        <li key={step.label} className={`step step--${step.state}`}>
          {/* The mark is decorative; the state is announced in text so it is
              not conveyed by symbol or colour alone. */}
          <span className="step__mark" aria-hidden="true">
            {MARK[step.state]}
          </span>
          <span className="step__body">
            <span className="step__label">{step.label}</span>
            {step.detail ? <span className="step__detail">{step.detail}</span> : null}
          </span>
          <span className="visually-hidden">{stateLabel(step.state)}</span>
        </li>
      ))}
    </ol>
  );
}

function stateLabel(state: StepState): string {
  switch (state) {
    case "done":
      return "completed";
    case "active":
      return "in progress";
    case "unavailable":
      return "not available yet";
    case "skipped":
      return "skipped";
    default:
      return "not started";
  }
}
