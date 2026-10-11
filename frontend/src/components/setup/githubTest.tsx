/** The ✓/✗ line every test button draws (the setup checklist's Shortcut
 * test). GitHub's own test is the Connect GitHub dialog's Test
 * (components/dialogs/ConnectGitHub.tsx), the one door to that credential. */

export type TestState = { testing: boolean; ok?: boolean; msg?: string };
export const idleTest: TestState = { testing: false };

export function TestResult({ state }: { state: TestState }) {
  if (state.testing) return <span className="test-result">testing…</span>;
  if (state.msg === undefined) return <span className="test-result" />;
  return (
    <span className={"test-result " + (state.ok ? "ok" : "bad")}>
      {(state.ok ? "✓ " : "✗ ") + state.msg}
    </span>
  );
}
